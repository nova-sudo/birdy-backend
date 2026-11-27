"""
sandbox/slack_fake.py
---------------------
Slack for a sandbox account. Slack lets a workspace be linked to one Birdy account only (the
unique index on integrations.slack_bot.team_id), so a tester whose workspace already belongs to
their real account could never finish the Slack step of onboarding. A sandbox account therefore
gets a made-up workspace: connecting skips Slack's own screens, the channel list is generated, and
every post is accepted and goes nowhere.

The Slack SDK does not use httpx, so sandbox/routing.py's hook never sees it. Its one choke point
is `AsyncWebClient.api_call` (every method goes through it): `install()` wraps that. A call is
answered here when the client's token, or the OAuth `code` being exchanged, is a sandbox
credential; any other call goes to Slack unchanged.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import time
from typing import Optional

from sandbox.credentials import PREFIX, make_credential, parse_credential

logger = logging.getLogger(__name__)

CODE_KIND = "slack_code"
# Every made-up workspace id starts with this, so a status read can tell a test workspace from a real one.
TEAM_PREFIX = "TSBX"
TOKEN_KIND = "slack"

# What Birdy asks for at install; echoed back as the granted scope.
SCOPE = "chat:write,channels:read,groups:read,commands"
CHANNELS = ("general", "birdy-alerts", "client-reports", "media-buying", "sales-team")

_counter = itertools.count(1)
_installed = {"done": False, "orig": None}


def _short(user_id: str, salt: str, n: int = 9) -> str:
    return hashlib.sha256(f"{salt}:{user_id}".encode()).hexdigest()[:n].upper()


def team_for(user_id: str) -> dict:
    """The account's made-up workspace. Its id is unique per account (the index requires it)."""
    name = user_id.split("@", 1)[0].replace(".", " ").replace("_", " ").title() or "Sandbox"
    return {"id": f"{TEAM_PREFIX}{_short(user_id, 'team')}", "name": f"{name} Sandbox Workspace"}


def channels_for(user_id: str) -> list[dict]:
    return [{"id": f"CSBX{_short(user_id, name)}", "name": name, "is_private": False, "is_member": True,
             "is_channel": True, "is_archived": False, "num_members": 3 + i}
            for i, name in enumerate(CHANNELS)]


def _ts() -> str:
    return f"{time.time():.6f}"[:-3] + f"{next(_counter) % 1000:03d}"


def _fields(*sources) -> dict:
    out: dict = {}
    for src in sources:
        if isinstance(src, dict):
            out.update({k: v for k, v in src.items() if v is not None})
    return out


def _credential(client, fields: dict):
    """The sandbox credential a call carries: its client's token, or the code being exchanged."""
    for value in (getattr(client, "token", None), fields.get("code"), fields.get("token")):
        if isinstance(value, str) and value.startswith(PREFIX):
            return parse_credential(value)
    return None


def carries_sandbox_credential(client, fields: dict) -> bool:
    return any(isinstance(v, str) and v.startswith(PREFIX)
               for v in (getattr(client, "token", None), fields.get("code"), fields.get("token")))


def answer(method: str, cred, fields: dict) -> dict:
    """Slack's reply to `method` for this account, as the data of a successful response."""
    user_id = cred.user_id
    channel = fields.get("channel")
    if method == "oauth.v2.access":
        return {"ok": True, "access_token": make_credential(user_id, TOKEN_KIND), "token_type": "bot",
                "scope": SCOPE, "bot_user_id": f"USBX{_short(user_id, 'bot')}", "app_id": "ASBXBIRDY",
                "team": team_for(user_id), "authed_user": {"id": f"USBX{_short(user_id, 'owner')}"}}
    if method == "auth.test":
        team = team_for(user_id)
        return {"ok": True, "team": team["name"], "team_id": team["id"],
                "user_id": f"USBX{_short(user_id, 'bot')}", "bot_id": f"BSBX{_short(user_id, 'bot')}"}
    if method == "conversations.list":
        return {"ok": True, "channels": channels_for(user_id), "response_metadata": {"next_cursor": ""}}
    if method in ("chat.postMessage", "chat.update"):
        ts = fields.get("ts") or _ts()
        return {"ok": True, "channel": channel, "ts": ts,
                "message": {"type": "message", "ts": ts, "text": fields.get("text") or ""}}
    if method == "chat.postEphemeral":
        return {"ok": True, "message_ts": _ts()}
    if method == "views.open":
        return {"ok": True, "view": {"id": f"VSBX{_short(user_id, _ts())}"}}
    if method == "conversations.join":
        return {"ok": True, "channel": {"id": channel}}
    logger.warning("sandbox_unhandled slack %s", method)
    return {"ok": True}


async def _api_call(self, api_method: str, *, http_verb: str = "POST", files=None, data=None, params=None,
                    json=None, headers=None, auth=None):
    fields = _fields(params, data if isinstance(data, dict) else None, json)
    if not carries_sandbox_credential(self, fields):
        return await _installed["orig"](self, api_method, http_verb=http_verb, files=files, data=data,
                                        params=params, json=json, headers=headers, auth=auth)
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    cred = _credential(self, fields)
    body = answer(api_method, cred, fields) if cred else {"ok": False, "error": "invalid_auth"}
    response = AsyncSlackResponse(client=self, http_verb=http_verb, api_url=f"sandbox://slack/{api_method}",
                                  req_args={}, data=body, headers={}, status_code=200)
    if not body.get("ok"):
        # A forged or foreign sandbox token: refused here, never sent to Slack.
        raise SlackApiError(message=f"The request to the Slack API failed. (sandbox: {api_method})", response=response)
    return response


def install() -> None:
    """Answer sandbox Slack calls in-process (idempotent)."""
    if _installed["done"]:
        return
    from slack_sdk.web.async_client import AsyncWebClient

    _installed["orig"] = AsyncWebClient.api_call
    AsyncWebClient.api_call = _api_call
    _installed["done"] = True


def uninstall() -> None:
    if not _installed["done"]:
        return
    from slack_sdk.web.async_client import AsyncWebClient

    AsyncWebClient.api_call = _installed["orig"]
    _installed["done"] = False
