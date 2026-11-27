"""
Sandbox accounts: Slack. A sandbox account connects a made-up workspace with no Slack screens,
lists its channels and "posts" without anything reaching Slack; a normal account is unchanged.
"""

import time
from urllib.parse import parse_qs, urlsplit

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

import routers.slack as slack_router
from core.config import JWT_ALGORITHM, JWT_SECRET
from main import app
from sandbox import make_credential, parse_credential, slack_fake
from services.slack_bot_service import get_decrypted_bot_token_for_user, list_bot_channels

SBX = "tester@sandbox.birdy.test"
OTHER = "second@sandbox.birdy.test"
REAL = "alice@example.com"
REDIRECT = "https://api.example.test/api/connect/slack/callback"


@pytest.fixture(autouse=True)
def _slack_config(monkeypatch):
    monkeypatch.setattr(slack_router, "SLACK_CLIENT_ID", "client-id")
    monkeypatch.setattr(slack_router, "SLACK_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(slack_router, "SLACK_REDIRECT_URI", REDIRECT)


@pytest.fixture
def real_slack(monkeypatch):
    """Every call that would have gone to Slack itself (none may, for a sandbox account)."""
    seen = []

    async def record(self, api_method, **kwargs):
        seen.append(api_method)
        raise AssertionError(f"left the process: slack {api_method}")

    monkeypatch.setitem(slack_fake._installed, "orig", record)
    return seen


def _client(user):
    token = pyjwt.encode({"sub": user, "exp": int(time.time()) + 600, "type": "access"}, JWT_SECRET,
                         algorithm=JWT_ALGORITHM)
    client = TestClient(app)
    client.cookies.set("auth_token", token)
    return client


def _follow(client, auth_url):
    parts = urlsplit(auth_url)
    return client.get(f"{parts.path}?{parts.query}", follow_redirects=False)


async def test_slack_connects_a_made_up_workspace_for_a_sandbox_account(mock_db, real_slack):
    client = _client(SBX)
    auth_url = client.get("/api/connect/slack").json()["auth_url"]
    assert auth_url.startswith(REDIRECT + "?") and "slack.com" not in auth_url
    query = parse_qs(urlsplit(auth_url).query)
    assert parse_credential(query["code"][0]).kind == slack_fake.CODE_KIND and query["state"]

    res = _follow(client, auth_url)
    assert res.status_code == 302 and res.headers["location"].endswith("status=success")

    status = client.get("/api/integrations/slack/status").json()
    assert status["installed"] is True
    token = await get_decrypted_bot_token_for_user(mock_db, SBX)
    cred = parse_credential(token)
    assert cred.user_id == SBX and cred.kind == slack_fake.TOKEN_KIND

    channels = client.get("/api/integrations/slack/channels").json()["channels"]
    assert {c["name"] for c in channels} == set(slack_fake.CHANNELS)
    picked = channels[0]
    assert client.put("/api/integrations/slack/channel",
                      json={"channel_id": picked["id"], "channel_name": picked["name"]}).status_code == 200
    assert client.get("/api/integrations/slack/status").json()["notify_channel_id"] == picked["id"]
    assert real_slack == []


async def test_a_sandbox_post_is_accepted_and_goes_nowhere(real_slack):
    client = AsyncWebClient(token=make_credential(SBX, slack_fake.TOKEN_KIND))
    sent = await client.chat_postMessage(channel="C1", text="hello")
    assert sent["ok"] and sent["channel"] == "C1" and sent["ts"]
    edited = await client.chat_update(channel="C1", ts=sent["ts"], text="hello again")
    assert edited["ts"] == sent["ts"]
    assert (await client.views_open(trigger_id="t", view={}))["ok"]
    assert [c["name"] for c in await list_bot_channels(client.token)] == list(slack_fake.CHANNELS)
    assert real_slack == []


async def test_two_sandbox_accounts_get_different_workspaces():
    assert slack_fake.team_for(SBX)["id"] != slack_fake.team_for(OTHER)["id"]
    assert slack_fake.team_for(SBX) == slack_fake.team_for(SBX)


async def test_a_forged_sandbox_token_is_refused_here_and_never_sent(real_slack):
    client = AsyncWebClient(token="sbx_not-a-real-credential")
    with pytest.raises(SlackApiError):
        await client.chat_postMessage(channel="C1", text="hello")
    assert real_slack == []


async def test_a_normal_account_still_goes_to_slack(mock_db, real_slack):
    client = _client(REAL)
    auth_url = client.get("/api/connect/slack").json()["auth_url"]
    assert auth_url.startswith("https://slack.com/oauth/v2/authorize?") and "sbx_" not in auth_url
    # a normal token is passed on to Slack untouched (here: the stand-in for Slack that records it)
    with pytest.raises(AssertionError):
        await AsyncWebClient(token="xoxb-real").chat_postMessage(channel="C1", text="hi")
    assert real_slack == ["chat.postMessage"]


async def test_callbacks_refuse_the_wrong_kind_of_code(mock_db, real_slack):
    # a normal account handing in a sandbox code
    state = slack_router._mint_install_state(REAL)
    res = _client(REAL).get("/api/connect/slack/callback",
                            params={"code": make_credential(SBX, slack_fake.CODE_KIND), "state": state},
                            follow_redirects=False)
    assert res.status_code == 302 and "status=error" in res.headers["location"]
    # a sandbox account handing in a real-looking code
    state = slack_router._mint_install_state(SBX)
    res = _client(SBX).get("/api/connect/slack/callback", params={"code": "real-code", "state": state},
                           follow_redirects=False)
    assert res.status_code == 302 and "status=error" in res.headers["location"]
    assert real_slack == []
    assert await get_decrypted_bot_token_for_user(mock_db, SBX) is None


async def test_a_sandbox_account_can_ask_for_a_real_workspace(mock_db, monkeypatch):
    sent = []

    async def slack(self, api_method, **kwargs):
        # The stand-in for Slack: the code exchange a real install makes.
        sent.append(api_method)
        return {"ok": True, "access_token": "xoxb-real-workspace", "bot_user_id": "UBOT",
                "scope": "chat:write", "team": {"id": "T0REAL", "name": "Real Workspace"}}

    monkeypatch.setitem(slack_fake._installed, "orig", slack)
    client = _client(SBX)
    before = client.get("/api/integrations/slack/status").json()
    assert before["installed"] is False and before["sandbox_account"] is True

    auth_url = client.get("/api/connect/slack", params={"real": "true"}).json()["auth_url"]
    assert auth_url.startswith("https://slack.com/oauth/v2/authorize?") and "sbx_" not in auth_url
    state = parse_qs(urlsplit(auth_url).query)["state"][0]

    res = client.get("/api/connect/slack/callback", params={"code": "real-code", "state": state},
                     follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"].endswith("status=success")
    assert sent == ["oauth.v2.access"]
    assert await get_decrypted_bot_token_for_user(mock_db, SBX) == "xoxb-real-workspace"
    status = client.get("/api/integrations/slack/status").json()
    assert status["team_name"] == "Real Workspace" and status["sandbox_account"] and not status["test_workspace"]


async def test_the_test_workspace_reads_as_one(mock_db, real_slack):
    client = _client(SBX)
    _follow(client, client.get("/api/connect/slack").json()["auth_url"])
    status = client.get("/api/integrations/slack/status").json()
    assert status["sandbox_account"] and status["test_workspace"]


async def test_a_normal_account_cannot_use_the_real_flag_to_skip_anything(mock_db, real_slack):
    # ?real=true changes nothing for a normal account, and a sandbox code is still refused.
    client = _client(REAL)
    auth_url = client.get("/api/connect/slack", params={"real": "true"}).json()["auth_url"]
    assert auth_url.startswith("https://slack.com/oauth/v2/authorize?")
    state = slack_router._mint_install_state(REAL, real=True)
    res = client.get("/api/connect/slack/callback",
                     params={"code": make_credential(SBX, slack_fake.CODE_KIND), "state": state},
                     follow_redirects=False)
    assert res.status_code == 302 and "status=error" in res.headers["location"]
    assert client.get("/api/integrations/slack/status").json()["sandbox_account"] is False
    assert real_slack == []
