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
from ai.agents.stock import apply as apply_mod
from ai.agents.stock import lessons, runner
from ai.agents.stock import slack as stock_slack
from ai.agents.stock.apply import apply_card, process_queued_lines
from ai.agents.stock.slack import (edit_view, finish_action, handle_card_action, handle_edit_submission,
                                   handle_thread_mention, post_urgent_alerts, publish_run, retry_unposted_runs,
                                   sync_card_messages, update_card_message)
from ai.agents.store import AGENTS, CARDS, MEMORY, RUNS
from ai.provider_factory import NoAiCredentialError
from routers import slack_events, slack_interactions
from services.slack_bot_service import get_slack_bot_status, mark_needs_reconnect, save_slack_bot_installation
from test_agent_learning import FakeProvider, use_provider
from test_stock_apply import GID, Killed, _ads, _agent, _card, _get, _line, _ln, _run, meta  # noqa: F401 - meta is a fixture
from test_stock_runner import USER

TEAM, OTHER_TEAM, OTHER_USER = "T1", "T2", "other@x.com"
NOW = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
SIGNING_SECRET = "test-stock-slack-secret"
SLACK = {"channel_id": "C1", "channel_name": "ads", "also_tag_user_id": "U777", "templates": {}}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    return await _card(db, agent, *lines, **{"slack": {"channel_id": "C1", "ts": "100.2", "thread_ts": "100.1"}, **extra})


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


async def _no_auto(*a, **k):
    return None


async def test_post_failure_keeps_report_and_retries_next_tick_once(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    """The run ends with the report and the error showing. Each message then reaches Slack once, using the stored ts."""
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    await _card(mock_db, agent, _ln(), _id="c1", run_id=run["_id"], slack={"channel_id": None, "ts": None})
    slack.fail["post"] = [None, "fatal_error"]  # the summary goes out, its card doesn't

    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    assert run["phase"] == "done" and "fatal_error" in run["publish_error"] and run["attempts"] == 0
    assert await _get(mock_db, "c1") and len(slack.posts) == 1

    assert await retry_unposted_runs(mock_db, NOW + timedelta(minutes=30), time.monotonic() + 30) == 1
    run = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert not run["publish_error"] and run["slack_pending"] is False and run["attempts"] == 0
    assert len(slack.posts) == 2 and slack.posts[1]["thread_ts"] == "100.1"
    assert [u["ts"] for u in slack.updates] == ["100.1"]   # the summary, updated and not posted again
    assert run["slack"]["ts"] == "100.1"

    assert await retry_unposted_runs(mock_db, NOW + timedelta(hours=2), time.monotonic() + 30) == 0
    assert len(slack.posts) == 2


async def _with_auto_applied(db, client, meta, agent):
    """A run whose publish is about to fail, with Auto's change already live on its card."""
    await _ads(db, "ad1", "spare")
    run = await _stored_run(db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    await _card(db, agent, _ln(mode="auto"), _id="c1", run_id=run["_id"], slack={"channel_id": None, "ts": None})
    return run


async def test_a_four_minute_outage_reaches_slack_late_with_autos_change(mock_db, mock_mongo_client, slack, installed, meta):
    settings = {"modes": {"remove": "auto", "revive": "ask", "add": "ask"}, "slack": SLACK}
    agent = await _agent(mock_db, settings=settings)
    run = await _with_auto_applied(mock_db, mock_mongo_client, meta, agent)
    slack.fail["post"] = ["fatal_error"] * 3

    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)

    assert run["phase"] == "done" and run["attempts"] == 0 and "fatal_error" in run["publish_error"]
    assert meta.real_writes == [("ad1", "PAUSED")] and slack.posts == []
    assert await retry_unposted_runs(mock_db, NOW + timedelta(minutes=4), time.monotonic() + 30) == 0
    assert slack.posts == []  # slowly: the next try isn't due yet

    slack.fail["post"] = []
    assert await retry_unposted_runs(mock_db, NOW + timedelta(minutes=15), time.monotonic() + 30) == 1

    assert len(slack.posts) == 2
    assert "late" in _all_text(slack.posts[0]["blocks"]).lower()
    assert "applied" in _all_text(slack.posts[1]["blocks"]) and "agent_card_undo:l1" in _action_ids(slack.posts[1]["blocks"])
    stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert not stored["publish_error"] and stored["attempts"] == 0 and stored["phase"] == "done"


async def test_a_revoked_token_waits_for_the_reconnect_then_posts(mock_db, mock_mongo_client, slack, installed, meta):
    settings = {"modes": {"remove": "auto", "revive": "ask", "add": "ask"}, "slack": SLACK}
    agent = await _agent(mock_db, settings=settings)
    run = await _with_auto_applied(mock_db, mock_mongo_client, meta, agent)
    slack.fail["post"] = ["token_revoked"]

    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)

    assert run["phase"] == "done" and "reconnect" in run["publish_error"].lower() and run["attempts"] == 0
    assert meta.real_writes == [("ad1", "PAUSED")]
    assert (await get_slack_bot_status(mock_db, USER))["needs_reconnect"] is True
    calls = len(slack.calls)
    await retry_unposted_runs(mock_db, NOW + timedelta(hours=1), time.monotonic() + 30)
    assert len(slack.calls) == calls and (await mock_db[RUNS].find_one({"_id": run["_id"]}))["slack_pending"] is True

    await save_slack_bot_installation(mock_db, USER, TEAM, "xoxb-new", "UBOT")
    assert await retry_unposted_runs(mock_db, NOW + timedelta(hours=1, minutes=15), time.monotonic() + 30) == 1

    assert len(slack.posts) == 2 and "late" in _all_text(slack.posts[0]["blocks"]).lower()
    assert "applied" in _all_text(slack.posts[1]["blocks"])
    stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert stored["slack_pending"] is False and not stored["publish_error"] and stored["attempts"] == 0


async def test_posting_stops_after_24_hours_with_the_error_kept(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    slack.fail["post"] = ["fatal_error"] * 99
    await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    tries = 0
    for hour in range(1, 30):
        await retry_unposted_runs(mock_db, NOW + timedelta(hours=hour), time.monotonic() + 30)
        tries = len(slack.calls)
        if hour == 24:
            at_24 = tries
    assert 1 < at_24 < 20 and tries == at_24 + 1 or tries == at_24  # slowly, and then not at all
    stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert stored["slack_pending"] is False and "fatal_error" in stored["publish_error"] and stored["attempts"] == 0


async def test_a_kill_while_publishing_is_still_retried(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    async def killed(*a, **k):
        raise Killed()

    monkeypatch.setattr(runner, "publish_run", killed)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    with pytest.raises(Killed):
        await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    # A kill during the last publish of a run that's done: nobody resumes it, so the retry takes the stale claim.
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"phase": "done", "claimed_at": _utcnow() - timedelta(hours=1)}})
    assert await retry_unposted_runs(mock_db, NOW + timedelta(minutes=30), time.monotonic() + 30) == 1
    assert len(slack.posts) == 1


def test_the_report_gets_the_error_and_not_the_claim():
    from routers.agent_stock import _run_to_api
    out = _run_to_api({"_id": "r1", "publish_error": "Slack refused", "slack_pending": True, "slack_late": True,
                       "claim_kind": "slack", "claimed_at": NOW, "clients": {}, "slack": {"ts": None}})
    assert out["publish_error"] == "Slack refused" and out["slack_pending"] and out["slack_late"]
    assert "claim_kind" not in out and "claimed_at" not in out


async def test_a_preview_is_never_retried_into_slack(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _stored_run(mock_db, agent, kind="preview", slack_pending=True, slack_retry_at=NOW.replace(tzinfo=None),
                      slack_retry_until=NOW.replace(tzinfo=None) + timedelta(days=9))
    assert await retry_unposted_runs(mock_db, NOW + timedelta(hours=1), time.monotonic() + 30) == 0
    assert slack.calls == []


async def test_a_preview_run_doesnt_become_pending(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, kind="preview", phase="publishing", slack={"channel_id": None, "ts": None})
    run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    assert not run.get("slack_pending") and slack.calls == []


async def test_the_retry_never_runs_while_a_tick_holds_the_run(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="waiting", slack_pending=True, slack_retry_at=NOW.replace(tzinfo=None),
                            slack_retry_until=NOW.replace(tzinfo=None) + timedelta(days=1), claimed_at=_utcnow())
    assert await retry_unposted_runs(mock_db, NOW + timedelta(hours=1), time.monotonic() + 30) == 0
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": None}})
    assert await retry_unposted_runs(mock_db, NOW + timedelta(hours=1), time.monotonic() + 30) == 1
    assert len(slack.posts) == 1


async def test_a_killed_retry_costs_the_run_no_attempt(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="waiting", slack_pending=True, slack_retry_at=NOW.replace(tzinfo=None),
                            slack_retry_until=NOW.replace(tzinfo=None) + timedelta(days=1), attempts=0,
                            clients={GID: {"state": "done", "health_done": True}})

    async def killed(*a, **k):
        raise Killed()

    monkeypatch.setattr(stock_slack, "publish_run", killed)
    with pytest.raises(Killed):
        await retry_unposted_runs(mock_db, NOW + timedelta(hours=1), time.monotonic() + 30)
    monkeypatch.setattr(stock_slack, "publish_run", publish_run)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": _utcnow() - timedelta(hours=1)}})
    claimed = await runner._claim(mock_db, run["_id"])
    assert claimed["taken_over"] is False and not claimed.get("claim_kind")


async def test_the_channel_errors_arent_retried(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db)
    for code in ("channel_not_found", "not_in_channel", "is_archived"):
        run = await _stored_run(mock_db, agent, _id=code, run_id=code, phase="publishing", slack={"channel_id": None, "ts": None})
        slack.fail["post"] = [code]
        run = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
        assert run["phase"] == "done" and "invite" in run["publish_error"].lower() and not run["slack_pending"]
    calls = len(slack.calls)
    assert await retry_unposted_runs(mock_db, NOW + timedelta(hours=2), time.monotonic() + 30) == 0
    assert len(slack.calls) == calls


async def test_a_failed_summary_update_doesnt_stop_new_cards(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, slack={"channel_id": "C1", "ts": "100.1"})
    await _card(mock_db, agent, _ln(), _id="c1")
    slack.fail["update"] = ["fatal_error"]
    with pytest.raises(stock_slack.SlackPublishError):
        await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert len(slack.posts) == 1 and slack.posts[0]["thread_ts"] == "100.1"
    assert (await _get(mock_db))["slack"]["ts"]


async def test_a_deleted_summary_gets_a_new_one(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, slack={"channel_id": "C1", "ts": "100.0"})
    await _card(mock_db, agent, _ln(), _id="c1")
    slack.fail["update"] = ["message_not_found"]
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert len(slack.posts) == 2 and "thread_ts" not in slack.posts[0] and slack.posts[1]["thread_ts"] == "100.1"
    assert (await mock_db[RUNS].find_one({"_id": run["_id"]}))["slack"]["ts"] == "100.1"


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
    await _card(mock_db, agent, _id="c2", run_id="r2", created=_utcnow(), slack={"channel_id": "C1", "ts": "9.9"})
    await publish_run(mock_db, run, agent, [await _get(mock_db, "c1")])
    assert "agent_card_apply" not in _action_ids(slack.posts[1]["blocks"])


async def test_a_retry_that_gets_further_doesnt_slow_down(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    await _card(mock_db, agent, _ln(), _id="c1", slack={"channel_id": None, "ts": None})
    await _card(mock_db, agent, _ln(), _id="c2", gid="g2", slack={"channel_id": None, "ts": None})
    slack.fail["post"] = ["fatal_error"]
    await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    first = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert first["slack_retry_n"] == 1

    slack.fail["post"] = [None, None, "fatal_error"]   # the summary and one card get out
    await retry_unposted_runs(mock_db, NOW + timedelta(minutes=15), time.monotonic() + 30)
    second = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert second["slack_retry_n"] == 1 and second["slack_pending"] is True


async def test_runs_waiting_for_a_reconnect_cant_starve_the_batch(mock_db, slack, installed, monkeypatch):
    monkeypatch.setattr(stock_slack, "RETRY_BATCH", 2)
    agent = await _slack_agent(mock_db)
    long_ago = NOW.replace(tzinfo=None) - timedelta(days=3)
    for n in range(3):
        await _stored_run(mock_db, agent, _id=f"w{n}", run_id=f"w{n}", phase="done", slack_pending=True, slack_wait_reconnect=True,
                          slack_retry_at=long_ago, slack_retry_until=NOW.replace(tzinfo=None) + timedelta(days=9))
    await mark_needs_reconnect(mock_db, USER, "token_revoked")
    await save_slack_bot_installation(mock_db, OTHER_USER, OTHER_TEAM, "xoxb-2", "UBOT")
    other = {**agent, "_id": "a2", "user_id": OTHER_USER}
    await mock_db[AGENTS].insert_one(other)
    await mock_db[RUNS].insert_one(_run(other, _id="due", run_id="due", user_id=OTHER_USER, summary={"clients": 1}, phase="done", slack_pending=True,
                                        slack_retry_at=NOW.replace(tzinfo=None), slack_retry_until=NOW.replace(tzinfo=None) + timedelta(days=1)))
    posted = 0
    for minutes in (15, 30, 45):
        posted += await retry_unposted_runs(mock_db, NOW + timedelta(minutes=minutes), time.monotonic() + 30)
    assert posted == 1 and (await mock_db[RUNS].find_one({"_id": "due"}))["slack_pending"] is False


async def test_a_reconnect_past_24_hours_survives_a_passing_error(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    slack.fail["post"] = ["token_revoked"]
    await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    await save_slack_bot_installation(mock_db, USER, TEAM, "xoxb-new", "UBOT")

    slack.fail["post"] = ["fatal_error"]
    await retry_unposted_runs(mock_db, NOW + timedelta(hours=30), time.monotonic() + 30)
    assert (await mock_db[RUNS].find_one({"_id": run["_id"]}))["slack_pending"] is True

    assert await retry_unposted_runs(mock_db, NOW + timedelta(hours=31), time.monotonic() + 30) == 1
    assert len(slack.posts) == 1


async def test_the_retry_posts_with_the_agent_off(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None})
    slack.fail["post"] = ["fatal_error"]
    await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    assert await retry_unposted_runs(mock_db, NOW + timedelta(minutes=15), time.monotonic() + 30) == 1


async def test_a_dead_token_past_24_hours_stops_only_once_a_newer_report_exists(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    monkeypatch.setattr(runner, "auto_apply_run", _no_auto)
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent, phase="publishing", slack={"channel_id": None, "ts": None}, as_of=NOW.replace(tzinfo=None))
    slack.fail["post"] = ["token_revoked"]
    await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    await retry_unposted_runs(mock_db, NOW + timedelta(hours=30), time.monotonic() + 30)
    assert (await mock_db[RUNS].find_one({"_id": run["_id"]}))["slack_pending"] is True
    await mock_db[RUNS].insert_one(_run(agent, _id="r2", as_of=run["as_of"] + timedelta(days=1)))
    await retry_unposted_runs(mock_db, NOW + timedelta(hours=31), time.monotonic() + 30)
    stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert stored["slack_pending"] is False and "reconnect" in stored["publish_error"].lower()


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

    await rows.update_one({"_id": deferred[0]}, {"$set": {"claimed_at": _utcnow()}})
    assert await finish_action(mock_db, mock_mongo_client, deferred[0]) is False  # still alive, as far as anyone knows
    assert (await _line(mock_db))["state"] == "applied"

    await rows.update_one({"_id": deferred[0]}, {"$set": {"claimed_at": _utcnow() - timedelta(hours=1)}})
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
    assert "didn't happen" in slack.ephemeral[-1]["text"]


async def test_undo_refused_tells_the_person_and_shows_it(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(state="applied", applied_at=_utcnow() - timedelta(days=3),
                                           undo_until=_utcnow() - timedelta(days=2), wrote=True), _id="c1")
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
# Apply all shows only what was shown
# ---------------------------------------------------------------------------

async def test_apply_all_leaves_a_card_nobody_was_shown_and_says_how_many(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    await _ads(mock_db, "ad1", "spare")
    await _ads(mock_db, "ad2", "spare2", gid="g2")
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}, summary={"clients": 2}))
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await _card(mock_db, agent, _ln(ad="ad2"), _id="c2", gid="g2", slack={"channel_id": None, "ts": None})

    payload, action = _press("agent_run_apply_all", "r1", ts="100.1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)

    assert (await _line(mock_db, "l1", "c1"))["state"] == "queued"
    assert (await _line(mock_db, "l1", "c2"))["state"] == "proposed"
    assert "1 card" in slack.ephemeral[-1]["text"] and "report" in slack.ephemeral[-1]["text"]
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert meta.real_writes == [("ad1", "PAUSED")] and (await _line(mock_db, "l1", "c2"))["state"] == "proposed"


async def test_apply_all_cron_expansion_keeps_to_what_was_showing(mock_db, mock_mongo_client, slack, installed, meta):
    """A press that ran out of budget is finished by the cron, against the press's time. A card posted after it wasn't showing."""
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    await _ads(mock_db, "ad1", "spare")
    await _ads(mock_db, "ad2", "spare2", gid="g2")
    at = _utcnow()
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}, apply_all={
        "actor": "user", "by": "sam", "source": "slack", "at": at, "pending": True}))
    await _posted_card(mock_db, agent, _ln(), _id="c1", slack={"channel_id": "C1", "ts": "100.2", "posted_at": at - timedelta(minutes=1)})
    await _posted_card(mock_db, agent, _ln(ad="ad2"), _id="c2", gid="g2", slack={"channel_id": "C1", "ts": "100.3", "posted_at": at + timedelta(minutes=1)})
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert meta.real_writes == [("ad1", "PAUSED")]


async def test_apply_all_from_the_dashboard_still_applies_unposted_cards(mock_db, mock_mongo_client, meta):
    from ai.agents.stock.apply import apply_run
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await mock_db[RUNS].insert_one(_run(agent))
    await _card(mock_db, agent, _ln(), _id="c1", slack={"channel_id": None, "ts": None})
    res = await apply_run(mock_db, mock_mongo_client, USER, "r1", by="me", source="dashboard", budget_s=20)
    assert res["outcome"] == "applied" and res["not_in_slack"] == 0


async def test_a_posted_card_records_when_it_was_posted(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    run = await _stored_run(mock_db, agent)
    await _card(mock_db, agent, _ln(), _id="c1")
    await publish_run(mock_db, run, agent, await mock_db[CARDS].find({}).to_list(None))
    assert (await _get(mock_db))["slack"]["posted_at"]


# ---------------------------------------------------------------------------
# A stored press: frozen, late, busy and cut off
# ---------------------------------------------------------------------------

async def _undo_press(db, client, agent, meta):
    await _ads(db, "ad1", "spare")
    await _posted_card(db, agent, _ln(), _id="c1")
    await apply_card(db, client, USER, "c1", by="sam", source="slack")
    deferred = []
    payload, action = _press("agent_card_undo:l1", "c1:l1")
    await handle_card_action(payload, action, db, client, defer=deferred.append)
    return deferred[0]


async def _age_claim(db, row_id, seconds):
    await db[stock_slack.SLACK_ACTIONS].update_one({"_id": row_id}, {"$set": {"claimed_at": _utcnow() - timedelta(seconds=seconds)}})


async def _age_undo(db, seconds):
    card = await _get(db)
    old = _utcnow() - timedelta(seconds=seconds)
    lines = [{**ln, "undoing_at": old, **({"undo_write_started_at": old} if ln.get("undo_write_started_at") else {})} for ln in card["lines"]]
    await db[CARDS].update_one({"_id": card["_id"]}, {"$set": {"lines": lines}})


async def test_a_press_is_taken_over_at_about_two_minutes(mock_db, mock_mongo_client, slack, installed, meta):
    assert timedelta(seconds=60) <= stock_slack.ACTION_RECLAIM <= timedelta(seconds=120)
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    await stock_slack._claim_action(mock_db, row)      # the deferred task claimed it, then froze
    await _age_claim(mock_db, row, 60)
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert (await _line(mock_db))["state"] == "applied"
    await _age_claim(mock_db, row, 130)
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert (await _line(mock_db))["state"] == "undone" and meta.real_writes[-1] == ("ad1", "ACTIVE")


async def test_a_frozen_undo_mid_line_is_recovered_in_about_two_minutes(mock_db, mock_mongo_client, slack, installed, meta):
    """It froze after moving the line to undoing, before anything was sent."""
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    await stock_slack._claim_action(mock_db, row)
    card = await _get(mock_db)
    lines = [{**card["lines"][0], "state": "undoing", "undoing_at": _utcnow() - timedelta(seconds=130), "undo_token": "frozen"}]
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines": lines}})
    await _age_claim(mock_db, row, 130)
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert (await _line(mock_db))["state"] == "undone" and meta.real_writes[-1] == ("ad1", "ACTIVE")


async def _frozen_holder_after(monkeypatch, when, on_thaw):
    """Runs `on_thaw` (what the world did while this invocation was frozen) the first time `when(kw)` is true."""
    real, fired = apply_mod._set_line, []

    async def set_line(db, card_id, line_id, **kw):
        out = await real(db, card_id, line_id, **kw)
        if when(kw) and not fired:
            fired.append(True)
            await on_thaw()
        return out

    monkeypatch.setattr(apply_mod, "_set_line", set_line)


async def test_a_late_thaw_cant_send_after_recovery_recorded_a_failure(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """Frozen right after its marker. Recovery reads Meta, finds nothing landed and says so. The thaw must not send."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    writes = list(meta.real_writes)

    async def world():
        await _age_undo(mock_db, 130)
        card = await _get(mock_db)
        await apply_mod.recover_stale_undo(mock_db, mock_mongo_client, "c1", "l1", older_than=stock_slack.ACTION_RECLAIM)
        assert (await _line(mock_db))["state"] == "applied" and (await _line(mock_db))["undo_error"], card

    await _frozen_holder_after(monkeypatch, lambda kw: kw.get("undo_write_started_at"), world)
    res = await apply_mod.undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="slack")

    assert meta.real_writes == writes and res["outcome"] == "busy"
    assert (await _line(mock_db))["state"] == "applied" and (await _line(mock_db))["undo_error"]


async def test_a_thaw_past_its_send_window_doesnt_send(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """Frozen after its marker with nobody having looked yet. It stands down, and as it knows nothing went out, says so."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    writes = list(meta.real_writes)

    async def world():
        await _age_undo(mock_db, 130)

    await _frozen_holder_after(monkeypatch, lambda kw: kw.get("undo_write_started_at"), world)
    res = await apply_mod.undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="slack")
    assert meta.real_writes == writes and res["outcome"] == "busy"
    assert (await _line(mock_db))["state"] == "applied"


async def test_a_thaw_before_its_marker_cant_take_a_newer_undo(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """Frozen before its marker. Recovery put the line back, a new undo owns it now. Only that one sends."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    real_state, fired = meta.state, []

    async def state(user_id, object_id, object_type, client):
        if not fired and (await _line(mock_db))["state"] == "undoing":
            fired.append(True)
            card = await _get(mock_db)
            lines = [{**card["lines"][0], "undo_token": "newer"}]
            await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines": lines}})
        return await real_state(user_id, object_id, object_type, client)

    monkeypatch.setattr(apply_mod, "get_object_state", state)
    writes = list(meta.real_writes)
    res = await apply_mod.undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="slack")
    assert meta.real_writes == writes and res["outcome"] == "busy"
    assert (await _line(mock_db))["undo_token"] == "newer" and (await _line(mock_db))["state"] == "undoing"


async def test_a_thawed_holder_cant_delete_the_row_of_the_one_that_took_over(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    rows = mock_db[stock_slack.SLACK_ACTIONS]

    async def taken_over(*a, **k):
        await rows.update_one({"_id": row}, {"$set": {"claim": "someone-else"}})
        return {"ok": True, "outcome": "undone"}

    monkeypatch.setattr(stock_slack, "undo_line", taken_over)
    await finish_action(mock_db, mock_mongo_client, row)
    assert await rows.count_documents({}) == 1 and slack.ephemeral[-1]["text"].startswith("Undoing")


async def test_a_thaw_before_the_undo_claim_doesnt_send_after_the_failure_was_told(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """Frozen right after claiming its row. The takeover's read fails, the person is told and the row is gone. Then the thaw."""
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    rows = mock_db[stock_slack.SLACK_ACTIONS]
    real, fired, seen = stock_slack.recover_stale_undo, [], {}
    good = apply_mod.get_object_state

    async def frozen(*a, **k):
        if not fired:
            fired.append(1)
            await rows.update_one({"_id": row}, {"$set": {"claimed_at": _utcnow() - timedelta(seconds=130)}})

            async def boom(*a, **k):
                raise RuntimeError("graph down")

            monkeypatch.setattr(apply_mod, "get_object_state", boom)
            await finish_action(mock_db, mock_mongo_client, row)
            monkeypatch.setattr(apply_mod, "get_object_state", good)
            line = await _line(mock_db)
            seen.update(left=await rows.count_documents({}), state=line["state"], writes=len(meta.real_writes))
        return await real(*a, **k)

    monkeypatch.setattr(stock_slack, "recover_stale_undo", frozen)
    await finish_action(mock_db, mock_mongo_client, row)

    assert seen == {"left": 0, "state": "applied", "writes": seen["writes"]}
    assert len(meta.real_writes) == seen["writes"] and (await _line(mock_db))["state"] == "applied"


async def test_a_thaw_whose_row_was_taken_over_stops_before_the_line_is_claimed(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    rows = mock_db[stock_slack.SLACK_ACTIONS]
    real = stock_slack.undo_line
    writes = len(meta.real_writes)

    async def after_takeover(*a, **k):
        await rows.update_one({"_id": row}, {"$set": {"claim": "the-takeover"}})
        return await real(*a, **k)

    monkeypatch.setattr(stock_slack, "undo_line", after_takeover)
    await finish_action(mock_db, mock_mongo_client, row)
    assert len(meta.real_writes) == writes and (await _line(mock_db))["state"] == "applied"
    assert (await rows.find_one({"_id": row}))["claim"] == "the-takeover"


async def test_a_freeze_between_the_row_check_and_the_token_still_cant_send(mock_db, mock_mongo_client, slack, installed, meta):
    """The send window starts at the row check, so however long the gap was, the fence reads it."""
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    writes = len(meta.real_writes)

    async def checked_long_ago():
        return _utcnow() - timedelta(seconds=130)

    res = await apply_mod.undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="slack", press_gate=checked_long_ago)
    assert len(meta.real_writes) == writes and res["outcome"] == "busy" and (await _line(mock_db))["state"] == "applied"


async def test_a_hung_undo_send_is_ended_by_its_overall_limit(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """httpx's 30 s is per phase. The whole send has a limit, so it's over before a takeover reads Meta."""
    assert apply_mod.UNDO_SEND_WINDOW.total_seconds() + apply_mod.UNDO_SEND_TIMEOUT_S < stock_slack.ACTION_RECLAIM.total_seconds()
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    monkeypatch.setattr(apply_mod, "UNDO_SEND_TIMEOUT_S", 0.05)
    meta.write_delay = 30
    started = time.monotonic()
    res = await apply_mod.undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="slack")
    assert time.monotonic() - started < 5 and res["outcome"] == "queued"
    assert (await _line(mock_db))["state"] == "undoing" and (await _line(mock_db))["undo_write_started_at"]


async def test_a_refusal_note_cut_off_is_still_sent_later(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(state="applied", applied_at=_utcnow() - timedelta(days=3),
                                           undo_until=_utcnow() - timedelta(days=2), wrote=True), _id="c1")
    deferred = []
    payload, action = _press("agent_card_undo:l1", "c1:l1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    good, told = slack.chat_postEphemeral, len(slack.ephemeral)

    async def hangs(**kw):
        await asyncio.sleep(30)

    slack.chat_postEphemeral = hangs
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(finish_action(mock_db, mock_mongo_client, deferred[0]), 0.05)
    assert (await mock_db[stock_slack.SLACK_ACTIONS].find_one({"_id": deferred[0]}))["claimed_at"] is None

    slack.chat_postEphemeral = good
    await finish_action(mock_db, mock_mongo_client, deferred[0])
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    assert len(slack.ephemeral) == told + 1 and "24 hours" in slack.ephemeral[-1]["text"]


async def test_a_failed_undo_cut_before_its_delete_doesnt_run_again(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """At the real cap. Meta's read fails at 4.6 s and the person is told nothing changed. Slack is slow to reply, so the
    cap cuts before the row goes. Next tick Meta is fine, and the Undo mustn't go out."""
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    good = apply_mod.get_object_state

    async def slow_fail(*a, **k):
        await asyncio.sleep(4.6)
        raise RuntimeError("graph slow, then down")

    monkeypatch.setattr(apply_mod, "get_object_state", slow_fail)
    real_post = slack.chat_postEphemeral

    async def post_then_hang(**kw):
        await real_post(**kw)
        await asyncio.sleep(0.8)

    slack.chat_postEphemeral = post_then_hang
    writes = len(meta.real_writes)
    await stock_slack.finish_pending_actions(mock_db, mock_mongo_client, time.monotonic() + 10)
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({"_id": row}) == 1

    slack.chat_postEphemeral = real_post
    monkeypatch.setattr(apply_mod, "get_object_state", good)
    await stock_slack.finish_pending_actions(mock_db, mock_mongo_client, time.monotonic() + 10)

    assert len(meta.real_writes) == writes and (await _line(mock_db))["state"] == "applied"
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    told = [e["text"] for e in slack.ephemeral if "nothing was changed" in e["text"]]
    assert 1 <= len(told) <= 2


async def test_a_kill_after_the_answer_is_stored_only_tells_again(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    good = apply_mod.get_object_state

    async def down(*a, **k):
        raise RuntimeError("graph down")

    monkeypatch.setattr(apply_mod, "get_object_state", down)
    real_tell = stock_slack._tell_or_note

    async def killed(*a, **k):
        await real_tell(*a, **k)
        raise Killed()

    monkeypatch.setattr(stock_slack, "_tell_or_note", killed)
    writes = len(meta.real_writes)
    with pytest.raises(Killed):
        await finish_action(mock_db, mock_mongo_client, row)

    monkeypatch.setattr(stock_slack, "_tell_or_note", real_tell)
    monkeypatch.setattr(apply_mod, "get_object_state", good)
    await _age_claim(mock_db, row, 130)
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)

    assert len(meta.real_writes) == writes and (await _line(mock_db))["state"] == "applied"
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    assert len([e for e in slack.ephemeral if "nothing was changed" in e["text"]]) == 2


async def test_a_good_undo_cut_before_its_delete_isnt_run_again(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    real_update = stock_slack.update_card_message

    async def hangs(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(stock_slack, "update_card_message", hangs)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(finish_action(mock_db, mock_mongo_client, row), 0.2)
    assert (await _line(mock_db))["state"] == "undone"

    monkeypatch.setattr(stock_slack, "update_card_message", real_update)
    calls = []
    real_undo = stock_slack.undo_line

    async def undo(*a, **k):
        calls.append(1)
        return await real_undo(*a, **k)

    monkeypatch.setattr(stock_slack, "undo_line", undo)
    await finish_action(mock_db, mock_mongo_client, row)
    assert calls == [] and await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0


async def test_giving_up_is_an_answer_too(mock_db, mock_mongo_client, slack, installed, meta):
    """Busy at the limit, told it didn't happen, then cut. With the try refunded, it must not run after all."""
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(state="applying"), _id="c1")
    deferred = []
    payload, action = _press("agent_card_dismiss", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    rows = mock_db[stock_slack.SLACK_ACTIONS]
    await rows.update_one({"_id": deferred[0]}, {"$set": {"tries": stock_slack.ACTION_TRIES - 1}})
    good = slack.chat_postEphemeral

    async def hangs(**kw):
        await asyncio.sleep(30)

    slack.chat_postEphemeral = hangs
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(finish_action(mock_db, mock_mongo_client, deferred[0]), 0.1)

    slack.chat_postEphemeral = good
    lines = [{**(await _get(mock_db))["lines"][0], "state": "proposed"}]
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines": lines}})
    await finish_action(mock_db, mock_mongo_client, deferred[0])

    assert (await _get(mock_db))["status"] == "open" and await rows.count_documents({}) == 0
    assert "didn't happen" in slack.ephemeral[-1]["text"]


async def test_a_press_note_clears_when_the_dashboard_retry_works(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    await mock_db[stock_slack.SLACK_ACTIONS].update_one({"_id": row}, {"$set": {"created_at": _utcnow() - timedelta(hours=1)}})
    await finish_action(mock_db, mock_mongo_client, row)
    assert "didn't happen" in (await _get(mock_db))["press_note"]

    await apply_mod.undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="me@x.com", source="dashboard")

    card = await _get(mock_db)
    assert card["lines"][0]["state"] == "undone" and not card.get("press_note")
    await update_card_message(mock_db, card)
    assert "didn't happen" not in _all_text(slack.updates[-1]["blocks"])


async def test_a_press_note_clears_when_the_line_settles_by_itself_or_the_card_is_replaced(mock_db, mock_mongo_client, slack, installed, meta):
    from ai.agents.stock.cards import supersede_open
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1", press_note="Birdy couldn't do that")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="me", source="dashboard")
    assert not (await _get(mock_db))["press_note"]

    await _posted_card(mock_db, agent, _ln(), _id="c2", press_note="Old news", run_id="r0")
    await supersede_open(mock_db, agent["_id"], GID, "c9")
    assert not (await _get(mock_db, "c2"))["press_note"]


async def test_a_text_edit_keeps_the_press_note(mock_db, slack, installed):
    from ai.agents.stock.cards import guarded_update
    agent = await _slack_agent(mock_db)
    card = await _posted_card(mock_db, agent, _ln(), _id="c1", press_note="Birdy couldn't do that")
    await guarded_update(mock_db, card, {"thought_process": "changed"})
    assert (await _get(mock_db))["press_note"]


async def _cold_start(monkeypatch):
    """The function's first Mongo call takes most of Slack's 3 s, and every later one an ordinary round trip."""
    real_owner, real_visible = stock_slack.get_user_id_by_team_id, apply_mod._visible_card

    async def slow_owner(*a, **k):
        await asyncio.sleep(1.7)
        return await real_owner(*a, **k)

    async def visible(*a, **k):
        await asyncio.sleep(0.03)
        return await real_visible(*a, **k)

    monkeypatch.setattr(stock_slack, "get_user_id_by_team_id", slow_owner)
    monkeypatch.setattr(apply_mod, "_visible_card", visible)


async def test_a_slow_first_mongo_call_cant_lose_an_apply(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await _cold_start(monkeypatch)
    payload, action = _press("agent_card_apply", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] in ("queued", "applied")

    for _ in range(2):
        await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert meta.real_writes == [("ad1", "PAUSED")] and (await _line(mock_db))["state"] == "applied"


async def test_slow_reads_come_out_of_the_time_for_the_line_not_the_answer(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    real_owner, real_visible = stock_slack.get_user_id_by_team_id, apply_mod._visible_card

    async def slow_owner(*a, **k):
        await asyncio.sleep(0.7)
        return await real_owner(*a, **k)

    async def slow_visible(*a, **k):
        await asyncio.sleep(1.0)
        return await real_visible(*a, **k)

    monkeypatch.setattr(stock_slack, "get_user_id_by_team_id", slow_owner)
    monkeypatch.setattr(apply_mod, "_visible_card", slow_visible)
    meta.write_delay = 30
    payload, action = _press("agent_card_apply", "c1")
    started = time.monotonic()
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert time.monotonic() - started < 2.8 and (await _line(mock_db))["state"] != "proposed"


async def test_a_slow_first_mongo_call_cant_lose_apply_all(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}))
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    real_owner = stock_slack.get_user_id_by_team_id

    async def slow_owner(*a, **k):
        await asyncio.sleep(2.0)
        return await real_owner(*a, **k)

    monkeypatch.setattr(stock_slack, "get_user_id_by_team_id", slow_owner)
    real_run = stock_slack.apply_run

    async def slow_run(*a, **k):
        await asyncio.sleep(0.03)
        return await real_run(*a, **k)

    monkeypatch.setattr(stock_slack, "apply_run", slow_run)
    payload, action = _press("agent_run_apply_all", "r1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert (await mock_db[RUNS].find_one({"_id": "r1"})).get("apply_all")

    for _ in range(2):
        await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=time.monotonic() + 30)
    assert meta.real_writes == [("ad1", "PAUSED")]


async def test_a_busy_dismiss_is_kept_and_retried(mock_db, mock_mongo_client, slack, installed, meta):
    """Dismiss creates no state of its own, so its busy means another line is going out. It must not vanish."""
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(state="applying"), _id="c1")
    deferred = []
    payload, action = _press("agent_card_dismiss", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    rows = mock_db[stock_slack.SLACK_ACTIONS]
    await rows.update_one({"_id": deferred[0]}, {"$set": {"tries": 2}})  # a takeover

    await finish_action(mock_db, mock_mongo_client, deferred[0])
    row = await rows.find_one({"_id": deferred[0]})
    assert row is not None and row["claimed_at"] is None

    lines = [{**(await _get(mock_db))["lines"][0], "state": "proposed"}]
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines": lines}})
    await finish_action(mock_db, mock_mongo_client, deferred[0])
    assert await rows.count_documents({}) == 0 and (await _get(mock_db))["status"] == "dismissed"


async def test_a_press_still_busy_at_the_limit_tells_the_person_it_didnt_happen(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(state="applying"), _id="c1")
    deferred = []
    payload, action = _press("agent_card_dismiss", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    for _ in range(stock_slack.ACTION_TRIES + 1):
        await finish_action(mock_db, mock_mongo_client, deferred[0])
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    note = slack.ephemeral[-1]["text"]
    assert "didn't happen" in note and slack.ephemeral[-1]["user"] == "U1"
    card = await _get(mock_db)
    assert card["status"] == "open" and "didn't happen" in card["press_note"]
    await update_card_message(mock_db, card)
    assert "didn't happen" in _all_text(slack.updates[-1]["blocks"])


async def test_a_busy_undo_held_by_a_lease_is_kept(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    await mock_db[stock_slack.SLACK_ACTIONS].update_one({"_id": row}, {"$set": {"tries": 2}})
    monkeypatch.setattr(apply_mod, "UNDO_LEASE_WAIT_S", 0.01)
    await mock_db[apply_mod.LEASES].insert_one({"_id": f"{agent['_id']}:{GID}", "token": "x", "expires_at": _utcnow() + timedelta(minutes=2)})
    await finish_action(mock_db, mock_mongo_client, row)
    assert (await mock_db[stock_slack.SLACK_ACTIONS].find_one({"_id": row}))["claimed_at"] is None
    assert (await _line(mock_db))["state"] == "applied"


async def test_an_old_press_is_given_up_on_and_told(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    await mock_db[stock_slack.SLACK_ACTIONS].update_one({"_id": row}, {"$set": {"created_at": _utcnow() - timedelta(hours=1)}})
    await finish_action(mock_db, mock_mongo_client, row)
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    assert "didn't happen" in slack.ephemeral[-1]["text"] and (await _line(mock_db))["state"] == "applied"


async def test_a_cut_during_a_stored_press_releases_it_and_costs_no_try(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)

    async def hangs(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(stock_slack, "undo_line", hangs)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(finish_action(mock_db, mock_mongo_client, row), 0.02)
    stored = await mock_db[stock_slack.SLACK_ACTIONS].find_one({"_id": row})
    assert stored["claimed_at"] is None and stored["tries"] == 0


async def test_a_cut_undo_with_nothing_sent_goes_back_to_applied(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")

    async def hangs(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(apply_mod, "get_object_state", hangs)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(apply_mod.undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="slack"), 0.05)
    assert (await _line(mock_db))["state"] == "applied"


async def test_slack_down_and_graph_slow_still_leave_time_for_meta_lines(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """The real numbers: a stuck press and a stuck Slack in the 10 s tick. Another client's queued line still goes."""
    from jobs.creative_tracking import STOCK_LINES_BUDGET_SECONDS
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    await _ads(mock_db, "ad2", "spare2", gid="g2")
    await _posted_card(mock_db, agent, _ln(ad="ad2"), _id="c2", gid="g2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="sam", source="slack", budget_s=0)

    async def hangs(*a, **k):
        await asyncio.sleep(60)

    monkeypatch.setattr(stock_slack, "undo_line", hangs)        # Graph is slow
    slack.chat_update = hangs                                   # Slack is down
    slack.chat_postEphemeral = hangs

    started = time.monotonic()
    res = await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc),
                                     deadline=started + STOCK_LINES_BUDGET_SECONDS)

    assert res["processed"] == 1 and ("ad2", "PAUSED") in meta.real_writes
    assert time.monotonic() - started <= STOCK_LINES_BUDGET_SECONDS + 0.5
    assert (await mock_db[stock_slack.SLACK_ACTIONS].find_one({"_id": row}))["claimed_at"] is None


def test_the_press_cap_fits_one_line_inside_the_tick():
    from jobs.creative_tracking import STOCK_LINES_BUDGET_SECONDS
    assert stock_slack.PRESS_CAP_S == 5.0
    assert apply_mod.MIN_TIME_FOR_LINE_S <= stock_slack.PRESS_CAP_S <= STOCK_LINES_BUDGET_SECONDS - apply_mod.MIN_TIME_FOR_LINE_S


async def test_a_slow_graph_undo_finishes_through_the_cron_at_the_real_numbers(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """The deferred task never ran, so the cron has it. Graph takes the 4 s a line is expected to: it isn't cut."""
    from jobs.creative_tracking import STOCK_LINES_BUDGET_SECONDS
    agent = await _slack_agent(mock_db)
    row = await _undo_press(mock_db, mock_mongo_client, agent, meta)
    real_state = apply_mod.get_object_state

    async def slow_read(*a, **k):
        await asyncio.sleep(2)
        return await real_state(*a, **k)

    monkeypatch.setattr(apply_mod, "get_object_state", slow_read)
    meta.write_delay = 2
    started = time.monotonic()
    await process_queued_lines(mock_db, mock_mongo_client, datetime.now(timezone.utc), deadline=started + STOCK_LINES_BUDGET_SECONDS)

    line = await _line(mock_db)
    assert line["state"] == "undone" and not line.get("undo_error") and meta.real_writes[-1] == ("ad1", "ACTIVE")
    assert await mock_db[stock_slack.SLACK_ACTIONS].count_documents({"_id": row}) == 0


async def test_sync_stops_early_when_slack_is_down(mock_db, slack, installed, monkeypatch):
    from ai.agents.stock.cards import guarded_update
    agent = await _slack_agent(mock_db, gids=(GID, "g2", "g3", "g4"))
    for n, gid in enumerate((GID, "g2", "g3", "g4")):
        card = await _posted_card(mock_db, agent, _ln(), _id=f"c{n}", gid=gid)
        await guarded_update(mock_db, card, {"thought_process": "x"})
    calls = []

    async def hangs(**kw):
        calls.append(1)
        await asyncio.sleep(30)

    slack.chat_update = hangs
    monkeypatch.setattr(stock_slack, "SYNC_CALL_S", 0.02)
    await sync_card_messages(mock_db, time.monotonic() + 5)
    assert len(calls) == stock_slack.SYNC_MAX_TIMEOUTS
    assert (await _get(mock_db, "c0"))["slack"]["needs_update"] is True


# ---------------------------------------------------------------------------
# Answering in time, and who pressed
# ---------------------------------------------------------------------------

async def test_apply_answers_inside_three_seconds_with_meta_and_slack_stuck(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    meta.write_delay = 30

    async def hangs(**kw):
        await asyncio.sleep(30)

    slack.chat_update = hangs
    slack.chat_postEphemeral = hangs
    payload, action = _press("agent_card_apply", "c1")
    started = time.monotonic()
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert time.monotonic() - started < 2.8


async def test_a_note_slack_didnt_take_lands_on_the_card(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1", status="superseded")

    async def hangs(**kw):
        await asyncio.sleep(30)

    slack.chat_postEphemeral = hangs
    payload, action = _press("agent_card_apply", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert "newer" in (await _get(mock_db))["press_note"]


async def test_the_user_id_is_stored_beside_the_name(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    deferred = []
    payload, action = _press("agent_card_apply", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    card = await _get(mock_db)
    assert card["decided_by"]["by"] == "sam" and card["decided_by"]["by_id"] == "U1"
    assert card["lines"][0]["decision"]["by_id"] == "U1"
    payload, action = _press("agent_card_undo:l1", "c1:l1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    await finish_action(mock_db, mock_mongo_client, deferred[0])
    assert (await _line(mock_db))["undo_decision"]["by_id"] == "U1"


async def test_dismiss_and_apply_all_store_the_user_id_too(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}, summary={"clients": 1}))
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await _posted_card(mock_db, agent, _ln(), _id="c2", gid="g2", run_id="r9")
    deferred = []
    payload, action = _press("agent_card_dismiss", "c2")
    await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append)
    await finish_action(mock_db, mock_mongo_client, deferred[0])
    assert (await _get(mock_db, "c2"))["decided_by"]["by_id"] == "U1"
    payload, action = _press("agent_run_apply_all", "r1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert (await _line(mock_db, "l1", "c1"))["decision"]["by_id"] == "U1"


async def test_edit_says_so_when_the_form_doesnt_open(mock_db, mock_mongo_client, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")

    async def network(**kw):
        raise OSError("no route")

    slack.views_open = network
    payload, action = _press("agent_card_edit", "c1")
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client) == {}
    assert "form" in slack.ephemeral[-1]["text"]


async def test_edit_says_so_when_slack_refuses_the_form(mock_db, mock_mongo_client, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")

    async def refused(**kw):
        raise SlackApiError("x", {"ok": False, "error": "expired_trigger_id"})

    slack.views_open = refused
    payload, action = _press("agent_card_edit", "c1")
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert "form" in slack.ephemeral[-1]["text"]


GUEST = {"id": "UGUEST", "name": "guest", "team_id": "T_THEIRS"}


@pytest.mark.parametrize("action_id,value", [
    ("agent_card_apply", "c1"), ("agent_run_apply_all", "r1"), ("agent_card_edit", "c1"),
    ("agent_card_dismiss", "c1"), ("agent_card_undo:l1", "c1:l1")])
async def test_a_press_from_another_organisation_is_refused_with_a_note(mock_db, mock_mongo_client, slack, installed, meta, action_id, value):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await mock_db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"}))
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    before = await _get(mock_db)
    deferred = []
    payload, action = _press(action_id, value, user=GUEST)
    assert await handle_card_action(payload, action, mock_db, mock_mongo_client, defer=deferred.append) == {}
    assert meta.real_writes == [] and deferred == [] and slack.views == [] and slack.updates == []
    assert await _get(mock_db) == before and await mock_db[stock_slack.SLACK_ACTIONS].count_documents({}) == 0
    assert slack.ephemeral[-1]["user"] == "UGUEST" and "workspace" in slack.ephemeral[-1]["text"]


async def test_a_press_from_the_same_team_with_team_id_goes_through(mock_db, mock_mongo_client, slack, installed, meta):
    agent = await _slack_agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload, action = _press("agent_card_apply", "c1", user={"id": "U1", "name": "sam", "team_id": TEAM})
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert meta.real_writes == [("ad1", "PAUSED")]


async def test_lesson_undo_from_another_organisation_is_refused(mock_db, mock_mongo_client, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "stop", "<@U1>")
    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    payload, action = _press("lesson_undo", lesson["_id"], user=GUEST)
    await handle_card_action(payload, action, mock_db, mock_mongo_client)
    assert (await mock_db[MEMORY].find_one({"_id": lesson["_id"]}))["status"] == "active"
    assert "workspace" in slack.ephemeral[-1]["text"]


async def test_edit_form_from_another_organisation_is_refused(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1", next_steps="Later")
    view = edit_view(await _get(mock_db))
    before = await _get(mock_db)
    res = await handle_edit_submission(_submission(view, user=GUEST, values=_values(view, next_steps="Mine")), mock_db)
    assert res["response_action"] == "errors" and "workspace" in json.dumps(res["errors"])
    assert await _get(mock_db) == before


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


async def _two_clients(db, name_a="Glow Aesthetics", name_b="Bright Smiles"):
    agent = await _slack_agent(db, gids=(GID, "g2"))
    await db.client_groups.update_one({"id": GID}, {"$set": {"name": name_a}})
    await db.client_groups.update_one({"id": "g2"}, {"$set": {"name": name_b}})
    await db[RUNS].insert_one(_run(agent, slack={"channel_id": "C1", "ts": "100.1"},
                                   clients={GID: {"state": "done"}, "g2": {"state": "done"}}))
    await _posted_card(db, agent, _ln(), _id="c1", client_name=name_a)
    await _posted_card(db, agent, _id="c2", gid="g2", client_name=name_b)
    return agent


async def test_mention_in_a_many_card_thread_goes_to_the_client_it_names(mock_db, slack, installed):
    await _two_clients(mock_db)
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "bright smiles hates video", "<@U1>")
    assert (await mock_db[MEMORY].find_one({"source_ref": "c2"}))["client_group_id"] == "g2"


async def test_an_unclear_mention_records_nothing_and_asks_which_client(mock_db, slack, installed):
    await _two_clients(mock_db)
    for text in ("no video on Fridays", "Glow Aesthetics and Bright Smiles both hate video"):
        assert await handle_thread_mention(mock_db, TEAM, "C1", "100.1", text, "<@U1>") is True
    assert await mock_db[MEMORY].count_documents({}) == 0
    reply = slack.posts[-1]
    assert "which client" in reply["text"].lower() and "Bright Smiles" in reply["text"]
    assert "lesson_undo" not in _all_text(reply.get("blocks"))


async def test_the_longest_name_wins(mock_db, slack, installed):
    await _two_clients(mock_db, "Acme", "Acme London")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "Acme London hates video", "<@U1>")
    assert (await mock_db[MEMORY].find_one({"author": "<@U1>"}))["client_group_id"] == "g2"
    await mock_db[MEMORY].delete_many({})
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "acme hates video", "<@U1>")
    assert (await mock_db[MEMORY].find_one({"author": "<@U1>"}))["client_group_id"] == GID


async def test_a_client_called_go_needs_its_exact_case(mock_db, slack, installed):
    await _two_clients(mock_db, "Go", "Bright Smiles")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "we should go ahead with video", "<@U1>")
    assert await mock_db[MEMORY].count_documents({}) == 0 and "which client" in slack.posts[-1]["text"].lower()
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "Go hates video", "<@U1>")
    assert (await mock_db[MEMORY].find_one({"author": "<@U1>"}))["client_group_id"] == GID


async def test_a_word_inside_another_word_isnt_a_name(mock_db, slack, installed):
    await _two_clients(mock_db, "Prime", "Bright Smiles")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "primetime video is out", "<@U1>")
    assert await mock_db[MEMORY].count_documents({}) == 0


async def test_a_one_card_thread_naming_another_client_uses_that_client(mock_db, slack, installed):
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": "Acme"}})
    await mock_db.client_groups.update_one({"id": "g2"}, {"$set": {"name": "Beta"}})
    await _posted_card(mock_db, agent, _ln(), _id="c1", client_name="Acme")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "for Beta, keep the summer ad", "<@U1>")
    lesson = await mock_db[MEMORY].find_one({"author": "<@U1>"})
    assert lesson["client_group_id"] == "g2" and "Beta" in slack.posts[-1]["text"]
    await mock_db[MEMORY].delete_many({})
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "keep the summer ad", "<@U1>")
    assert (await mock_db[MEMORY].find_one({"author": "<@U1>"}))["client_group_id"] == GID


async def test_a_one_card_thread_naming_two_others_asks(mock_db, slack, installed):
    agent = await _slack_agent(mock_db, gids=(GID, "g2", "g3"))
    for gid, name in ((GID, "Acme"), ("g2", "Beta"), ("g3", "Gamma")):
        await mock_db.client_groups.update_one({"id": gid}, {"$set": {"name": name}})
    await _posted_card(mock_db, agent, _ln(), _id="c1", client_name="Acme")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "Beta and Gamma hate this", "<@U1>")
    assert await mock_db[MEMORY].count_documents({}) == 0


async def test_a_client_name_in_the_reply_can_not_ping(mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": "<!channel>"}})
    await _posted_card(mock_db, agent, _ln(), _id="c1", client_name="<!channel>")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "stop this", "<@U1>")
    assert "<!channel>" not in _all_text(slack.posts[-1]["blocks"]) and "<!channel>" not in slack.posts[-1]["text"]


async def test_acmes_filter_doesnt_refuse_betas_ad(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch):
    """The AI's filter names no client. Scoped to Acme, it can't reach Beta."""
    reply = json.dumps({"kind": "exception", "text": "Leave the Summer ads alone", "filter": {"ad_name_contains": "Summer"}})
    use_provider(monkeypatch, FakeProvider(reply=reply))
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": "Acme"}})
    await mock_db.client_groups.update_one({"id": "g2"}, {"$set": {"name": "Beta"}})
    for gid, ad in ((GID, "a1"), ("g2", "b1")):
        await mock_db["meta_ad_state"].insert_many([
            {"client_group_id": gid, "ad_id": ad, "name": "Summer sale", "adset_id": f"s{gid}", "campaign_id": "c1", "effective_status": "ACTIVE"},
            {"client_group_id": gid, "ad_id": f"{ad}x", "name": "Spare", "adset_id": f"s{gid}", "campaign_id": "c1", "effective_status": "ACTIVE"}])
    from ai.agents import claims
    await claims.claim_clients(mock_db, USER, agent["_id"], ["g2"])
    await _posted_card(mock_db, agent, {**_ln(ad="a1"), "ad_name": "Summer sale"}, _id="c1", client_name="Acme")
    await _card(mock_db, agent, {**_ln(ad="b1"), "ad_name": "Summer sale"}, _id="c2", run_id="r2", gid="g2", client_name="Beta")

    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "leave the Summer ads alone", "<@U1>")

    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    assert lesson["filter"] == {"ad_name_contains": "Summer", "client_group_id": GID}
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="sam", source="dashboard")
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="dashboard")
    assert meta.real_writes == [("b1", "PAUSED")]
    assert (await _line(mock_db, "l1", "c1"))["reason_code"] == "exception"


@pytest.mark.parametrize("flt", [{"client_group_id": GID}, {"weekdays": [0, 1, 2, 3, 4, 5, 6]},
                                 {"client_group_id": GID, "weekdays": [1]}, {"ad_name_contains": "Summer", "weekdays": [1]}])
async def test_leave_acme_alone_refuses_nothing_on_apply_or_auto(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch, flt):
    """One sentence in a channel can't stop every change on a client."""
    use_provider(monkeypatch, FakeProvider(reply=json.dumps({"kind": "exception", "text": "Leave Acme alone", "filter": flt})))
    settings = {"modes": {"remove": "auto", "revive": "ask", "add": "ask"}, "slack": SLACK}
    agent = await _agent(mock_db, settings=settings)
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": "Acme"}})
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _posted_card(mock_db, agent, _ln(), _id="c0")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "leave Acme alone", "<@U1>")

    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    assert lesson["kind"] == "fact" and not lesson.get("filter") and "stays a note" in slack.posts[-1]["text"]

    await apply_card(mock_db, mock_mongo_client, USER, "c0", by="sam", source="slack")
    assert ("ad1", "PAUSED") in meta.real_writes and (await _line(mock_db, "l1", "c0"))["state"] == "applied"

    run = await _stored_run(mock_db, agent, _id="r5", run_id="r5", phase="publishing", slack={"channel_id": "C1", "ts": "1.1"})
    await _card(mock_db, agent, _ln(ad="ad2", mode="auto"), _id="c5", run_id="r5", slack={"channel_id": None, "ts": None})
    await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=NOW)
    assert ("ad2", "PAUSED") in meta.real_writes


@pytest.mark.parametrize("client,name", [("Acme", "Acme"), ("Acme", "ACME"), ("Acme London", "Acme"), ("Acme London", "acme london"),
                                         ("Acme", "Acme Summer"), ("  Acme   London ", "Acme  London")])
async def test_a_name_filter_matching_the_clients_own_name_refuses_nothing(mock_db, mock_mongo_client, slack, installed, meta, monkeypatch, client, name):
    reply = {"kind": "exception", "text": "Leave Acme alone", "filter": {"ad_name_contains": name}}
    use_provider(monkeypatch, FakeProvider(reply=json.dumps(reply)))
    agent = await _slack_agent(mock_db)
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": client}})
    await _ads(mock_db, "ad1", "spare")
    await _posted_card(mock_db, agent, {**_ln(), "ad_name": "Acme Summer sale"}, _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "leave Acme alone", "<@U1>")

    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    assert lesson["kind"] == "fact" and not lesson.get("filter") and "stays a note" in slack.posts[-1]["text"]
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="slack")
    assert ("ad1", "PAUSED") in meta.real_writes


async def test_a_name_filter_unlike_the_clients_name_stays_a_filter(mock_db, slack, installed, monkeypatch):
    reply = {"kind": "exception", "text": "Leave Summer alone", "filter": {"ad_name_contains": "Summer"}}
    use_provider(monkeypatch, FakeProvider(reply=json.dumps(reply)))
    agent = await _slack_agent(mock_db)
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": "Acme London"}})
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "leave Summer alone", "<@U1>")
    assert (await mock_db[MEMORY].find_one({"source": "slack_mention"}))["kind"] == "exception"


async def test_a_dashboard_lesson_can_still_use_the_clients_name(mock_db, monkeypatch):
    reply = {"kind": "exception", "text": "Leave Acme alone", "filter": {"ad_name_contains": "Acme"}}
    use_provider(monkeypatch, FakeProvider(reply=json.dumps(reply)))
    agent = await _slack_agent(mock_db)
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"name": "Acme"}})
    lesson = await lessons.lesson_from_text(mock_db, agent, "leave Acme", source="dismiss", author="x", client_group_id=GID)
    assert lesson["filter"] == {"ad_name_contains": "Acme"}


async def test_a_name_filter_with_this_clients_own_id_stays_a_filter(mock_db, slack, installed, monkeypatch):
    reply = {"kind": "exception", "text": "Leave Summer alone", "filter": {"ad_name_contains": "Summer", "client_group_id": GID}}
    use_provider(monkeypatch, FakeProvider(reply=json.dumps(reply)))
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "leave the Summer ads alone", "<@U1>")
    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    assert lesson["kind"] == "exception" and lesson["filter"] == {"ad_name_contains": "Summer", "client_group_id": GID}


async def test_a_filter_naming_another_client_stays_a_note_and_the_reply_says_so(mock_db, slack, installed, monkeypatch):
    reply = json.dumps({"kind": "exception", "text": "Leave Summer alone",
                        "filter": {"ad_name_contains": "Summer", "client_group_id": "g2"}})
    use_provider(monkeypatch, FakeProvider(reply=reply))
    agent = await _slack_agent(mock_db, gids=(GID, "g2"))
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    await handle_thread_mention(mock_db, TEAM, "C1", "100.1", "leave the Summer ads alone", "<@U1>")
    lesson = await mock_db[MEMORY].find_one({"source": "slack_mention"})
    assert lesson["kind"] == "fact" and not lesson.get("filter")
    assert "stays a note" in slack.posts[-1]["text"]


async def test_a_lesson_with_no_client_gets_no_filter(mock_db, monkeypatch):
    reply = json.dumps({"kind": "exception", "text": "Leave Summer alone", "filter": {"ad_name_contains": "Summer"}})
    use_provider(monkeypatch, FakeProvider(reply=reply))
    agent = await _slack_agent(mock_db)
    lesson = await lessons.lesson_from_text(mock_db, agent, "leave Summer", source="slack_mention", author="x")
    assert lesson["kind"] == "fact" and not lesson.get("filter")


async def test_a_dismiss_reason_from_the_dashboard_keeps_its_unscoped_filter(mock_db, monkeypatch):
    reply = json.dumps({"kind": "exception", "text": "Leave Summer alone", "filter": {"ad_name_contains": "Summer"}})
    use_provider(monkeypatch, FakeProvider(reply=reply))
    agent = await _slack_agent(mock_db)
    lesson = await lessons.lesson_from_text(mock_db, agent, "leave Summer", source="dismiss", author="x", client_group_id=GID)
    assert lesson["filter"] == {"ad_name_contains": "Summer"}


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


@pytest.mark.parametrize("extra,outer", [({"user_team": OTHER_TEAM}, {}), ({"team": OTHER_TEAM}, {}),
                                         ({"user_team": OTHER_TEAM, "team": TEAM}, {}), ({}, {"is_ext_shared_channel": True})])
async def test_route_a_guests_mention_in_a_card_thread_teaches_nothing(routes, mock_db, slack, installed, extra, outer):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload = _mention("100.1")
    payload["event"].update(extra)
    payload.update(outer)
    with patch("routers.slack_events.run_chat", AsyncMock()) as chat, \
            patch("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack):
        resp = await _post_event(routes, payload)
    assert resp.status_code == 200
    chat.assert_not_called()
    assert await mock_db[MEMORY].count_documents({}) == 0
    assert "can't teach this agent" in slack.posts[-1]["text"] and slack.posts[-1]["thread_ts"] == "100.1"


@pytest.mark.parametrize("extra,outer", [({"user_team": TEAM}, {"is_ext_shared_channel": True}), ({"team": TEAM}, {"is_ext_shared_channel": True}),
                                         ({}, {"is_ext_shared_channel": False}), ({}, {})])
async def test_route_a_colleagues_mention_still_teaches(routes, mock_db, slack, installed, extra, outer):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    payload = _mention("100.1")
    payload["event"].update(extra)
    payload.update(outer)
    with patch("routers.slack_events.run_chat", AsyncMock()) as chat, \
            patch("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack):
        await _post_event(routes, payload)
    chat.assert_not_called()
    assert await mock_db[MEMORY].count_documents({"source": "slack_mention"}) == 1


async def test_route_a_guests_mention_outside_a_card_thread_is_left_as_it_was(routes, mock_db, slack, installed):
    agent = await _slack_agent(mock_db)
    await _posted_card(mock_db, agent, _ln(), _id="c1")
    reply = {"reply": "hi", "session_id": "s1"}
    payload = _mention("999.9")
    payload["event"]["user_team"] = OTHER_TEAM
    with patch("routers.slack_events.run_chat", AsyncMock(return_value=reply)) as chat, \
            patch("routers.slack_events.get_provider_for_user", AsyncMock()), \
            patch("credits.is_blocked", AsyncMock(return_value=False)), \
            patch("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack):
        await _post_event(routes, payload)
    chat.assert_awaited_once()


async def test_route_a_guests_mention_doesnt_reach_chat_when_the_lookup_fails(routes, mock_db, slack, installed):
    payload = _mention("100.1")
    payload["event"]["user_team"] = OTHER_TEAM
    with patch("routers.slack_events.run_chat", AsyncMock()) as chat, \
            patch("routers.slack_events.handle_thread_mention", AsyncMock(side_effect=RuntimeError("blip"))), \
            patch("slack_sdk.web.async_client.AsyncWebClient", lambda **kw: slack):
        await _post_event(routes, payload)
    chat.assert_not_called()


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
