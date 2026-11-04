"""
tests/test_stock_slack.py
-------------------------
The stock agent in Slack: posting a run, the urgent alerts, the buttons, the edit form and @Birdy replies.
Every Slack call goes to FakeSlack and every Meta call to test_stock_apply's FakeMeta. Nothing leaves the test.
"""

import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from slack_sdk.errors import SlackApiError

from ai import provider_factory
from ai.agents.stock import lessons, runner
from ai.agents.stock import slack as stock_slack
from ai.agents.stock.apply import apply_card, process_queued_lines
from ai.agents.stock.slack import (edit_view, finish_action, handle_card_action, handle_edit_submission,
                                   handle_thread_mention, post_urgent_alerts, publish_run, sync_card_messages,
                                   update_card_message)
from ai.agents.store import AGENTS, CARDS, MEMORY, RUNS
from ai.provider_factory import NoAiCredentialError
from routers import slack_events, slack_interactions
from services.slack_bot_service import get_slack_bot_status, save_slack_bot_installation
from test_stock_apply import GID, _ads, _agent, _card, _get, _line, _ln, _run, meta  # noqa: F401 - meta is a fixture
from test_stock_runner import USER

TEAM, OTHER_TEAM, OTHER_USER = "T1", "T2", "other@x.com"
SIGNING_SECRET = "test-stock-slack-secret"
SLACK = {"channel_id": "C1", "channel_name": "ads", "also_tag_user_id": "U777", "templates": {}}


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.ephemeral, self.views = [], [], [], []
        self.calls = []
        self.fail: dict[str, list] = {}
        self.n = 0

    def _maybe_fail(self, method: str, kw: dict):
        queue = self.fail.get(method)
        if queue:
            err = queue.pop(0)
            if callable(err):
                err = err(kw)
            if err:
                raise SlackApiError(err, {"ok": False, "error": err})

    async def chat_postMessage(self, **kw):
        self.calls.append("post")
        self._maybe_fail("post", kw)
        self.n += 1
        self.posts.append(kw)
        return {"ok": True, "ts": f"100.{self.n}", "channel": kw["channel"]}

    async def chat_update(self, **kw):
        self.calls.append("update")
        self._maybe_fail("update", kw)
        self.updates.append(kw)
        return {"ok": True, "ts": kw["ts"]}

    async def chat_postEphemeral(self, **kw):
        self.calls.append("ephemeral")
        self.ephemeral.append(kw)
        return {"ok": True}

    async def views_open(self, **kw):
        self.calls.append("views_open")
        self.views.append(kw)
        return {"ok": True}


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    """The AI that shapes a lesson never runs here: with no provider, the person's own words are kept."""
    async def none(*a, **k):
        raise NoAiCredentialError("no AI in tests")

    monkeypatch.setattr(provider_factory, "get_provider_for_user", none)


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(stock_slack, "AsyncWebClient", lambda **kw: fake)
    return fake


@pytest.fixture
async def installed(mock_db):
    await save_slack_bot_installation(mock_db, USER, TEAM, "xoxb-test", "UBOT")
    await save_slack_bot_installation(mock_db, OTHER_USER, OTHER_TEAM, "xoxb-other", "UBOT2")


async def _slack_agent(db, *, slack_settings=None, gids=(GID,)):
    from ai.agents.stock.settings import DEFAULT_SETTINGS
    return await _agent(db, gids=gids, settings={
        "modes": {"remove": "ask", "revive": "ask", "add": "ask"}, "slack": slack_settings or SLACK,
        "batches": DEFAULT_SETTINGS["batches"]})


async def _stored_run(db, agent, **extra) -> dict:
    run = _run(agent, **{"summary": {"clients": 1}, **extra})
    await db[RUNS].insert_one(run)
    return await db[RUNS].find_one({"_id": run["_id"]})


def _all_text(blocks) -> str:
    return json.dumps(blocks or [])


def _action_ids(blocks) -> list[str]:
    return [el.get("action_id") for b in blocks or [] if b.get("type") == "actions" for el in b["elements"]]


def _press(action_id, value=None, *, team=TEAM, ts="100.2", user=None, trigger="trig"):
    action = {"action_id": action_id, "type": "button", **({"value": value} if value is not None else {})}
    return ({"type": "block_actions", "team": {"id": team}, "trigger_id": trigger,
             "user": user if user is not None else {"id": "U1", "name": "sam"},
             "channel": {"id": "C1"}, "message": {"ts": ts, "thread_ts": "100.1"}, "actions": [action]}, action)


async def _posted_card(db, agent, *lines, **extra):
    """A card whose message is already in Slack."""
    return await _card(db, agent, *lines, slack={"channel_id": "C1", "ts": "100.2", "thread_ts": "100.1"}, **extra)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

async def test_summary_then_one_reply_per_card_with_thread_ts(mock_db, slack, installed):
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await _card(mock_db, agent, _id="c2", gid="g2", tag="green")
    cards = await mock_db[CARDS].find({}).to_list(None)

    await publish_run(mock_db, run, agent, cards)

    assert len(slack.posts) == 3
    summary, *replies = slack.posts
    assert summary["channel"] == "C1" and "thread_ts" not in summary
    assert "agent_open_report" in _action_ids(summary["blocks"]) and "agent_run_apply_all" in _action_ids(summary["blocks"])
    assert all(r["thread_ts"] == "100.1" and r["channel"] == "C1" for r in replies)
    stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert stored["slack"] == {"channel_id": "C1", "ts": "100.1"}
    assert (await _get(mock_db, "c1"))["slack"]["ts"] == "100.2"
    assert (await _get(mock_db, "c1"))["slack"]["thread_ts"] == "100.1"
    assert "agent_card_apply" in _action_ids(replies[0]["blocks"])


async def test_a_repeat_publishes_only_new_cards_and_updates_the_summary(mock_db, slack, installed):
    """A kill or an auto_apply_run raise repeats publishing even with no new cards."""
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert len(slack.posts) == 2

    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert len(slack.posts) == 2 and [u["ts"] for u in slack.updates] == ["100.1"]

    await _card(mock_db, agent, _id="c2", gid="g2")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert len(slack.posts) == 3 and slack.posts[-1]["thread_ts"] == "100.1" and len(slack.updates) == 2


async def test_all_clean_alert_day_posts_one_line_no_report(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    alert = {"client_group_id": GID, "code": "prepaid_no_topups", "severity": "info", "title": "Prepaid"}
    late = {"client_group_id": GID, "code": "spend_data_late", "severity": "info", "title": "Late"}
    run = await _stored_run(mock_db, agent, kind="alert_only", alerts=[alert, late])
    await publish_run(mock_db, run, agent, [])
    assert len(slack.posts) == 1
    assert not slack.posts[0].get("blocks") and "\n" not in slack.posts[0]["text"]

    await publish_run(mock_db, run, agent, [])
    assert len(slack.posts) == 1


@pytest.mark.parametrize("summary,alerts,shows", [
    ({"health_unread": [GID]}, [], "health not read yet"),
    ({"could_not_check": [{"client_group_id": GID, "reason": "Boom"}]}, [], "Boom"),
    ({}, [{"client_group_id": GID, "code": "spend_drop", "severity": "warning", "title": "Spend dropped"}], "Spend dropped"),
    ({"reconnect": True}, [{"client_group_id": None, "code": "meta_reconnect", "severity": "critical",
                            "title": "Meta needs reconnecting"}], "Meta needs reconnecting"),
])
async def test_what_blocks_an_all_clean_day(mock_db, slack, installed, summary, alerts, shows):
    agent = await _slack_agent(mock_db)
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": "Client g1"}})
    run = await _stored_run(mock_db, agent, kind="alert_only", summary=summary, alerts=alerts)
    await publish_run(mock_db, run, agent, [])
    assert len(slack.posts) == 1 and slack.posts[0]["blocks"]
    assert shows in _all_text(slack.posts[0]["blocks"])
    if "health_unread" in summary:
        assert "Client g1" in _all_text(slack.posts[0]["blocks"])


async def test_an_alert_day_names_the_client_though_it_has_no_card(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    alert = {"client_group_id": GID, "code": "spend_drop", "severity": "warning", "title": "Spend dropped"}
    run = await _stored_run(mock_db, agent, kind="alert_only", alerts=[alert])
    await publish_run(mock_db, run, agent, [])
    assert "Client g1: Spend dropped" in _all_text(slack.posts[0]["blocks"])


async def test_a_preview_run_never_reaches_slack(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, kind="preview", alerts=[
        {"client_group_id": GID, "code": "account_disabled", "severity": "critical", "title": "Disabled"}])
    await publish_run(mock_db, run, agent, [])
    await post_urgent_alerts(mock_db, run, agent)
    assert slack.calls == []


async def test_post_failure_keeps_report_and_retries_next_tick_once(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    """Through the runner: the report stays, and each message reaches Slack once using the stored ts."""
    async def no_auto(*a, **k):
        return None

    monkeypatch.setattr(runner, "auto_apply_run", no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None},
                            clients={GID: {"state": "done", "health_done": True}})
    await _card(mock_db, agent, _ln(), _id="c1", run_id=run["_id"], slack={"channel_id": None, "ts": None})
    slack.fail["post"] = [None, "fatal_error"]  # the summary goes out, its card doesn't

    now = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=now)
    assert run["phase"] == "waiting" and "fatal_error" in run["publish_error"]
    assert await _get(mock_db, "c1") and len(slack.posts) == 1

    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=now + timedelta(minutes=15))
    assert run["phase"] == "done" and not run.get("publish_error")
    assert len(slack.posts) == 2 and slack.posts[1]["thread_ts"] == "100.1"
    assert [u["ts"] for u in slack.updates] == ["100.1"]   # the summary, updated and not posted again
    assert (await mock_db[RUNS].find_one({"_id": run["_id"]}))["slack"]["ts"] == "100.1"


async def test_publish_gives_up_after_its_tries(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    async def no_auto(*a, **k):
        return None

    monkeypatch.setattr(runner, "auto_apply_run", no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None},
                            clients={GID: {"state": "done", "health_done": True}})
    slack.fail["post"] = ["channel_not_found"] * 10
    now = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
    for n in range(5):
        run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None,
                                       now_utc=now + timedelta(minutes=15 * n))
    assert run["phase"] == "done" and slack.calls.count("post") == runner.PUBLISH_TRIES
    assert "invite" in run["publish_error"].lower()


async def test_slack_disconnected_records_error_ask_lines_still_apply(mock_db, mock_mongo_client, slack, meta):
    """No install at all. The run says why, and Apply works from the report."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    await _card(mock_db, agent, _ln(), _id="c1", run_id=run["_id"], slack={"channel_id": None, "ts": None})
    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None,
                                   now_utc=datetime(2026, 11, 3, 7, tzinfo=timezone.utc))
    assert run["phase"] == "done" and "Slack" in run["publish_error"] and slack.calls == []

    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="dashboard")
    assert res["outcome"] == "applied" and meta.real_writes == [("ad1", "PAUSED")]
    assert await update_card_message(mock_db, await _get(mock_db)) is False and slack.calls == []


async def test_no_channel_chosen_records_error(mock_db, slack, installed):
    agent = await _slack_agent(mock_db, slack_settings={"channel_id": None, "templates": {}})
    run = await _stored_run(mock_db, agent)
    with pytest.raises(stock_slack.SlackPublishError) as e:
        await publish_run(mock_db, run, agent, [])
    assert e.value.retry is False and "channel" in str(e.value).lower() and slack.calls == []


async def test_a_dead_token_marks_reconnect_and_stops(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    slack.fail["post"] = ["token_revoked"]

    async def no_auto(*a, **k):
        return None

    monkeypatch.setattr(runner, "auto_apply_run", no_auto)
    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None,
                                   now_utc=datetime(2026, 11, 3, 7, tzinfo=timezone.utc))
    assert run["phase"] == "done" and "reconnect" in run["publish_error"].lower()
    assert (await get_slack_bot_status(mock_db, USER))["needs_reconnect"] is True


async def test_publish_stops_at_the_deadline_and_keeps_what_it_posted(mock_db, slack, installed):
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    run = await _stored_run(mock_db, agent)
    run["tick_deadline"] = time.monotonic() - 1
    await _card(mock_db, agent, _ln(), _id="c1")
    with pytest.raises(stock_slack.SlackPublishError) as e:
        await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert e.value.retry is True
    # The summary goes first, or there's no thread; a card waits for the next tick.
    assert len(slack.posts) == 1 and not (await _get(mock_db)).get("slack")


async def test_failed_template_render_uses_default_and_sets_notice(mock_db, slack, installed):
    bad = {**SLACK, "templates": {"card": {"blocks": "this isn't blocks"}}}
    agent = await _slack_agent(mock_db, slack_settings=bad)
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert len(slack.posts) == 2
    assert "agent_card_apply" in _action_ids(slack.posts[1]["blocks"])
    stored = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    assert stored["slack_template_notices"]["card"]["message"]
    assert "summary" not in stored["slack_template_notices"]


async def test_a_template_slack_refuses_is_sent_with_the_default_and_sets_notice(mock_db, slack, installed):
    custom = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Mine: {{client}}"}}]}
    agent = await _slack_agent(mock_db, slack_settings={**SLACK, "templates": {"card": custom}})
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    slack.fail["post"] = [None, "invalid_blocks"]
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert len(slack.posts) == 2 and "Mine:" not in _all_text(slack.posts[1]["blocks"])
    stored = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    assert "card" in stored["slack_template_notices"]


async def test_a_custom_template_is_used_and_the_buttons_stay(mock_db, slack, installed):
    custom = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Mine: {{client}}"}}]}
    agent = await _slack_agent(mock_db, slack_settings={**SLACK, "templates": {"card": custom}})
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert "Mine: Client g1" in _all_text(slack.posts[1]["blocks"])
    assert "agent_card_apply" in _action_ids(slack.posts[1]["blocks"])
    assert "slack_template_notices" not in (await mock_db[AGENTS].find_one({"_id": agent["_id"]}))


async def test_also_tag_person_is_mentioned_in_summary(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert "<@U777>" in _all_text(slack.posts[0]["blocks"])
    assert "<@U777>" not in _all_text(slack.posts[1]["blocks"])


async def test_a_bad_also_tag_never_pings(mock_db, slack, installed):
    agent = await _slack_agent(mock_db, slack_settings={**SLACK, "also_tag_user_id": "<!channel>"})
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert "<!channel>" not in _all_text(slack.posts[0]["blocks"])


async def test_an_ad_name_can_not_ping_the_channel(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, {**_ln(state="blocked", error="Nope"), "ad_name": "<!channel> sale"}, _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert "<!channel>" not in _all_text(slack.posts[1]["blocks"])


async def test_chart_groups_are_never_passed_to_rendering(mock_db, slack, installed, monkeypatch):
    seen = []
    real = stock_slack.make_context

    def spy(*a, **k):
        seen.append(k.get("groups"))
        return real(*a, **k)

    monkeypatch.setattr(stock_slack, "make_context", spy)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert seen and all(g == [] for g in seen)


async def test_card_buttons_get_the_real_has_newer(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1", status="partly_applied")
    await _card(mock_db, agent, _id="c2", run_id="r2", created=datetime.utcnow(), slack={"channel_id": "C1", "ts": "9.9"})
    await publish_run(mock_db, run, agent, [await _get(mock_db, "c1")])
    assert "agent_card_apply" not in _action_ids(slack.posts[1]["blocks"])


async def test_a_publish_that_gets_further_uses_no_try(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    """A big agency can take several ticks to post. Only a tick that gets nowhere counts."""
    async def no_auto(*a, **k):
        return None

    async def partial(db, run, agent, cards):
        raise stock_slack.SlackPublishError("out of time", posted=5)

    monkeypatch.setattr(runner, "auto_apply_run", no_auto)
    monkeypatch.setattr(runner, "publish_run", partial)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    now = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
    for n in range(runner.PUBLISH_TRIES + 2):
        run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None,
                                       now_utc=now + timedelta(minutes=15 * n))
    assert run["phase"] == "waiting" and run["publish_tries"] == 0


async def test_a_retry_waits_for_a_later_tick(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    calls = []

    async def no_auto(*a, **k):
        return None

    async def fails(db, run, agent, cards):
        calls.append(1)
        raise RuntimeError("slack down")

    monkeypatch.setattr(runner, "auto_apply_run", no_auto)
    monkeypatch.setattr(runner, "publish_run", fails)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None,
                                   now_utc=datetime(2026, 11, 3, 7, tzinfo=timezone.utc))
    assert len(calls) == 1 and run["phase"] == "waiting" and run["claimed_at"] is None


async def test_the_template_notice_shows_in_settings_until_it_is_saved(mock_db, slack, installed):
    from routers.agent_stock import _save_template, _template_view
    bad = {**SLACK, "templates": {"card": {"blocks": "this isn't blocks"}}}
    agent = await _slack_agent(mock_db, slack_settings=bad)
    await stock_slack._note_template_failure(mock_db, agent, "card")
    agent = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    assert "default layout" in _template_view("card", agent)["notice"]
    assert _template_view("summary", agent)["notice"] is None
    saved = await _save_template(mock_db, agent, USER, "card", None)
    assert _template_view("card", saved)["notice"] is None


# ---------------------------------------------------------------------------
# Urgent alerts
# ---------------------------------------------------------------------------

DISABLED = {"client_group_id": GID, "code": "account_disabled", "severity": "critical", "title": "Ad account is disabled",
            "detail": "Reason"}
REJECTED = {"client_group_id": GID, "code": "ad_rejected", "severity": "warning", "title": "1 ad rejected", "detail": "Why"}
INFO = {"client_group_id": GID, "code": "prepaid_no_topups", "severity": "info", "title": "No top-ups", "detail": ""}
DROP = {"client_group_id": GID, "code": "spend_drop", "severity": "warning", "title": "Spend dropped", "detail": ""}


async def test_urgent_alerts_post_once_each_as_more_are_read(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, alerts=[DISABLED, INFO, DROP])
    await post_urgent_alerts(mock_db, run, agent)
    assert len(slack.posts) == 1 and "disabled" in slack.posts[0]["text"]
    stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert len(stored["urgent_posted_keys"]) == 1

    stored["alerts"] = [DISABLED, INFO, DROP, REJECTED]
    await post_urgent_alerts(mock_db, stored, agent)
    assert len(slack.posts) == 2 and "rejected" in slack.posts[1]["text"]

    await post_urgent_alerts(mock_db, await mock_db[RUNS].find_one({"_id": run["_id"]}), agent)
    assert len(slack.posts) == 2


async def test_each_urgent_key_is_saved_right_after_its_post(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    second = {**DISABLED, "client_group_id": "g2"}
    run = await _stored_run(mock_db, agent, alerts=[DISABLED, second])
    slack.fail["post"] = [None, "fatal_error"]
    with pytest.raises(stock_slack.SlackPublishError):
        await post_urgent_alerts(mock_db, run, agent)
    assert len((await mock_db[RUNS].find_one({"_id": run["_id"]}))["urgent_posted_keys"]) == 1
    await post_urgent_alerts(mock_db, await mock_db[RUNS].find_one({"_id": run["_id"]}), agent)
    assert len(slack.posts) == 2 and slack.calls.count("post") == 3


async def test_urgent_posting_has_its_own_timeout(mock_db, slack, installed, monkeypatch):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, alerts=[DISABLED])

    async def hangs(**kw):
        await asyncio.sleep(30)

    slack.chat_postMessage = hangs
    monkeypatch.setattr(stock_slack, "URGENT_TIMEOUT_S", 0.05)
    started = time.monotonic()
    with pytest.raises(stock_slack.SlackPublishError):
        await post_urgent_alerts(mock_db, run, agent)
    assert time.monotonic() - started < 2


async def test_urgent_timeout_shrinks_near_the_deadline(mock_db, slack, installed, monkeypatch):
    run = {"tick_deadline": time.monotonic() - 1000}
    assert stock_slack._urgent_timeout(run) == 1.0
    assert stock_slack._urgent_timeout({}) == stock_slack.URGENT_TIMEOUT_S


async def test_urgent_alerts_are_not_posted_again_in_the_summary_run(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, alerts=[DISABLED])
    await post_urgent_alerts(mock_db, run, agent)
    run = await mock_db[RUNS].find_one({"_id": run["_id"]})
    await publish_run(mock_db, run, agent, [])
    await post_urgent_alerts(mock_db, await mock_db[RUNS].find_one({"_id": run["_id"]}), agent)
    assert len(slack.posts) == 2  # the alert, then the summary that lists it


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

async def test_apply_button_updates_message_with_who_and_results(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload, action = _press("agent_card_apply", "c1")

    assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}

    assert meta.real_writes == [("ad1", "PAUSED")]
    card = await _get(mock_db)
    assert card["decided_by"]["by"] == "sam" and card["decided_by"]["source"] == "slack"
    assert len(slack.updates) == 1
    update = slack.updates[0]
    assert update["channel"] == "C1" and update["ts"] == "100.2"
    text = _all_text(update["blocks"])
    assert "sam" in text and "applied" in text and "Name ad1" in text
    assert "agent_card_apply" not in _action_ids(update["blocks"]) and "agent_card_undo:l1" in _action_ids(update["blocks"])


async def test_who_falls_back_to_a_mention(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload, action = _press("agent_card_apply", "c1", user={"id": "UABC123"})
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert (await _get(mock_db))["decided_by"]["by"] == "<@UABC123>"
    assert "<@UABC123>" in _all_text(slack.updates[0]["blocks"])


async def test_a_strange_name_is_shown_escaped(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload, action = _press("agent_card_apply", "c1", user={"id": "U9", "name": "<!channel>"})
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert "<!channel>" not in _all_text(slack.updates[0]["blocks"])


async def test_action_from_another_team_is_ignored(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    before = await _get(mock_db)
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}))

    for action_id, value in (("agent_card_apply", "c1"), ("agent_card_dismiss", "c1"), ("agent_card_edit", "c1"),
                             ("agent_card_undo:l1", "c1:l1"), ("agent_run_apply_all", "r1")):
        for team in (OTHER_TEAM, "T_UNKNOWN", None):
            payload, action = _press(action_id, value, team=team)
            assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}

    assert meta.real_writes == [] and slack.calls == []
    assert await _get(mock_db) == before
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    assert "apply_all" not in (await mock_db[RUNS].find_one({"_id": "r1"}))


async def test_second_press_returns_the_existing_result(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload, action = _press("agent_card_apply", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert meta.real_writes == [("ad1", "PAUSED")] and len(slack.updates) == 2


async def test_apply_passes_a_budget_and_the_cron_finishes_what_doesnt_fit(mock_db, mock_mongo_client, slack, installed, meta,
                                                                           monkeypatch):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    meta.write_delay = 0.2
    monkeypatch.setattr(stock_slack, "APPLY_BUDGET_S", 0.0)
    payload, action = _press("agent_card_apply", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "queued" and meta.real_writes == []
    assert "queued" in _all_text(slack.updates[-1]["blocks"])

    meta.write_delay = 0
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert (await _line(mock_db))["state"] == "applied"
    assert "applied" in _all_text(slack.updates[-1]["blocks"])
    assert "agent_card_undo:l1" in _action_ids(slack.updates[-1]["blocks"])


async def test_a_refused_apply_tells_only_the_person_who_pressed(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1", status="superseded")
    payload, action = _press("agent_card_apply", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert meta.real_writes == [] and slack.posts == []
    assert slack.ephemeral[0]["user"] == "U1" and "newer" in slack.ephemeral[0]["text"]
    assert _action_ids(slack.updates[0]["blocks"]) == []


async def test_a_mode_now_flag_line_shows_its_note_on_the_card(mock_db, mock_mongo_client, slack, installed, meta):
    """Mixed card: the outcome has reason None, so the note can only come from the line."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _posted_card(mock_db, agent, _ln(1), _ln(2, kind="revive", ad="ad2"), _id="c1")
    settings = {**agent["settings"], "modes": {"remove": "ask", "revive": "flag", "add": "ask"}}
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings": settings}})
    payload, action = _press("agent_card_apply", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    from ai.agents.stock.apply import NOW_FLAG
    assert (await _line(mock_db, "l2"))["state"] == "flagged"
    assert NOW_FLAG in _all_text(slack.updates[-1]["blocks"])


async def test_undo_acknowledges_first_and_finishes_after(mock_db, mock_mongo_client, slack, installed, meta):
    """undo_line can take a lease wait plus three Graph calls, which passes Slack's 3 s."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    writes = list(meta.real_writes)
    deferred = []
    payload, action = _press("agent_card_undo:l1", "c1:l1")

    assert await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append) == {}

    # Answered with nothing sent to Meta and the line untouched: the work is stored, not done.
    assert meta.real_writes == writes and (await _line(mock_db))["state"] == "applied"
    assert len(deferred) == 1 and await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 1

    await finish_action(mock_db, mock_mongo_client, deferred[0])

    assert meta.real_writes == [*writes, ("ad1", "ACTIVE")] and (await _line(mock_db))["state"] == "undone"
    assert (await _line(mock_db))["undo_decision"]["by"] == "sam"
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    assert "undone" in _all_text(slack.updates[-1]["blocks"]) and slack.updates[-1]["ts"] == "100.2"


async def test_undo_lost_to_a_frozen_function_is_finished_by_the_cron(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    payload, action = _press("agent_card_undo:l1", "c1:l1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=lambda _: None)  # never ran

    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert (await _line(mock_db))["state"] == "undone" and meta.real_writes[-1] == ("ad1", "ACTIVE")


async def test_two_undo_presses_make_one_write(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    deferred = []
    payload, action = _press("agent_card_undo:l1", "c1:l1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 1
    await asyncio.gather(*(finish_action(mock_db, mock_mongo_client, d) for d in deferred))
    assert meta.real_writes.count(("ad1", "ACTIVE")) == 1


async def test_an_action_claimed_by_a_killed_function_is_taken_over(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    deferred = []
    payload, action = _press("agent_card_undo:l1", "c1:l1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    rows = mock_db[stock_slack.SLACK_ACTIONS]

    await rows.update_one({"_id": deferred[0]}, {"$set": {"claimed_at": datetime.utcnow()}})
    assert await finish_action(mock_db, mock_mongo_client, deferred[0]) is False  # still alive, as far as anyone knows
    assert (await _line(mock_db))["state"] == "applied"

    await rows.update_one({"_id": deferred[0]}, {"$set": {"claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    assert await finish_action(mock_db, mock_mongo_client, deferred[0]) is True
    assert (await _line(mock_db))["state"] == "undone"


async def test_an_action_that_keeps_failing_is_dropped_and_the_person_told(mock_db, mock_mongo_client, slack, installed, meta,
                                                                           monkeypatch):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    deferred = []
    payload, action = _press("agent_card_dismiss", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)

    async def broken(*a, **k):
        raise RuntimeError("mongo blip")

    monkeypatch.setattr(stock_slack, "dismiss_card", broken)
    rows = mock_db[stock_slack.SLACK_ACTIONS]
    for _ in range(stock_slack.ACTION_TRIES + 1):
        await rows.update_one({"_id": deferred[0]}, {"$set": {"claimed_at": None}})
        await finish_action(mock_db, mock_mongo_client, deferred[0])
    assert await rows.count_documents({}) == 0
    assert "couldn't" in slack.ephemeral[-1]["text"].lower()


async def test_undo_refused_tells_the_person_and_shows_it(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(state="applied", applied_at=datetime.utcnow() - timedelta(days=3),
                                           undo_until=datetime.utcnow() - timedelta(days=2), wrote=True), _id="c1")
    deferred = []
    payload, action = _press("agent_card_undo:l1", "c1:l1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    await finish_action(mock_db, mock_mongo_client, deferred[0])
    assert meta.real_writes == [] and "24 hours" in slack.ephemeral[-1]["text"]


async def test_undo_button_must_name_its_own_line(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload, action = _press("agent_card_undo:l9", "c1:l1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=lambda _: None)
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0


async def test_dismiss_acknowledges_first_and_finishes_after(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    deferred = []
    payload, action = _press("agent_card_dismiss", "c1")
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append) == {}
    assert (await _get(mock_db))["status"] == "open"

    await finish_action(mock_db, mock_mongo_client, deferred[0])

    card = await _get(mock_db)
    assert card["status"] == "dismissed" and card["decided_by"]["by"] == "sam"
    text = _all_text(slack.updates[-1]["blocks"])
    assert "Dismissed by sam" in text and _action_ids(slack.updates[-1]["blocks"]) == []


async def test_apply_all_queues_and_updates_the_summary(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}, summary={"clients": 1}))
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload, action = _press("agent_run_apply_all", "r1", ts="100.1")
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}

    assert meta.real_writes == [] and (await _line(mock_db))["state"] == "queued"
    assert (await _line(mock_db))["decision"]["by"] == "sam"
    update = slack.updates[-1]
    assert update["ts"] == "100.1" and "agent_run_apply_all" not in _action_ids(update["blocks"])
    assert "sam" in _all_text(update["blocks"])
    assert slack.ephemeral

    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert (await _line(mock_db))["state"] == "applied"
    assert any(u["ts"] == "100.2" and "applied" in _all_text(u["blocks"]) for u in slack.updates)


async def test_apply_all_with_nothing_waiting_says_so(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}, summary={"clients": 1}))
    payload, action = _press("agent_run_apply_all", "r1", ts="100.1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert meta.real_writes == [] and "Nothing" in slack.ephemeral[-1]["text"]


async def test_open_report_is_only_acknowledged(mock_db, mock_mongo_client, slack, installed):
    payload, action = _press("agent_open_report")
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}
    assert slack.calls == []


async def test_a_change_made_in_birdy_reaches_the_slack_message(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="owner@x.com", source="dashboard")
    assert (await _get(mock_db))["slack"]["needs_update"] is True

    assert await sync_card_messages(mock_db, time.monotonic() + 5) == 1
    assert slack.updates[-1]["ts"] == "100.2"
    assert "owner@x.com" not in _all_text(slack.updates[-1]["blocks"])  # an account id isn't a name
    assert (await _get(mock_db))["slack"]["needs_update"] is False
    assert await sync_card_messages(mock_db, time.monotonic() + 5) == 0


async def test_a_superseded_card_loses_its_buttons_in_slack(mock_db, slack, installed):
    from ai.agents.stock.cards import supersede_open
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await supersede_open(mock_db, agent["_id"], GID, "r2")
    await sync_card_messages(mock_db, time.monotonic() + 5)
    assert _action_ids(slack.updates[-1]["blocks"]) == []


async def test_sync_gives_up_on_a_message_that_is_gone(mock_db, slack, installed):
    from ai.agents.stock.cards import guarded_update
    agent = await _slack_agent(mock_db)
    card = await _posted_card(mock_db, agent, _ln(), _id="c1")
    await guarded_update(mock_db, card, {"thought_process": "x"})
    slack.fail["update"] = ["message_not_found"]
    await sync_card_messages(mock_db, time.monotonic() + 5)
    assert (await _get(mock_db))["slack"]["needs_update"] is False


async def test_sync_retries_a_blip_then_stops(mock_db, slack, installed):
    from ai.agents.stock.cards import guarded_update
    agent = await _slack_agent(mock_db)
    card = await _posted_card(mock_db, agent, _ln(), _id="c1")
    await guarded_update(mock_db, card, {"thought_process": "x"})
    slack.fail["update"] = ["fatal_error"] * 20
    for n in range(stock_slack.UPDATE_TRIES):
        assert (await _get(mock_db))["slack"]["needs_update"] is True, n
        await sync_card_messages(mock_db, time.monotonic() + 5)
    assert (await _get(mock_db))["slack"]["needs_update"] is False


async def test_a_write_during_an_update_keeps_the_card_due(mock_db, slack, installed):
    from ai.agents.stock.cards import guarded_update
    agent = await _slack_agent(mock_db)
    card = await _posted_card(mock_db, agent, _ln(), _id="c1")
    await guarded_update(mock_db, card, {"thought_process": "x"})

    real = slack.chat_update

    async def update_then_write(**kw):
        res = await real(**kw)
        fresh = await _get(mock_db)
        await guarded_update(mock_db, fresh, {"thought_process": "y"})
        return res

    slack.chat_update = update_then_write
    await update_card_message(mock_db, await _get(mock_db))
    assert (await _get(mock_db))["slack"]["needs_update"] is True


async def test_a_dead_token_on_update_marks_reconnect_and_clears_the_queue(mock_db, slack, installed):
    from ai.agents.stock.cards import guarded_update
    agent = await _slack_agent(mock_db)
    card = await _posted_card(mock_db, agent, _ln(), _id="c1")
    await guarded_update(mock_db, card, {"thought_process": "x"})
    slack.fail["update"] = ["invalid_auth"]
    await sync_card_messages(mock_db, time.monotonic() + 5)
    assert (await get_slack_bot_status(mock_db, USER))["needs_reconnect"] is True
    assert (await _get(mock_db))["slack"]["needs_update"] is False


async def test_cron_keeps_working_when_slack_steps_fail(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("slack step")

    monkeypatch.setattr(stock_slack, "sync_card_messages", broken)
    monkeypatch.setattr(stock_slack, "finish_pending_actions", broken)
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    res = await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert res["processed"] == 1


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

def _submission(view, *, team=TEAM, values=None, user=None):
    return {"type": "view_submission", "team": {"id": team}, "user": user or {"id": "U1", "name": "sam"},
            "view": {"callback_id": view["callback_id"], "private_metadata": view["private_metadata"],
                     "state": {"values": values or {}}}}


def _values(view, **changes):
    """What Slack sends for a form left as it was, with `changes` by block id."""
    out = {}
    for b in view["blocks"]:
        if b.get("type") != "input":
            continue
        el = b["element"]
        if el["type"] == "plain_text_input":
            out[b["block_id"]] = {"value": {"type": el["type"], "value": changes.get(b["block_id"], el.get("initial_value"))}}
        else:
            keep = changes.get(b["block_id"])
            options = el.get("initial_options") or []
            if keep is not None:
                options = [o for o in el["options"] if o["value"] in keep]
            out[b["block_id"]] = {"value": {"type": el["type"], "selected_options": options}}
    return out


async def test_edit_button_opens_the_form(mock_db, mock_mongo_client, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1", thought_process="Because", next_steps="Later")
    payload, action = _press("agent_card_edit", "c1")
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}
    assert slack.views[0]["trigger_id"] == "trig"
    view = slack.views[0]["view"]
    assert view["callback_id"] == "agent_card_edit" and len(view["title"]["text"]) <= 24
    assert "Because" in _all_text(view["blocks"]) and "Name ad1" in _all_text(view["blocks"])


def test_edit_view_keeps_inside_slacks_limits():
    long_name = "x" * 500
    view = edit_view({"_id": "c1", "version": 3, "client_name": "A" * 200, "status": "open",
                      "lines": [{"id": "l1", "kind": "remove", "ad_name": long_name, "state": "proposed"}],
                      "thought_process": "y" * 5000, "next_steps": "", "additional_changes": None})
    by_id = {b["block_id"]: b for b in view["blocks"] if b.get("type") == "input"}
    assert len(by_id["lines"]["element"]["options"][0]["text"]["text"]) <= 75
    assert len(by_id["thought_process"]["element"]["initial_value"]) <= 3000
    assert "initial_value" not in by_id["next_steps"]["element"]
    assert json.loads(view["private_metadata"]) == {"card_id": "c1", "version": 3}


def test_edit_view_with_no_lines_has_no_lines_field():
    view = edit_view({"_id": "c1", "lines": []})
    assert all(b.get("block_id") != "lines" for b in view["blocks"])


async def test_edit_form_saves_and_records_lesson(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(1), _ln(2, ad="ad2"), _id="c1", thought_process="Because", next_steps="Later",
                       additional_changes="", original={"lines": [{"id": "l1"}, {"id": "l2"}], "thought_process": "Because",
                                                        "next_steps": "Later", "additional_changes": ""})
    view = edit_view(await _get(mock_db))
    payload = _submission(view, values=_values(view, lines=["l1"], next_steps="Watch the budget next week"))

    assert await handle_edit_submission(payload, mock_db) == {}

    card = await _get(mock_db)
    assert [ln["id"] for ln in card["lines"]] == ["l1"] and card["next_steps"] == "Watch the budget next week"
    assert card["thought_process"] == "Because"
    lesson = await mock_db[MEMORY].find_one({"source": "edit", "source_ref": "c1"})
    assert lesson and lesson["author"] == "sam" and lesson["client_group_id"] == GID
    assert slack.updates[-1]["ts"] == "100.2" and "Watch the budget" in _all_text(slack.updates[-1]["blocks"])


async def test_edit_from_another_team_changes_nothing(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1", next_steps="Later")
    view = edit_view(await _get(mock_db))
    before = await _get(mock_db)
    for team in (OTHER_TEAM, "T_UNKNOWN"):
        payload = _submission(view, team=team, values=_values(view, next_steps="Hacked"))
        res = await handle_edit_submission(payload, mock_db)
        assert res.get("response_action") == "errors"
    assert await _get(mock_db) == before and slack.calls == []


async def test_edit_of_a_card_that_changed_is_refused(mock_db, slack, installed):
    from ai.agents.stock.cards import guarded_update
    agent = await _slack_agent(mock_db)
    card = await _posted_card(mock_db, agent, _ln(), _id="c1", next_steps="Later")
    view = edit_view(card)
    await guarded_update(mock_db, card, {"next_steps": "Someone else"})
    res = await handle_edit_submission(_submission(view, values=_values(view, next_steps="Mine")), mock_db)
    assert res["response_action"] == "errors" and "changed" in json.dumps(res["errors"])
    assert (await _get(mock_db))["next_steps"] == "Someone else"


async def test_edit_of_a_decided_card_is_refused(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1", next_steps="Later", status="applied")
    view = edit_view(await _get(mock_db))
    res = await handle_edit_submission(_submission(view, values=_values(view, next_steps="Mine")), mock_db)
    assert res["response_action"] == "errors" and (await _get(mock_db))["next_steps"] == "Later"


async def test_a_form_left_as_it_was_saves_nothing_even_with_long_text(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    long_text = "y" * 5000
    await _posted_card(mock_db, agent, _ln(), _id="c1", thought_process=long_text, next_steps="Later")
    view = edit_view(await _get(mock_db))
    assert await handle_edit_submission(_submission(view, values=_values(view)), mock_db) == {}
    card = await _get(mock_db)
    assert card["thought_process"] == long_text and not card.get("version")
    assert await mock_db[MEMORY].count_documents({}) == 0


# ---------------------------------------------------------------------------
# @Birdy
# ---------------------------------------------------------------------------

async def test_thread_mention_becomes_lesson_not_chat(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    handled = await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "<@UBOT> never pause video ads &amp; carousels", "<@U1>")
    assert handled is True

    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    assert lesson["agent_id"] == agent["_id"] and lesson["author"] == "<@U1>" and lesson["client_group_id"] == GID
    assert lesson["text"] == "never pause video ads & carousels"
    reply = slack.posts[-1]
    assert reply["thread_ts"] == "100.1" and reply["channel"] == "C1"
    assert "never pause video ads" in _all_text(reply["blocks"])
    button = next(el for b in reply["blocks"] if b["type"] == "actions" for el in b["elements"])
    assert button["action_id"] == "lesson_undo" and button["value"] == lesson["_id"]


async def test_mention_outside_card_thread_goes_to_chat(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    assert await handle_thread_mention(mock_db, TEAM, "C1", "999.9", "hello", "<@U1>") is False
    assert await handle_thread_mention(mock_db, TEAM, "C2", "100.1", "hello", "<@U1>") is False
    assert await handle_thread_mention(mock_db, TEAM, "C1", None, "hello", "<@U1>") is False
    assert slack.calls == [] and await mock_db[MEMORY].count_documents({}) == 0


async def test_mention_from_another_team_goes_to_chat(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    assert await handle_thread_mention(mock_db, OTHER_TEAM, "C1", "100.1", "hello", "<@U1>") is False
    assert await mock_db[MEMORY].count_documents({}) == 0


async def test_mention_in_a_many_card_thread_goes_to_the_client_it_names(mock_db, slack, installed):
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    await _posted_card(mock_db, agent, _ln(), _id="c1", client_name="Glow Aesthetics")
    await _posted_card(mock_db, agent, _id="c2", gid="g2", client_name="Bright Smiles")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "bright smiles hates video", "<@U1>")
    assert (await mock_db[MEMORY].find_one({"source_ref": "c2"}))["client_group_id"] == "g2"

    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "no video on Fridays", "<@U1>")
    lesson = await mock_db[MEMORY].find_one({"text": "no video on Fridays"})
    assert lesson.get("client_group_id") is None
    assert "all clients" in _all_text(slack.posts[-1]["blocks"])


async def test_mention_reply_can_not_ping(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "<!channel> stop this", "<@U1>")
    assert "<!channel>" not in _all_text(slack.posts[-1]["blocks"]) and "<!channel>" not in slack.posts[-1]["text"]


async def test_mention_lesson_uses_lesson_from_text(mock_db, slack, installed, monkeypatch):
    calls = []

    async def fake(db, agent, text, *, source, author, client_group_id=None, source_ref=None):
        calls.append(dict(text=text, source=source, author=author, client_group_id=client_group_id, source_ref=source_ref))
        return {"_id": "m1", "text": "Shaped by AI"}

    monkeypatch.setattr(lessons, "lesson_from_text", fake)
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    assert await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "stop", "<@U1>") is True
    assert calls == [dict(text="stop", source="slack_mention", author="<@U1>", client_group_id=GID, source_ref="c1")]
    assert "Shaped by AI" in _all_text(slack.posts[-1]["blocks"])


async def test_mention_whose_lesson_could_not_be_stored_says_so_and_stays_out_of_chat(mock_db, slack, installed, monkeypatch):
    async def fake(*a, **k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(lessons, "lesson_from_text", fake)
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    assert await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "stop", "<@U1>") is True
    assert "couldn't" in slack.posts[-1]["text"].lower() and _action_ids(slack.posts[-1].get("blocks")) == []


async def test_lesson_undo_overrules_it(mock_db, mock_mongo_client, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "stop", "<@U1>")
    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    assert await lessons.lessons_for_prompt(mock_db, agent["_id"], GID) == ["stop"]

    payload, action = _press("lesson_undo", lesson["_id"], ts="100.5")
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}

    stored = await mock_db[MEMORY].find_one({"_id": lesson["_id"]})
    assert stored["status"] == "overruled" and await lessons.lessons_for_prompt(mock_db, agent["_id"], GID) == []
    assert slack.updates[-1]["ts"] == "100.5" and _action_ids(slack.updates[-1].get("blocks")) == []


async def test_lesson_undo_from_another_team_is_ignored(mock_db, mock_mongo_client, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "stop", "<@U1>")
    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    calls = len(slack.calls)
    payload, action = _press("lesson_undo", lesson["_id"], team=OTHER_TEAM)
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}
    assert (await mock_db[MEMORY].find_one({"_id": lesson["_id"]}))["status"] == "active" and len(slack.calls) == calls


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

def _sign(body: bytes, ts: str) -> str:
    return "v0=" + hmac.new(SIGNING_SECRET.encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    monkeypatch.setattr("services.slack_signature.SLACK_SIGNING_SECRET", SIGNING_SECRET)


@pytest.fixture
def routes(mock_mongo_client):
    @asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    app = FastAPI()
    app.include_router(slack_interactions.router)
    app.include_router(slack_events.router)
    with patch("routers.slack_interactions.get_mongo_client", fake_client), \
            patch("routers.slack_events.get_mongo_client", fake_client):
        yield app


async def _post_interaction(app, payload: dict):
    body = f"payload={urllib.parse.quote(json.dumps(payload))}".encode()
    ts = str(int(time.time()))
    headers = {"X-Slack-Signature": _sign(body, ts), "X-Slack-Request-Timestamp": ts,
               "Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/slack/interactions", content=body, headers=headers)


async def _post_event(app, payload: dict):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {"X-Slack-Signature": _sign(body, ts), "X-Slack-Request-Timestamp": ts, "Content-Type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/slack/events", content=body, headers=headers)


async def test_route_apply_button(routes, mock_db, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    resp = await _post_interaction(routes, _press("agent_card_apply", "c1")[0])
    assert resp.status_code == 200 and meta.real_writes == [("ad1", "PAUSED")]


async def test_route_undo_finishes_after_the_response(routes, mock_db, mock_mongo_client, slack, installed, meta):
    """ASGITransport runs background tasks before returning, so here the work is done by the end of the call."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    resp = await _post_interaction(routes, _press("agent_card_undo:l1", "c1:l1")[0])
    assert resp.status_code == 200 and (await _line(mock_db))["state"] == "undone"


async def test_route_undo_answers_before_the_slow_work_starts(routes, mock_db, mock_mongo_client, slack, installed, meta):
    """The route's answer can't wait on Meta. A stuck Graph call must not hold the response."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    order = []

    async def slow_finish(row_id):
        order.append("work")

    sent = []

    async def asgi_send(message):
        if message["type"] == "http.response.start":
            sent.append(message["status"])
            order.append("response")

    body = f"payload={urllib.parse.quote(json.dumps(_press('agent_card_undo:l1', 'c1:l1')[0]))}".encode()
    ts = str(int(time.time()))
    delivered = []

    async def receive():
        if not delivered:
            delivered.append(True)
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/api/slack/interactions", "raw_path": b"/api/slack/interactions", "query_string": b"", "root_path": "",
             "headers": [(b"x-slack-signature", _sign(body, ts).encode()), (b"x-slack-request-timestamp", ts.encode()),
                         (b"content-type", b"application/x-www-form-urlencoded")], "client": ("t", 1), "server": ("t", 80)}
    with patch("routers.slack_interactions._finish_stock_action", slow_finish):
        await routes(scope, receive, asgi_send)
    assert order == ["response", "work"] and sent == [200]


async def test_route_edit_submission(routes, mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1", next_steps="Later")
    view = edit_view(await _get(mock_db))
    resp = await _post_interaction(routes, _submission(view, values=_values(view, next_steps="Sooner")))
    assert resp.status_code == 200 and (await _get(mock_db))["next_steps"] == "Sooner"


async def test_route_lesson_undo_and_open_report(routes, mock_db, slack, installed):
    resp = await _post_interaction(routes, _press("agent_open_report")[0])
    assert resp.status_code == 200 and resp.json() == {} and slack.calls == []


async def test_route_other_buttons_keep_their_old_paths(routes, mock_db, slack, installed):
    with patch("routers.slack_interactions._handle_block_actions", AsyncMock(return_value={})) as old:
        await _post_interaction(routes, _press("something_else", "x")[0])
    old.assert_awaited_once()


def _mention(thread_ts=None, text="<@UBOT> stop pausing video", team=TEAM):
    event = {"type": "app_mention", "channel": "C1", "ts": "200.1", "text": text, "user": "U1"}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "team_id": team, "event": event}


async def test_route_mention_in_a_card_thread_never_reaches_chat(routes, mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    with patch("routers.slack_events.run_chat", AsyncMock()) as chat, \
            patch("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack):
        resp = await _post_event(routes, _mention("100.1"))
    assert resp.status_code == 200
    chat.assert_not_called()
    assert (await mock_db[MEMORY].find_one({"source": "slack_mention"}))["text"] == "stop pausing video"
    assert "Thinking" not in json.dumps([p.get("text") for p in slack.posts])


async def test_route_mention_elsewhere_still_chats(routes, mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    reply = {"reply": "hi", "session_id": "s1"}
    with patch("routers.slack_events.run_chat", AsyncMock(return_value=reply)) as chat, \
            patch("routers.slack_events.get_provider_for_user", AsyncMock()), \
            patch("credits.is_blocked", AsyncMock(return_value=False)), \
            patch("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack):
        await _post_event(routes, _mention("999.9"))
        await _post_event(routes, _mention())
    assert chat.await_count == 2 and await mock_db[MEMORY].count_documents({}) == 0


async def test_route_a_lookup_failure_falls_back_to_chat(routes, mock_db, slack, installed):
    reply = {"reply": "hi", "session_id": "s1"}
    with patch("routers.slack_events.run_chat", AsyncMock(return_value=reply)) as chat, \
            patch("routers.slack_events.get_provider_for_user", AsyncMock()), \
            patch("credits.is_blocked", AsyncMock(return_value=False)), \
            patch("routers.slack_events.handle_thread_mention", AsyncMock(side_effect=RuntimeError("blip"))), \
            patch("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack):
        resp = await _post_event(routes, _mention("100.1"))
    assert resp.status_code == 200
    chat.assert_awaited_once()
