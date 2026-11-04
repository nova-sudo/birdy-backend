"""Applying, undoing and auto-running stock card lines (spec section 7). These change real client
ad accounts, so most of this file is about what happens when something goes wrong. Meta is always
the fake below: no test can reach Graph."""

import asyncio
import contextlib
import inspect
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import routers.agent_stock as stock_router
import routers.agents as agents_router
from ai.agents import claims, guardrails, meta_state
from ai.agents.outcomes import UNDO_AUTO_WINDOW
from ai.agents.stock import apply as apply_mod
from ai.agents.stock import runner
from ai.agents.stock.apply import (STUCK_AFTER, apply_card, apply_run, auto_apply_run, dismiss_card,
                                   process_queued_lines, undo_line)
from ai.agents.stock.candidates import build_candidates
from ai.agents.stock.cards import build_card, client_name_flags, guarded_update, supersede_open
from ai.agents.stock.checks import client_facts
from ai.agents.stock.settings import DEFAULT_SETTINGS
from ai.agents.stock.windows import Window
from ai.agents.store import AD_DAYS, AGENTS, CARDS, RUNS, TYPE_STOCK, create_agent, create_agent_indexes
from ai.suggestions.contracts import ACTION_PAUSE_ADS
from ai.suggestions.store import ACTIVITY, KIND_ACTION_APPLIED, KIND_ACTION_UNDONE
from dependencies import get_current_user
from jobs import creative_tracking
from main import app
from test_agent_learning import FakeProvider, use_provider
from test_stock_runner import _run_all, _setup, fakes  # noqa: F401 - fakes is a fixture
from test_stock_runner import FACTS, USER, NOW

GID = "g1"
ZONES = {"currency": "GBP", "green_under": 8, "red_over": 10}


class Killed(BaseException):
    """The function being killed. Not an Exception, so no handler in the code can see it."""


class FakeMeta:
    def __init__(self):
        self.ads: dict[str, dict | None] = {}
        self.writes: list[tuple[str, str]] = []
        self.reads: list[str] = []
        self.read_error: Exception | None = None
        self.fail_write: str | None = None      # Meta refuses the write, nothing changes
        self.lost_reply = False                 # the write lands, then the reply is lost
        self.kill_after_write = False
        self.kill_before_write = False
        self.write_delay = 0.0
        self.names: dict[str, str] = {}         # an ad renamed in Meta since the last refresh
        self.adset_reads: list[str] = []
        self.db = None

    async def adset_ads(self, mongo_client, user_id, adset_id):
        """What Meta has for the ad set: the mirror's rows, unless a test changed the ad in Meta."""
        self.adset_reads.append(adset_id)
        out = []
        async for row in self.db["meta_ad_state"].find({"adset_id": adset_id}):
            ad = row["ad_id"]
            if ad in self.ads and self.ads[ad] is None:
                continue
            status = (self.ads.get(ad) or {}).get("effective_status") or row.get("effective_status")
            out.append({"id": ad, "name": self.names.get(ad) or row.get("name") or "", "effective_status": status})
        return out

    def set(self, ad, status):
        self.ads[ad] = {"status": status, "effective_status": status}

    async def state(self, user_id, object_id, object_type, mongo_client):
        self.reads.append(object_id)
        if self.read_error:
            raise self.read_error
        return self.ads.get(object_id, {"status": "ACTIVE", "effective_status": "ACTIVE"})

    async def execute(self, mongo_client, user_id, action):
        assert action["type"] in ("pause_ads", "enable_ads")
        desired = "PAUSED" if action["type"] == "pause_ads" else "ACTIVE"
        succeeded, failed = [], []
        for t in action["targets"]:
            ad = t["object_id"]
            if self.kill_before_write:
                raise Killed()
            if self.write_delay:
                await asyncio.sleep(self.write_delay)
            if self.fail_write:
                self.writes.append((ad, "refused"))
                failed.append({"object_id": ad, "error": self.fail_write})
                continue
            self.writes.append((ad, desired))
            self.set(ad, desired)
            if self.kill_after_write:
                raise Killed()
            if self.lost_reply:
                failed.append({"object_id": ad, "error": "Failed to reach Meta: timed out"})
            else:
                succeeded.append(ad)
        return {"succeeded": succeeded, "failed": failed}

    @property
    def real_writes(self):
        return [w for w in self.writes if w[1] != "refused"]


@pytest.fixture
def meta(monkeypatch, mock_db):
    m = FakeMeta()
    m.db = mock_db
    monkeypatch.setattr(meta_state, "fetch_adset_ads", m.adset_ads)
    monkeypatch.setattr(apply_mod, "execute_action", m.execute)
    monkeypatch.setattr(apply_mod, "get_object_state", m.state)
    return m


def _ln(n=1, kind="remove", ad=None, mode="ask", state=None, **extra):
    ad = ad if ad is not None else f"ad{n}"
    return {"id": f"l{n}", "kind": kind, "ad_name": f"Name {ad}", "mode": mode,
            "state": state or ("flagged" if mode == "flag" else "proposed"), "error": None,
            "meta_ids": [ad] if ad else [], "flags": [], **extra}


async def _agent(db, *, status="active", claim=True, settings=None, guardrails=None, gids=(GID,)):
    agent = await create_agent(db, USER, type=TYPE_STOCK)
    fields = {"status": status, "scope": {"client_group_ids": list(gids), "schedule": {"time": "07:00", "tz": "UTC"}},
              "settings": {**DEFAULT_SETTINGS, "zones": ZONES, "modes": {"remove": "auto", "revive": "auto", "add": "ask"},
                           **(settings or {})}}
    if guardrails is not None:
        fields["guardrails"] = {**agent["guardrails"], **guardrails}
    await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": fields})
    if claim:
        await claims.claim_clients(db, USER, agent["_id"], list(gids))
    for gid in gids:
        if not await db.client_groups.find_one({"id": gid, "user_id": USER}):
            await db.client_groups.insert_one({"id": gid, "user_id": USER, "name": f"Client {gid}",
                                               "meta_ad_account_id": f"act_{gid}", "meta_campaign_ids": None})
    return await db[AGENTS].find_one({"_id": agent["_id"]})


async def _ads(db, *live, off=(), adset="s1", gid=GID):
    """Mirrored ad state. Each ad set here keeps a spare live ad unless a test takes it away."""
    for ad, status in [(a, "ACTIVE") for a in live] + [(a, "PAUSED") for a in off]:
        await db["meta_ad_state"].insert_one({"client_group_id": gid, "ad_id": ad, "name": f"Name {ad}",
                                              "adset_id": adset, "campaign_id": "c1", "effective_status": status})


async def _card(db, agent, *lines, _id="c1", run_id="r1", status="open", gid=GID, created=None, **extra):
    doc = {"_id": _id, "agent_id": agent["_id"], "user_id": USER, "run_id": run_id, "client_group_id": gid,
           "client_name": f"Client {gid}", "tag": "changes", "lines": list(lines), "status": status, "stale": False,
           "ai_failed": False, "created_at": created or datetime.utcnow() - timedelta(hours=1), **extra}
    await db[CARDS].insert_one(doc)
    return doc


def _run(agent, *, run_id="r1", clients=None, alerts=None, **extra):
    return {"_id": run_id, "agent_id": agent["_id"], "user_id": USER, "kind": "batch", "local_date": "2026-11-03",
            "clients": clients if clients is not None else {GID: {"state": "done", "health_done": True}},
            "alerts": alerts or [], "phase": "publishing", **extra}


async def _get(db, _id="c1"):
    return await db[CARDS].find_one({"_id": _id})


async def _line(db, line_id="l1", _id="c1"):
    return next(ln for ln in (await _get(db, _id))["lines"] if ln["id"] == line_id)


async def _applied_rows(db, **q):
    return await db[ACTIVITY].find({"kind": KIND_ACTION_APPLIED, **q}).to_list(None)


def _later(minutes=0, **kw):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes, **kw)


async def _tick(db, client, minutes=0, **kw):
    return await process_queued_lines(db, client, _later(minutes, **kw), deadline=time.monotonic() + 30)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

async def test_apply_pauses_mirrors_and_logs(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="sam", source="dashboard")
    assert res["ok"] and res["outcome"] == "applied"
    assert meta.writes == [("ad1", "PAUSED")]
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["wrote"] is True and line["decision"]["by"] == "sam"
    assert line["undo_until"] - line["applied_at"] == UNDO_AUTO_WINDOW
    card = await _get(mock_db)
    assert card["status"] == "applied" and card["decided_by"]["by"] == "sam" and card["version"] >= 1
    state = await mock_db["meta_ad_state"].find_one({"ad_id": "ad1"})
    assert state["effective_status"] == "PAUSED"


async def test_bring_back_enables(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(kind="revive", ad="old"))
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "applied"
    assert meta.writes == [("old", "ACTIVE")]
    assert (await mock_db["meta_ad_state"].find_one({"ad_id": "old"}))["effective_status"] == "ACTIVE"


async def test_double_apply_writes_once(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    results = await asyncio.gather(
        *[apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="slack") for _ in range(5)])
    assert meta.writes == [("ad1", "PAUSED")] and len(await _applied_rows(mock_db)) == 1
    assert sorted(r["outcome"] for r in results).count("applied") == 1
    # A later press returns what happened. It writes nothing.
    again = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="slack")
    assert again["ok"] and again["outcome"] == "already" and again["lines"][0]["state"] == "applied"
    assert meta.writes == [("ad1", "PAUSED")]


def _both_read_first(monkeypatch, callers=2):
    """Real interleaving: mongomock doesn't yield, so hold each caller after its first read until all
    have read the same card."""
    barrier = asyncio.Barrier(callers)
    real = apply_mod._visible_card

    async def read_then_wait(*a):
        card = await real(*a)
        await barrier.wait()
        return card

    monkeypatch.setattr(apply_mod, "_visible_card", read_then_wait)


async def test_two_applies_that_read_the_same_card_write_once(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    meta.write_delay = 0.01
    _both_read_first(monkeypatch)
    results = await asyncio.gather(*[apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="slack")
                                     for _ in range(2)])
    assert meta.writes == [("ad1", "PAUSED")] and len(await _applied_rows(mock_db)) == 1
    assert sorted(r["outcome"] for r in results) == ["already", "applied"]


async def test_claim_lost_to_a_writer_between_read_and_write_is_retried(mock_db, mock_mongo_client, meta, monkeypatch):
    """The version check is what makes the claim atomic: the other press gets in after this one read."""
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    real = apply_mod.guarded_update
    first = []

    async def rival_first(db, card, fields, **kw):
        if not first:
            first.append(1)
            await apply_card(mock_db, mock_mongo_client, USER, "c1", by="rival", source="slack")
        return await real(db, card, fields, **kw)

    monkeypatch.setattr(apply_mod, "guarded_update", rival_first)
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="slack")
    assert res["outcome"] == "already" and meta.writes == [("ad1", "PAUSED")]
    assert (await _line(mock_db))["decision"]["by"] == "rival"


async def test_refused_when_agent_off(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, status="paused")
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "blocked" and "off" in res["reason"]
    assert meta.writes == [] and meta.reads == []
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "agent_off" and "off" in line["error"]


async def test_refused_when_client_moved(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await claims.claim_clients(mock_db, USER, "another-agent", [GID], move=True)
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and "Another agent" in res["reason"]
    assert meta.writes == []
    assert (await _line(mock_db))["reason_code"] == "not_acting_agent"


@pytest.mark.parametrize("fields", [{"status": "superseded"}, {"preview": True}, {"status": "dismissed"},
                                    {"status": "flagged"}])
async def test_refused_on_superseded_or_preview_card(fields, mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(), **fields)
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "refused" and res["reason"]
    assert meta.writes == [] and (await _line(mock_db))["state"] == "proposed"


async def test_closing_card_and_another_users_card_are_not_found(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(), status="closing")
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "not_found"
    await _card(mock_db, agent, _ln(), _id="c2", run_id="r2")
    assert (await apply_card(mock_db, mock_mongo_client, "u2", "c2", by="s", source="d"))["outcome"] == "not_found"
    assert meta.writes == []


async def test_flag_line_never_writes(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1, mode="flag"), _ln(2))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert res["outcome"] == "noop" and meta.writes == []
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert meta.writes == [("ad2", "PAUSED")]
    assert (await _line(mock_db, "l1"))["state"] == "flagged"
    assert (await _get(mock_db))["status"] == "applied"  # a Flag line isn't waiting on anyone


async def test_add_line_turns_into_a_flag_with_add_writes_off(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(1, kind="add", ad=""), _ln(2, ad="ad1"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert meta.writes == [("ad1", "PAUSED")]
    assert (await _line(mock_db, "l1"))["state"] == "flagged"
    assert (await _get(mock_db))["status"] == "applied"


async def test_unknown_line_is_rejected(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["nope"])
    assert res["outcome"] == "bad_lines" and meta.writes == []


async def test_guardrail_last_live_ad_blocks_with_reason(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1")
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and "no live ads" in res["reason"]
    assert meta.writes == []
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "last_live_ad"
    assert (await _get(mock_db))["status"] == "open"


async def test_second_line_sees_the_first_lines_pause(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2")
    await _card(mock_db, agent, _ln(1), _ln(2))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "partial" and meta.writes == [("ad1", "PAUSED")]
    assert (await _line(mock_db, "l2"))["reason_code"] == "last_live_ad"
    assert (await _get(mock_db))["status"] == "partly_applied"


async def test_protected_tag_blocks(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, {**_ln(), "ad_name": "Evergreen hero"})
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == []
    assert (await _line(mock_db))["reason_code"] == "protected_tag"


async def test_pause_counts_toward_daily_cap(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, guardrails={"max_pauses_per_client_day": 1})
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert meta.writes == [("ad1", "PAUSED")]
    [row] = await _applied_rows(mock_db)
    assert row["applied_action_type"] == ACTION_PAUSE_ADS and row["client_group_id"] == GID
    assert row["ref_id"] == "c1:l1" and row["agent_id"] == agent["_id"] and row["actor"] == "user"
    assert (await _line(mock_db, "l2"))["reason_code"] == "pause_cap"


async def test_already_off_in_meta_writes_nothing_and_does_not_count(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    meta.set("ad1", "PAUSED")
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "applied" and meta.writes == []
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["wrote"] is False and "Already" in line["note"]
    [row] = await _applied_rows(mock_db)
    assert "applied_action_type" not in row


async def test_revive_of_an_ad_gone_from_meta_fails_plainly(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, off=["old"])
    meta.ads["old"] = None
    await _card(mock_db, agent, _ln(kind="revive", ad="old"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    line = await _line(mock_db)
    assert line["state"] == "failed" and "no longer exists" in line["error"] and meta.writes == []


async def test_revive_never_passes_max_ads_live(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "a", "b", "c", "d", off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(kind="revive", ad="old"))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == []
    assert (await _line(mock_db))["reason_code"] == "max_ads_live"


async def test_remove_runs_before_bring_back_so_a_swap_fits_the_limit(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "a", "b", "c", "d", off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(1, kind="revive", ad="old"), _ln(2, ad="a"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert meta.writes == [("a", "PAUSED"), ("old", "ACTIVE")]


async def test_client_on_hold_is_blocked(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, settings={"clients": {GID: {"on_hold": True}}})
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == []
    assert (await _line(mock_db))["reason_code"] == "on_hold"


# ---------------------------------------------------------------------------
# Meta failures: nothing is called failed unless Meta confirms it
# ---------------------------------------------------------------------------

async def test_meta_refuses_the_write(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    meta.fail_write = "Meta API error: not allowed"
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "failed" and "not allowed" in res["reason"]
    line = await _line(mock_db)
    assert line["state"] == "failed" and "not allowed" in line["error"]
    assert await _applied_rows(mock_db) == []
    # Failed lines wait for an explicit Retry: a plain second press doesn't resend.
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "already"
    assert len(meta.writes) == 1
    meta.fail_write = None
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert res["outcome"] == "applied" and (await _line(mock_db))["error"] is None
    assert meta.real_writes == [("ad1", "PAUSED")] and len(await _applied_rows(mock_db)) == 1


async def test_write_reply_lost_but_meta_shows_it_landed(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    meta.lost_reply = True
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "applied" and len(meta.writes) == 1
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["wrote"] is True and line["error"] is None
    [row] = await _applied_rows(mock_db)
    assert row["applied_action_type"] == ACTION_PAUSE_ADS  # it counts: Birdy did pause it


async def test_write_reply_lost_and_meta_cannot_be_read_stays_applying(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    meta.lost_reply = True

    real_state = meta.state
    calls = {"n": 0}

    async def second_read_fails(*a):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("Graph down")
        return await real_state(*a)

    apply_mod.get_object_state = second_read_fails
    try:
        res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    finally:
        apply_mod.get_object_state = meta.state
    assert res["outcome"] == "queued" and (await _line(mock_db))["state"] == "applying"
    await _tick(mock_db, mock_mongo_client, minutes=STUCK_AFTER.total_seconds() / 60 + 1)
    assert (await _line(mock_db))["state"] == "applied" and len(meta.writes) == 1


async def test_meta_unreadable_before_the_write_changes_nothing(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    meta.read_error = RuntimeError("Graph down")
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "failed" and meta.writes == []
    assert "nothing was changed" in (await _line(mock_db))["error"]


# ---------------------------------------------------------------------------
# Crash safety
# ---------------------------------------------------------------------------

async def _kill_after_write(db, client, meta, agent):
    await _ads(db, "ad1", "spare")
    await _card(db, agent, _ln())
    meta.kill_after_write = True
    with pytest.raises(Killed):
        await apply_card(db, client, USER, "c1", by="s", source="d")
    meta.kill_after_write = False
    line = await _line(db)
    assert line["state"] == "applying" and line["write_started_at"]
    assert meta.writes == [("ad1", "PAUSED")]


async def test_kill_after_write_recovers_from_metas_status_without_resending(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _kill_after_write(mock_db, mock_mongo_client, meta, agent)
    # Too soon: that invocation could still be alive.
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying" and meta.reads.count("ad1") == 1
    await _tick(mock_db, mock_mongo_client, minutes=STUCK_AFTER.total_seconds() / 60 + 1)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["wrote"] is True and line["error"] is None
    assert meta.writes == [("ad1", "PAUSED")]
    [row] = await _applied_rows(mock_db)
    assert row["applied_action_type"] == ACTION_PAUSE_ADS
    assert (await mock_db["meta_ad_state"].find_one({"ad_id": "ad1"}))["effective_status"] == "PAUSED"
    assert (await _get(mock_db))["status"] == "applied"


async def test_kill_after_activity_row_does_not_log_twice(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    real = apply_mod.log_activity

    async def log_then_die(*a, **kw):
        await real(*a, **kw)
        raise Killed()

    monkeypatch.setattr(apply_mod, "log_activity", log_then_die)
    with pytest.raises(Killed):
        await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    monkeypatch.setattr(apply_mod, "log_activity", real)
    await _tick(mock_db, mock_mongo_client, minutes=STUCK_AFTER.total_seconds() / 60 + 1)
    assert (await _line(mock_db))["state"] == "applied" and len(await _applied_rows(mock_db)) == 1
    assert len(meta.writes) == 1


async def test_kill_mid_write_that_never_landed_fails_without_resending(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    meta.kill_before_write = True
    with pytest.raises(Killed):
        await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    meta.kill_before_write = False
    await _tick(mock_db, mock_mongo_client, minutes=STUCK_AFTER.total_seconds() / 60 + 1)
    line = await _line(mock_db)
    assert line["state"] == "failed" and meta.writes == [] and "still" in line["error"]
    assert await _applied_rows(mock_db) == []
    # A person can retry it.
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert res["outcome"] == "applied" and meta.writes == [("ad1", "PAUSED")]


async def test_kill_before_the_write_was_started_is_requeued_and_rechecked(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(state="applying", applying_at=datetime.utcnow() - timedelta(hours=1),
                                    decision={"actor": "user", "by": "s", "source": "d"}))
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and meta.writes == [("ad1", "PAUSED")]


async def test_requeue_recheck_refuses_after_the_client_moved(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(state="applying", applying_at=datetime.utcnow() - timedelta(hours=1),
                                    decision={"actor": "user", "by": "s", "source": "d"}))
    await claims.claim_clients(mock_db, USER, "another-agent", [GID], move=True)
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "not_acting_agent" and meta.writes == []


async def test_a_line_that_keeps_dying_before_the_write_gives_up(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(state="applying", applying_at=datetime.utcnow() - timedelta(hours=1),
                                    recoveries=apply_mod.MAX_RECOVERIES,
                                    decision={"actor": "user", "by": "s", "source": "d"}))
    await _tick(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "Nothing was changed" in line["error"] and meta.writes == []


async def test_unreadable_status_waits_then_says_it_could_not_confirm(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _kill_after_write(mock_db, mock_mongo_client, meta, agent)
    meta.read_error = RuntimeError("Graph down")
    await _tick(mock_db, mock_mongo_client, minutes=10)
    assert (await _line(mock_db))["state"] == "applying"
    await _tick(mock_db, mock_mongo_client, minutes=apply_mod.CONFIRM_GIVE_UP.total_seconds() / 60 + 1)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "couldn't confirm" in line["error"]
    assert meta.writes == [("ad1", "PAUSED")]
    # Retry reads first, finds it did land, and counts it as Birdy's write. Nothing is sent again.
    meta.read_error = None
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert res["outcome"] == "applied" and meta.writes == [("ad1", "PAUSED")]
    assert (await _line(mock_db))["wrote"] is True
    [row] = await _applied_rows(mock_db)
    assert row["applied_action_type"] == ACTION_PAUSE_ADS


async def test_recovery_records_truth_on_a_card_superseded_meanwhile(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _kill_after_write(mock_db, mock_mongo_client, meta, agent)
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"status": "superseded"}})
    await _tick(mock_db, mock_mongo_client, minutes=10)
    assert (await _line(mock_db))["state"] == "applied"
    assert (await _get(mock_db))["status"] == "superseded"
    # What Birdy did can still be undone.
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "undone" and meta.writes[-1] == ("ad1", "ACTIVE")


async def test_write_racing_a_supersede_is_stopped_at_the_last_step(mock_db, mock_mongo_client, meta):
    """The card closes after the claim and before the send: the status check on write_started
    catches it."""
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    real = meta.state

    async def supersede_during_read(*a):
        await supersede_open(mock_db, agent["_id"], GID, "newer-run")
        return await real(*a)

    apply_mod.get_object_state = supersede_during_read
    try:
        res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    finally:
        apply_mod.get_object_state = meta.state
    assert res["outcome"] == "blocked" and meta.writes == []
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "superseded"


async def test_finish_survives_a_card_edit_during_the_write(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    real = meta.execute

    async def edit_during_write(*a):
        await guarded_update(mock_db, await _get(mock_db), {"next_steps": "edited"})
        return await real(*a)

    apply_mod.execute_action = edit_during_write
    try:
        await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    finally:
        apply_mod.execute_action = meta.execute
    card = await _get(mock_db)
    assert card["next_steps"] == "edited" and card["lines"][0]["state"] == "applied"


# ---------------------------------------------------------------------------
# Superseded and partly applied
# ---------------------------------------------------------------------------

async def test_queued_line_on_a_card_superseded_mid_flight_is_refused(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(state="queued", decision={"actor": "user", "by": "s", "source": "d"}),
                status="superseded")
    await _tick(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "superseded" and meta.writes == []


async def test_partly_applied_card_refuses_remaining_lines_once_a_newer_card_exists(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d",
                             line_ids=["l1"]))["outcome"] == "applied"
    assert (await _get(mock_db))["status"] == "partly_applied"
    await _card(mock_db, agent, _ln(1, ad="ad2"), _id="c2", run_id="r2", created=datetime.utcnow() + timedelta(hours=1))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l2"])
    assert res["outcome"] == "refused" and "newer" in res["reason"] and len(meta.writes) == 1
    assert (await _line(mock_db, "l2"))["state"] == "proposed"
    assert (await _get(mock_db))["status"] == "partly_applied"  # its applied line keeps its Undo


async def test_partly_applied_card_can_finish_with_no_newer_card(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l2"])
    assert (await _get(mock_db))["status"] == "applied" and len(meta.writes) == 2


async def test_queued_line_on_a_partly_applied_card_with_a_newer_card_is_refused(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(state="queued", decision={"actor": "user", "by": "s", "source": "d"}),
                status="partly_applied")
    await _card(mock_db, agent, _id="c2", run_id="r2", created=datetime.utcnow())
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "superseded" and meta.writes == []


async def test_patch_still_cannot_edit_a_partly_applied_card(mock_db):
    agent = await _agent(mock_db)
    card = await _card(mock_db, agent, _ln(), status="partly_applied")
    assert await guarded_update(mock_db, card, {"next_steps": "x"}) is False
    assert await guarded_update(mock_db, card, {"next_steps": "x"}, statuses=("partly_applied",)) is True


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

async def test_undo_remove_and_window_expiry(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="dashboard")
    assert res["ok"] and res["outcome"] == "undone" and meta.writes[-1] == ("ad1", "ACTIVE")
    line = await _line(mock_db, "l1")
    assert line["state"] == "undone" and line["undo_decision"]["by"] == "sam"
    assert (await mock_db["meta_ad_state"].find_one({"ad_id": "ad1"}))["effective_status"] == "ACTIVE"
    assert await mock_db[ACTIVITY].count_documents({"kind": KIND_ACTION_UNDONE, "ref_id": "c1:l1"}) == 1
    # A second press does nothing.
    writes = len(meta.writes)
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "already"
    assert len(meta.writes) == writes
    # Past the window it is refused.
    past = datetime.utcnow() - UNDO_AUTO_WINDOW - timedelta(minutes=1)
    card = await _get(mock_db)
    card["lines"][1]["applied_at"] = past
    card["lines"][1]["undo_until"] = past + UNDO_AUTO_WINDOW
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines": card["lines"]}})
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l2", by="s", source="d")
    assert res["outcome"] == "expired" and len(meta.writes) == writes
    assert (await _line(mock_db, "l2"))["state"] == "applied"


async def test_undo_bring_back_pauses(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "spare", off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(kind="revive", ad="old"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"
    assert meta.writes == [("old", "ACTIVE"), ("old", "PAUSED")]


async def test_double_undo_writes_once(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    await asyncio.gather(*[undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
                           for _ in range(4)])
    assert meta.writes == [("ad1", "PAUSED"), ("ad1", "ACTIVE")]


async def test_two_undos_that_read_the_same_card_write_once(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    meta.write_delay = 0.01
    _both_read_first(monkeypatch)
    results = await asyncio.gather(*[undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
                                     for _ in range(2)])
    assert meta.writes == [("ad1", "PAUSED"), ("ad1", "ACTIVE")]
    assert sorted(r["outcome"] for r in results) == ["already", "undone"]


@pytest.mark.parametrize("state", ["proposed", "failed", "blocked", "queued"])
async def test_only_an_applied_line_can_be_undone(state, mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(state=state))
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "not_undoable" and meta.writes == []


async def test_undo_refused_after_the_client_moved(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    await claims.claim_clients(mock_db, USER, "another-agent", [GID], move=True)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "blocked" and "Another agent" in res["reason"]
    assert meta.writes == [("ad1", "PAUSED")]
    line = await _line(mock_db)
    assert line["state"] == "applied"


async def test_undo_allowed_when_agent_off_and_nobody_else_holds_the_client(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await claims.release_clients(mock_db, USER, agent["_id"])
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "undone" and meta.ads["ad1"]["status"] == "ACTIVE"


async def test_undo_refused_when_agent_off_and_another_agent_holds_the_client(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await claims.claim_clients(mock_db, USER, "another-agent", [GID], move=True)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "blocked" and "Another agent" in res["reason"] and len(meta.writes) == 1


async def test_undo_of_a_line_birdy_did_not_write_sends_nothing(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    meta.set("ad1", "PAUSED")  # someone else paused it, so Undo must not turn it on
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "undone" and meta.writes == []


async def test_undo_failure_keeps_the_line_applied(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    meta.fail_write = "Meta API error: nope"
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "failed" and "nope" in res["reason"]
    line = await _line(mock_db)
    assert line["state"] == "applied" and "nope" in line["undo_error"]
    meta.fail_write = None
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"


async def test_killed_undo_recovers_from_metas_status(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    meta.kill_after_write = True
    with pytest.raises(Killed):
        await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    meta.kill_after_write = False
    assert (await _line(mock_db))["state"] == "undoing"
    await _tick(mock_db, mock_mongo_client, minutes=10)
    assert (await _line(mock_db))["state"] == "undone"
    assert meta.writes == [("ad1", "PAUSED"), ("ad1", "ACTIVE")]
    assert await mock_db[ACTIVITY].count_documents({"kind": KIND_ACTION_UNDONE}) == 1


async def test_killed_undo_that_never_landed_goes_back_to_applied(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    meta.kill_before_write = True
    with pytest.raises(Killed):
        await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    meta.kill_before_write = False
    await _tick(mock_db, mock_mongo_client, minutes=10)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["undo_error"] and len(meta.writes) == 1


async def test_undo_of_remove_skips_the_live_limit(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "b", "c", "d")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    await _ads(mock_db, "e")  # something else went live since: Undo still restores what Birdy changed
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "undone" and meta.writes[-1] == ("ad1", "ACTIVE")


async def test_undo_of_bring_back_skips_the_pause_cap(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, guardrails={"max_pauses_per_client_day": 1})
    await _ads(mock_db, "ad1", "spare", off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(1), _ln(2, kind="revive", ad="old"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l2", by="s", source="d")
    assert res["outcome"] == "undone" and meta.ads["old"]["status"] == "PAUSED"


async def test_undo_of_bring_back_keeps_last_live_ad(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(kind="revive", ad="old"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "blocked" and "no live ads" in res["reason"] and len(meta.writes) == 1
    assert (await _line(mock_db))["state"] == "applied"


async def test_undo_marker_lost_sends_nothing(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    real = apply_mod.guarded_update

    async def marker_write_loses(db, card, fields, **kw):
        if any(ln.get("undo_write_started_at") for ln in fields.get("lines") or []):
            return False
        return await real(db, card, fields, **kw)

    monkeypatch.setattr(apply_mod, "guarded_update", marker_write_loses)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["ok"] is False and meta.writes == [("ad1", "PAUSED")]
    assert (await _line(mock_db))["state"] == "applied"


async def test_undo_contended_returns_a_retry_message(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    monkeypatch.setattr(apply_mod, "UNDO_LEASE_WAIT_S", 0.02)
    await mock_db[apply_mod.LEASES].insert_one({"_id": apply_mod._lease_key(agent["_id"], GID), "token": "other",
                                                "expires_at": datetime.utcnow() + timedelta(minutes=1)})
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "busy" and "moment" in res["reason"]
    assert (await _line(mock_db))["state"] == "applied" and len(meta.writes) == 1


# ---------------------------------------------------------------------------
# Dismiss
# ---------------------------------------------------------------------------

async def test_dismiss_closes_the_card_and_later_apply_is_refused(mock_db, mock_mongo_client, meta, monkeypatch):
    use_provider(monkeypatch, FakeProvider(reply='{"kind": "fact", "text": "Client said wait"}'))
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    res = await dismiss_card(mock_db, USER, "c1", by="sam", source="slack", reason="Client said wait")
    assert res["ok"] and res["outcome"] == "dismissed"
    card = await _get(mock_db)
    assert card["status"] == "dismissed" and card["dismiss_reason"] == "Client said wait"
    assert card["decided_by"]["by"] == "sam" and card["decided_at"]
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "refused"
    assert meta.writes == []
    assert (await dismiss_card(mock_db, USER, "c1", by="s", source="d"))["outcome"] == "already"


async def test_dismiss_is_busy_while_a_line_is_being_applied(mock_db):
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(state="applying"))
    res = await dismiss_card(mock_db, USER, "c1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "busy"
    assert (await _get(mock_db))["status"] == "open"


async def test_dismissing_a_partly_applied_card_keeps_its_undo(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert (await dismiss_card(mock_db, USER, "c1", by="s", source="d"))["outcome"] == "dismissed"
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l2"])
    assert res["outcome"] == "refused" and len(meta.writes) == 1
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"


async def test_a_fully_applied_card_cannot_be_dismissed(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    res = await dismiss_card(mock_db, USER, "c1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "already" and "undo" in res["reason"].lower()
    assert (await _get(mock_db))["status"] == "applied"


# ---------------------------------------------------------------------------
# Auto
# ---------------------------------------------------------------------------

async def test_mixed_modes_only_ask_lines_wait(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare", off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(1, mode="auto"), _ln(2, mode="ask"), _ln(3, mode="flag"),
                _ln(4, kind="revive", ad="old", mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == [("ad1", "PAUSED"), ("old", "ACTIVE")]
    line = await _line(mock_db, "l1")
    assert line["state"] == "applied" and line["decision"]["actor"] == "birdy"
    assert (await _line(mock_db, "l2"))["state"] == "proposed"
    assert (await _line(mock_db, "l3"))["state"] == "flagged"
    assert (await _get(mock_db))["status"] == "partly_applied"  # as soon as any line applies, Auto included
    rows = await _applied_rows(mock_db)
    assert {r["actor"] for r in rows} == {"birdy"}
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert (await _line(mock_db, "l2"))["state"] == "applied"
    assert (await _get(mock_db))["status"] == "applied"


@pytest.mark.parametrize("why", ["stale", "open_alert", "flagged_text", "ai_failed", "over_add_limit", "needs_file"])
async def test_auto_falls_back_to_ask(why, mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "true")
    agent = await _agent(mock_db, settings={"creative": {**DEFAULT_SETTINGS["creative"], "auto_max_adds": 1},
                                            "modes": {"remove": "auto", "revive": "auto", "add": "auto"}})
    await _ads(mock_db, "ad1", "spare")
    line, others, card_extra, run_extra = _ln(mode="auto"), [], {}, {}
    if why == "stale":
        card_extra = {"stale": True}
    elif why == "open_alert":
        run_extra = {"alerts": [{"client_group_id": GID, "code": "payment_issue", "severity": "critical"}]}
    elif why == "flagged_text":
        line["flags"] = [{"kind": "client_name", "note": "Mentions Acme"}]
    elif why == "ai_failed":
        card_extra = {"ai_failed": True}
    elif why == "over_add_limit":
        line = _ln(1, kind="add", ad="", mode="auto")
        others = [_ln(2, kind="add", ad="", mode="auto")]
    elif why == "needs_file":
        line = _ln(1, kind="add", ad="", mode="auto", needs_file=True)
    await _card(mock_db, agent, line, *others, **card_extra)
    run = _run(agent, **run_extra)
    await auto_apply_run(mock_db, mock_mongo_client, run, agent)
    assert meta.writes == []
    target = "l2" if why == "over_add_limit" else "l1"
    got = await _line(mock_db, target)
    assert got["mode"] == "ask" and got["auto_fallback"] == why and got["state"] == "proposed"
    if why == "over_add_limit":
        assert (await _line(mock_db, "l1"))["mode"] == "auto"  # within the limit: Task 12 posts it
    # It waits for a person, even when the reason has passed.
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"stale": False, "ai_failed": False}})
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == []


async def test_needs_file_state_also_falls_back(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "true")
    agent = await _agent(mock_db, settings={"modes": {"remove": "auto", "revive": "auto", "add": "auto"}})
    await _card(mock_db, agent, _ln(1, kind="add", ad="", mode="auto", state="needs_file"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    line = await _line(mock_db)
    assert line["mode"] == "ask" and line["auto_fallback"] == "needs_file" and line["state"] == "needs_file"


async def test_auto_stale_client_from_the_run_falls_back(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent, clients={GID: {"state": "done", "stale": True}}), agent)
    assert meta.writes == [] and (await _line(mock_db))["auto_fallback"] == "stale"


@pytest.mark.parametrize("code", ["prepaid_no_topups", "spend_data_late"])
async def test_info_alerts_do_not_stop_auto(code, mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    run = _run(agent, alerts=[{"client_group_id": GID, "code": code, "severity": "info"}])
    await auto_apply_run(mock_db, mock_mongo_client, run, agent)
    assert meta.writes == [("ad1", "PAUSED")]


async def test_another_clients_alert_does_not_stop_auto(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    run = _run(agent, alerts=[{"client_group_id": "other", "code": "payment_issue", "severity": "critical"}])
    await auto_apply_run(mock_db, mock_mongo_client, run, agent)
    assert meta.writes == [("ad1", "PAUSED")]


async def test_auto_waits_for_unread_health_then_acts(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    due = _run(agent, clients={GID: {"state": "done", "health_due": datetime.utcnow()}})
    await auto_apply_run(mock_db, mock_mongo_client, due, agent)
    line = await _line(mock_db)
    assert meta.writes == [] and line["mode"] == "auto" and line["state"] == "proposed"
    assert "auto_fallback" not in line
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == [("ad1", "PAUSED")]


async def test_auto_repeats_safely_and_acts_only_on_new_lines(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    run = _run(agent)
    await auto_apply_run(mock_db, mock_mongo_client, run, agent)
    await auto_apply_run(mock_db, mock_mongo_client, run, agent)
    assert meta.writes == [("ad1", "PAUSED")]
    await _card(mock_db, agent, _ln(1, ad="ad2", mode="auto"), _id="c2", gid="g2")
    run["clients"]["g2"] = {"state": "done"}
    await claims.claim_clients(mock_db, USER, agent["_id"], ["g2"])
    await mock_db["meta_ad_state"].insert_one({"client_group_id": "g2", "ad_id": "ad2", "adset_id": "s2",
                                               "effective_status": "ACTIVE"})
    await mock_db["meta_ad_state"].insert_one({"client_group_id": "g2", "ad_id": "x", "adset_id": "s2",
                                               "effective_status": "ACTIVE"})
    await auto_apply_run(mock_db, mock_mongo_client, run, agent)
    assert meta.writes == [("ad1", "PAUSED"), ("ad2", "PAUSED")]


@pytest.mark.parametrize("status", ["closing", "superseded", "dismissed", "flagged", "applied"])
async def test_auto_uses_an_allow_list_of_card_statuses(status, mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"), status=status)
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == [] and (await _line(mock_db))["state"] == "proposed"


async def test_auto_acts_on_a_partly_applied_card_unless_newer_exists(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1, ad="ad1", mode="auto"), status="partly_applied")
    await _card(mock_db, agent, _ln(1, ad="ad2", mode="auto"), _id="c9", run_id="r0", status="partly_applied",
                created=datetime.utcnow() - timedelta(days=3))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent, run_id="r0"), agent)
    assert meta.writes == []
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == [("ad1", "PAUSED")]


@pytest.mark.parametrize("field", ["kind", "reconnect", "agent_off"])
async def test_auto_does_nothing_for_a_preview_dead_token_or_agent_off(field, mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, status="paused" if field == "agent_off" else "active")
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    extra = {"kind": {"kind": "preview"}, "reconnect": {"reconnect": True}, "agent_off": {}}[field]
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent, **extra), agent)
    line = await _line(mock_db)
    assert meta.writes == [] and line["state"] == "proposed" and line["mode"] == "auto"


async def test_auto_turned_off_since_the_tick_started_is_seen(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == []


async def test_auto_past_its_deadline_leaves_the_rest_for_the_cron(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1, mode="auto"), _ln(2, mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent, deadline=time.monotonic() - 1)
    assert meta.writes == [] and (await _line(mock_db, "l1"))["state"] == "queued"
    await _tick(mock_db, mock_mongo_client)
    assert meta.writes == [("ad1", "PAUSED"), ("ad2", "PAUSED")]
    assert (await _line(mock_db, "l1"))["decision"]["actor"] == "birdy"


async def test_auto_guardrail_blocks_with_reason(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1")
    await _card(mock_db, agent, _ln(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == [] and (await _line(mock_db))["reason_code"] == "last_live_ad"


# ---------------------------------------------------------------------------
# The real runner
# ---------------------------------------------------------------------------

async def test_runner_auto_end_to_end(mock_db, mock_mongo_client, fakes, meta, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner, "auto_apply_run", auto_apply_run)
    agent = await _setup(mock_db, "g1")
    await mock_db[AGENTS].update_one({"_id": agent["_id"]},
                                     {"$set": {"settings.modes.remove": "auto"}})
    await mock_db["meta_ad_state"].update_one({"ad_id": "ad_g1"}, {"$set": {"adset_id": "s1"}})
    await mock_db["meta_ad_state"].insert_one({"client_group_id": "g1", "ad_id": "spare", "name": "Spare", "adset_id": "s1",
                                               "campaign_id": "c1", "effective_status": "ACTIVE", "creative": {}})
    for ad, spend, day in (("ad_g1", "120", "2026-11-02"), ("spare", "10", "2026-11-02"), ("spare", "10", "2026-11-01")):
        await mock_db[AD_DAYS].insert_one({"user_id": USER, "client_group_id": "g1", "ad_id": ad, "campaign_id": "c1",
                                           "ad_name": ad, "date_start": day,
                                           "insight_data": {"spend": spend,
                                                            "actions": [{"action_type": "lead", "value": "1"}]}})
    agent = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await mock_db[CARDS].find().to_list(None)
    assert meta.writes == [("ad_g1", "PAUSED")] and card["lines"][0]["state"] == "applied"
    assert card["status"] == "applied"


async def test_runner_passes_its_deadline_to_auto(mock_db, mock_mongo_client, fakes, monkeypatch):  # noqa: F811
    got = []

    async def auto(db, client, run, agent, *, deadline=None):
        got.append(deadline)

    monkeypatch.setattr(runner, "auto_apply_run", auto)
    agent = await _setup(mock_db, "g1")
    run = await runner.get_or_create_run(mock_db, agent, NOW)
    deadline = time.monotonic() + 100
    await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=deadline, now_utc=NOW)
    assert got == [deadline]


async def test_runner_card_is_flagged_on_another_clients_name(mock_db, mock_mongo_client, fakes):  # noqa: F811
    agent = await _setup(mock_db, "g1")
    await mock_db.client_groups.insert_one({"id": "g2", "user_id": USER, "name": "Glow Leeds"})
    await mock_db.client_groups.insert_one({"id": "g3", "user_id": "someone-else", "name": "Ad"})
    fakes.draft = {"add": ["Glow Leeds summer"], "remove": [], "revive": [], "additional_changes": "",
                   "thought_process": "Try this.", "next_steps": "", "flags": [], "failed": False}
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await mock_db[CARDS].find().to_list(None)
    assert [f["kind"] for f in card["lines"][0]["flags"]] == ["client_name"]
    assert "Glow Leeds" in card["lines"][0]["flags"][0]["note"]


# ---------------------------------------------------------------------------
# The text check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,names,own,hits", [
    ("GLOW leeds summer offer", ["Glow Leeds"], "Acme", ["Glow Leeds"]),
    ("Glow  Leeds", ["Glow Leeds"], "Acme", ["Glow Leeds"]),               # any run of spaces
    ("Glowing Leedsville", ["Glow Leeds"], "Acme", []),                    # whole words only
    ("Glow", ["Glow Leeds"], "Acme", []),
    ("Acme summer", ["Acme"], "Acme", []),                                 # its own name
    ("acme summer", ["ACME "], "Acme", []),
    ("Acme London promo", ["Acme"], "Acme London", []),                    # its own longer name
    ("Acme London promo", ["Acme London"], "Acme", ["Acme London"]),       # another's longer name
    ("Dr. Smith (Leeds) promo", ["Dr. Smith (Leeds)"], "Acme", ["Dr. Smith (Leeds)"]),  # regex characters
    ("C&A sale", ["C&A"], "Acme", ["C&A"]),
    ("UK leads v2", ["UK"], "Acme", []),                                   # under 3 characters
    ("Go big video", ["Go"], "Acme", []),
    ("Clínica Sol oferta", ["Clínica Sol"], "Acme", ["Clínica Sol"]),
    ("anything", ["", None, "  ", "A"], "Acme", []),                       # nothing to match on
    ("Glow Glow", ["Glow", "glow"], "Acme", ["Glow"]),                     # one flag per name
    ("x" * 5000 + " Glow", ["(a+)+$", "Glow"], "Acme", ["Glow"]),          # a name is never a pattern
])
def test_client_name_flags(text, names, own, hits):
    flags = client_name_flags(text, names, own)
    assert [f["kind"] for f in flags] == ["client_name"] * len(hits)
    for flag, hit in zip(flags, hits):
        assert hit in flag["note"]


def test_build_card_checks_add_text_and_keeps_ai_flags():
    draft = {"add": ["src:a1"], "remove": ["o1"], "revive": [], "additional_changes": "", "thought_process": "t",
             "next_steps": "n", "flags": [{"id": "src:a1", "kind": "city", "note": "Says Leeds"}], "failed": False}
    candidates = {"add": [{"candidate_id": "src:a1", "ad_id": "a1", "ad_name": "Summer", "source_name": "Beta Clinic",
                           "creative": {"title": "New at Beta Clinic", "body": None}}],
                  "remove": [{"ad_id": "o1", "ad_name": "Beta Clinic old"}], "revive": []}
    card = build_card(FACTS, "propose", draft, DEFAULT_SETTINGS, "r1", candidates=candidates,
                      client_names=["Acme", "Beta Clinic"])
    add, remove = card["lines"]
    assert [f["kind"] for f in add["flags"]] == ["city", "client_name"]
    assert remove["flags"] == []  # an ad in the client's own account imports nobody's branding


def test_build_card_source_name_alone_is_not_a_match():
    """Every Add comes from another client, so the source's name on the line isn't ad text."""
    draft = {"add": ["src:a1"], "remove": [], "revive": [], "additional_changes": "", "thought_process": "t",
             "next_steps": "n", "flags": [], "failed": False}
    candidates = {"add": [{"candidate_id": "src:a1", "ad_id": "a1", "ad_name": "Summer", "source_name": "Beta Clinic",
                           "creative": {"title": "Summer offer"}}], "remove": [], "revive": []}
    card = build_card(FACTS, "propose", draft, DEFAULT_SETTINGS, "r1", candidates=candidates,
                      client_names=["Beta Clinic"])
    assert card["lines"][0]["flags"] == []


# ---------------------------------------------------------------------------
# Apply all and the queue
# ---------------------------------------------------------------------------

async def _three_cards(db, agent):
    for n, gid in enumerate(("g1", "g2", "g3")):
        await claims.claim_clients(db, USER, agent["_id"], [gid])
        await _ads(db, f"ad{gid}", f"spare{gid}", gid=gid)
        await _card(db, agent, _ln(1, ad=f"ad{gid}"), _id=f"c{n}", gid=gid, slack={"channel_id": "C1", "ts": f"1.{n}"})
    await db[RUNS].insert_one(_run(agent, clients={g: {"state": "done"} for g in ("g1", "g2", "g3")}))


async def test_apply_run_queues_past_budget_and_cron_finishes(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, gids=("g1", "g2", "g3"))
    await _three_cards(mock_db, agent)
    res = await apply_run(mock_db, mock_mongo_client, USER, "r1", by="sam", source="slack", budget_s=0)
    assert res["ok"] and res["outcome"] == "queued" and meta.writes == []
    await _tick(mock_db, mock_mongo_client)
    assert sorted(meta.writes) == [("adg1", "PAUSED"), ("adg2", "PAUSED"), ("adg3", "PAUSED")]
    for n in range(3):
        line = await _line(mock_db, "l1", f"c{n}")
        assert line["state"] == "applied" and line["decision"]["by"] == "sam"
    assert (await mock_db[RUNS].find_one({"_id": "r1"}))["apply_all"]["pending"] is False


async def test_apply_run_inside_budget_finishes_now(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, gids=("g1", "g2", "g3"))
    await _three_cards(mock_db, agent)
    res = await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="d", budget_s=5)
    assert res["outcome"] == "applied" and res["applied"] == 3 and len(meta.writes) == 3
    assert (await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="d", budget_s=5))["outcome"] == "noop"
    assert len(meta.writes) == 3


async def test_apply_run_returns_at_budget_when_meta_hangs_and_nothing_is_sent_twice(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setattr(apply_mod, "MIN_TIME_FOR_LINE_S", 0)
    agent = await _agent(mock_db, gids=("g1", "g2", "g3"))
    await _three_cards(mock_db, agent)
    meta.write_delay = 0.3
    started = time.monotonic()
    res = await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="slack", budget_s=0.1)
    assert time.monotonic() - started < 0.29
    assert res["outcome"] == "queued"
    states = sorted([(await _line(mock_db, "l1", f"c{n}"))["state"] for n in range(3)])
    assert states == ["applying", "queued", "queued"]  # cut off mid-send: the send may or may not have landed
    meta.write_delay = 0
    await _tick(mock_db, mock_mongo_client)  # the queue moves on at once
    await _tick(mock_db, mock_mongo_client, minutes=10)  # and the cut-off line is read, never re-sent
    states = [(await _line(mock_db, "l1", f"c{n}"))["state"] for n in range(3)]
    assert states.count("applied") == 2 and states.count("failed") == 1
    assert len(meta.writes) == 2


async def test_apply_run_cut_off_before_the_send_goes_back_in_the_queue(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, gids=("g1", "g2", "g3"))
    await _three_cards(mock_db, agent)
    real = meta.state

    async def slow_read(*a):
        await asyncio.sleep(0.3)
        return await real(*a)

    apply_mod.get_object_state = slow_read
    try:
        await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="slack", budget_s=0.05)
    finally:
        apply_mod.get_object_state = meta.state
    states = [(await _line(mock_db, "l1", f"c{n}"))["state"] for n in range(3)]
    assert states == ["queued", "queued", "queued"]
    await _tick(mock_db, mock_mongo_client)
    assert len(meta.writes) == 3


async def test_apply_all_only_covers_cards_shown_when_it_was_pressed(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, gids=("g1", "g2", "g3"))
    await _three_cards(mock_db, agent)
    await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="slack", budget_s=0)
    await _ads(mock_db, "late", "sparelate", gid="g4")
    await claims.claim_clients(mock_db, USER, agent["_id"], ["g4"])
    await _card(mock_db, agent, _ln(1, ad="late"), _id="c9", gid="g4", created=datetime.utcnow() + timedelta(seconds=5))
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db, "l1", "c9"))["state"] == "proposed" and len(meta.writes) == 3


async def test_apply_run_skips_cards_it_may_not_touch(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "ad3", "ad4", "spare")
    await mock_db[RUNS].insert_one(_run(agent))
    await _card(mock_db, agent, _ln(1, ad="ad1"), _id="ok")
    await _card(mock_db, agent, _ln(1, ad="ad2"), _id="closed", status="superseded", gid="gx")
    await _card(mock_db, agent, _ln(1, ad="ad3"), _id="closing", status="closing", gid="gy")
    await _card(mock_db, agent, _ln(1, ad="ad4", mode="flag"), _id="flagged", status="flagged", gid="gz")
    res = await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="d", budget_s=5)
    assert res["applied"] == 1 and meta.writes == [("ad1", "PAUSED")]


async def test_apply_run_refuses_a_preview_and_a_stranger(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await mock_db[RUNS].insert_one(_run(agent, kind="preview"))
    assert (await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="d"))["outcome"] == "refused"
    assert (await apply_run(mock_db, mock_mongo_client, "u2", "r1", by="s", source="d"))["outcome"] == "not_found"


async def test_apply_card_with_a_budget_leaves_the_rest_for_the_cron(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    assert res["outcome"] == "queued" and meta.writes == []
    await _tick(mock_db, mock_mongo_client)
    assert len(meta.writes) == 2 and (await _get(mock_db))["status"] == "applied"


async def test_one_line_raising_does_not_stop_the_others(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    real = apply_mod.check_guardrails
    calls = []

    async def first_raises(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return await real(*a, **kw)

    monkeypatch.setattr(apply_mod, "check_guardrails", first_raises)
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert (await _line(mock_db, "l1"))["state"] == "failed" and meta.writes == [("ad2", "PAUSED")]
    assert "boom" not in (await _line(mock_db, "l1"))["error"]


async def test_cron_stops_at_its_deadline(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(state="queued", decision={"actor": "user", "by": "s", "source": "d"}))
    await process_queued_lines(mock_db, mock_mongo_client, _later(), deadline=time.monotonic() - 1)
    assert meta.writes == [] and (await _line(mock_db))["state"] == "queued"


async def test_creatives_tick_runs_the_queue(mock_db, mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    calls = []

    async def noop_posts(db, client, now, *, deadline):
        return {}

    async def noop_track(db, client, now, *, deadline):
        return {}

    async def queue(db, client, now, *, deadline):
        calls.append(deadline)
        return {"processed": 1}

    async def reconcile(db):
        return {}

    async def cleanup(db, now):
        return {}

    monkeypatch.setattr(creative_tracking, "get_mongo_client", fake_client)
    monkeypatch.setattr(creative_tracking, "run_post_jobs", noop_posts)
    monkeypatch.setattr(creative_tracking, "track_due_tests", noop_track)
    monkeypatch.setattr(creative_tracking, "process_queued_lines", queue)
    monkeypatch.setattr(creative_tracking, "reconcile_all", reconcile)
    monkeypatch.setattr(creative_tracking, "cleanup_archived_media", cleanup)
    result = await creative_tracking.run_creatives_tick()
    assert calls and result["stock_lines"] == {"processed": 1}


async def test_creatives_tick_survives_a_queue_failure(mock_db, mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    tracked = []

    async def noop_posts(db, client, now, *, deadline):
        return {}

    async def track(db, client, now, *, deadline):
        tracked.append(1)
        return {}

    async def queue(db, client, now, *, deadline):
        raise RuntimeError("queue down")

    async def reconcile(db):
        return {}

    async def cleanup(db, now):
        return {}

    monkeypatch.setattr(creative_tracking, "get_mongo_client", fake_client)
    monkeypatch.setattr(creative_tracking, "run_post_jobs", noop_posts)
    monkeypatch.setattr(creative_tracking, "track_due_tests", track)
    monkeypatch.setattr(creative_tracking, "process_queued_lines", queue)
    monkeypatch.setattr(creative_tracking, "reconcile_all", reconcile)
    monkeypatch.setattr(creative_tracking, "cleanup_archived_media", cleanup)
    result = await creative_tracking.run_creatives_tick()
    assert tracked and "queue down" in result["stock_lines"]["error"]


async def test_indexes_cover_the_queue_queries(mock_mongo_client, mock_db):
    await create_agent_indexes(mock_mongo_client)
    names = await mock_db[CARDS].index_information()
    assert any(ix["key"] == [("lines.state", 1)] for ix in names.values())


# ---------------------------------------------------------------------------
# Review fixes: live reads, the lease, modes at write time, budgets
# ---------------------------------------------------------------------------

async def test_cron_path_respects_daily_cap(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, guardrails={"max_pauses_per_client_day": 3})
    await _ads(mock_db, "ad1", "ad2", "ad3", "ad4", "ad5", "spare")
    await _card(mock_db, agent, *[_ln(i) for i in range(1, 6)])
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    frozen = _later()  # the tick's clock stops here: rows logged during the tick are later than it
    await asyncio.sleep(0.01)
    await process_queued_lines(mock_db, mock_mongo_client, frozen, deadline=time.monotonic() + 30)
    assert len(meta.writes) == 3
    states = [ln.get("reason_code") for ln in (await _get(mock_db))["lines"]]
    assert states.count("pause_cap") == 2


async def test_live_read_refuses_the_last_live_ad_the_mirror_got_wrong(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    meta.set("spare", "PAUSED")  # mirror says live, Meta says not
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == [] and meta.adset_reads == ["s1"]
    assert (await _line(mock_db))["reason_code"] == "last_live_ad"


async def test_live_says_a_spare_exists_but_birdy_just_paused_it(mock_db, mock_mongo_client, meta):
    """Meta and the mirror disagree on the spare: a read straight after a write can lag, so refuse."""
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", off=["spare"])
    await mock_db["meta_ad_state"].update_one({"ad_id": "spare"}, {"$set": {"updated_at": datetime.utcnow()}})
    meta.set("spare", "ACTIVE")
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == []


async def test_mirror_alone_says_last_live_but_meta_has_another(mock_db, mock_mongo_client, meta, monkeypatch):
    """A new ad the mirror hasn't seen yet: the live list decides."""
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1")
    real = meta.adset_ads

    async def with_new_ad(*a):
        return await real(*a) + [{"id": "brand_new", "name": "New", "effective_status": "ACTIVE"}]

    monkeypatch.setattr(meta_state, "fetch_adset_ads", with_new_ad)
    await _card(mock_db, agent, _ln())
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "applied"


@pytest.mark.parametrize("kind,ad", [("remove", "ad1"), ("revive", "old")])
async def test_live_read_failing_refuses_every_write(kind, ad, mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare", off=["old"])
    meta.set("old", "PAUSED")

    async def boom(*a, **k):
        raise RuntimeError("Graph down")

    monkeypatch.setattr(meta_state, "fetch_adset_ads", boom)
    await _card(mock_db, agent, _ln(kind=kind, ad=ad))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == [] and "Couldn't check" in res["reason"]
    assert (await _line(mock_db))["reason_code"] == "live_check_failed"


async def test_ad_with_no_known_ad_set_is_refused(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await mock_db["meta_ad_state"].insert_one({"client_group_id": GID, "ad_id": "ad1", "name": "x",
                                               "effective_status": "ACTIVE"})
    await _card(mock_db, agent, _ln(), _ln(2, ad="unmirrored"))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == [] and meta.adset_reads == []
    assert {ln["reason_code"] for ln in (await _get(mock_db))["lines"]} == {"no_adset"}


async def test_protected_tag_on_the_live_name_blocks(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    meta.names["ad1"] = "Now Evergreen"  # renamed in Meta since the refresh
    await _card(mock_db, agent, _ln())
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == []
    assert (await _line(mock_db))["reason_code"] == "protected_tag"


async def test_a_protected_neighbour_does_not_stop_changing_another_ad(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    meta.names["spare"] = "Evergreen hero"
    await _card(mock_db, agent, _ln())
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "applied"


async def test_taught_exception_is_checked_on_the_target_not_its_neighbours(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    meta.names["ad1"] = "Spring hero"
    await mock_db["agent_memory"].insert_one({"_id": "m", "agent_id": agent["_id"], "text": "Leave Spring alone",
                                              "active": True, "filter": {"ad_name_contains": "spring"}})
    await _card(mock_db, agent, _ln(1), _ln(2))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert meta.writes == [("ad2", "PAUSED")]
    line = await _line(mock_db, "l1")
    assert line["reason_code"] == "exception" and "Spring" in line["error"]


async def test_custom_agents_ad_set_targets_still_check_every_ad(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    meta.names["spare"] = "Evergreen hero"
    doc = {"action": {"type": "pause_ads", "targets": [{"object_id": "s1", "object_type": "adset"}]},
           "client_group_id": GID}
    g = await guardrails.check_adset_contents_live(mock_db, mock_mongo_client, agent, doc, memories=[],
                                                   now_utc=datetime.utcnow())
    assert g.ok is False and g.reason == "protected_tag"


async def test_two_requests_never_empty_an_ad_set(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2")
    await _card(mock_db, agent, _ln(1), _ln(2))
    real = meta.state

    async def graph_latency(*a):
        await asyncio.sleep(0.02)
        return await real(*a)

    monkeypatch.setattr(apply_mod, "get_object_state", graph_latency)
    results = await asyncio.gather(
        apply_card(mock_db, mock_mongo_client, USER, "c1", by="a", source="d", line_ids=["l1"]),
        apply_card(mock_db, mock_mongo_client, USER, "c1", by="b", source="d", line_ids=["l2"]))
    assert sorted(r["outcome"] for r in results) == ["applied", "queued"]  # the loser waits for the cron
    assert len(meta.writes) == 1
    await _tick(mock_db, mock_mongo_client)
    assert len(meta.writes) == 1
    assert await mock_db["meta_ad_state"].count_documents({"adset_id": "s1", "effective_status": "ACTIVE"}) == 1
    assert {(await _line(mock_db, "l1"))["state"], (await _line(mock_db, "l2"))["state"]} == {"applied", "blocked"}


async def test_apply_all_and_the_cron_never_empty_an_ad_set(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await mock_db[RUNS].insert_one(_run(agent))
    real = meta.state

    async def graph_latency(*a):
        await asyncio.sleep(0.02)
        return await real(*a)

    monkeypatch.setattr(apply_mod, "get_object_state", graph_latency)

    async def cron_soon():
        await asyncio.sleep(0.005)
        return await process_queued_lines(mock_db, mock_mongo_client, _later(), deadline=time.monotonic() + 5)

    await asyncio.gather(apply_run(mock_db, mock_mongo_client, USER, "r1", by="a", source="d", budget_s=5), cron_soon())
    await _tick(mock_db, mock_mongo_client)
    assert await mock_db["meta_ad_state"].count_documents({"adset_id": "s1", "effective_status": "ACTIVE"}) == 1


async def test_a_held_lease_leaves_the_line_queued_and_expires(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await mock_db[apply_mod.LEASES].insert_one({"_id": apply_mod._lease_key(agent["_id"], GID), "token": "crashed",
                                                "expires_at": datetime.utcnow() + timedelta(milliseconds=50)})
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "queued" and meta.reads == [] and (await _line(mock_db))["state"] == "queued"
    await asyncio.sleep(0.06)  # the invocation that held it was killed: it frees itself
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied"


async def test_lease_is_per_client_and_released_after_each_line(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await mock_db[apply_mod.LEASES].insert_one({"_id": apply_mod._lease_key("another-agent", GID), "token": "x",
                                                "expires_at": datetime.utcnow() + timedelta(minutes=1)})
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "applied"
    assert await mock_db[apply_mod.LEASES].count_documents({"_id": apply_mod._lease_key(agent["_id"], GID)}) == 0


async def test_lease_released_when_the_budget_cuts_a_line_off(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    meta.write_delay = 0.3
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0.05)
    assert await mock_db[apply_mod.LEASES].count_documents({"_id": apply_mod._lease_key(agent["_id"], GID)}) == 0


async def test_auto_stops_when_the_mode_became_flag(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.clients.g1": {"modes": {"remove": "flag"}}}})
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    line = await _line(mock_db)
    assert meta.writes == [] and line["state"] == "flagged" and line["mode"] == "flag"
    assert (await _get(mock_db))["status"] == "flagged"


async def test_auto_stops_when_the_mode_became_ask(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.modes.remove": "ask"}})
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    line = await _line(mock_db)
    assert meta.writes == [] and line["state"] == "proposed" and line["mode"] == "ask"
    # A person can still say yes.
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "applied"


async def test_auto_line_waiting_in_the_queue_rechecks_its_mode(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent, deadline=time.monotonic() - 1)
    assert (await _line(mock_db))["state"] == "queued"
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.modes.remove": "ask"}})
    await _tick(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert meta.writes == [] and line["state"] == "proposed" and line["mode"] == "ask"


async def test_ask_line_now_flag_writes_nothing(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="ask"))
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.modes.remove": "flag"}})
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "flagged" and res["ok"] and "Flag" in res["reason"] and meta.writes == []
    assert (await _line(mock_db))["state"] == "flagged"


async def test_ask_line_is_not_auto_run_when_the_mode_became_auto(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(mode="ask"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert meta.writes == [] and (await _line(mock_db))["state"] == "proposed"


async def test_supersede_bumps_the_version_so_a_line_write_cannot_reverse_it(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    real = apply_mod.guarded_update
    done = []

    async def supersede_between_read_and_write(db, card, fields, **kw):
        if not done:
            done.append(1)
            await _card(mock_db, agent, _id="c2", run_id="r2", created=datetime.utcnow())
            await supersede_open(db, agent["_id"], GID, "r2")
        return await real(db, card, fields, **kw)

    monkeypatch.setattr(apply_mod, "guarded_update", supersede_between_read_and_write)
    await _tick(mock_db, mock_mongo_client)
    card = await _get(mock_db)
    assert card["status"] == "superseded" and card["version"] >= 2 and meta.writes == []
    assert card["lines"][0]["reason_code"] == "superseded"


async def test_timed_out_write_that_lands_late_is_applied_with_its_row(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())

    async def timed_out(mc, user, action):
        meta.writes.append(("ad1", "PAUSED"))
        return {"succeeded": [], "failed": [{"object_id": "ad1", "error": "Failed to reach Meta: timed out"}]}

    monkeypatch.setattr(apply_mod, "execute_action", timed_out)
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "queued" and (await _line(mock_db))["state"] == "applying"  # no false failed
    monkeypatch.setattr(apply_mod, "execute_action", meta.execute)
    meta.set("ad1", "PAUSED")  # it lands a moment later
    await _tick(mock_db, mock_mongo_client, minutes=7)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["wrote"] is True
    [row] = await _applied_rows(mock_db)
    assert row["applied_action_type"] == ACTION_PAUSE_ADS
    meta.writes.clear()
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"
    assert meta.writes == [("ad1", "ACTIVE")]


async def test_retry_after_a_late_landing_counts_as_birdys_write(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _kill_after_write_then_unlanded(mock_db, mock_mongo_client, meta, agent)
    meta.set("ad1", "PAUSED")  # recovery said not landed, then it landed
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert res["outcome"] == "applied" and (await _line(mock_db))["wrote"] is True
    [row] = await _applied_rows(mock_db)
    assert row["applied_action_type"] == ACTION_PAUSE_ADS


async def _kill_after_write_then_unlanded(db, client, meta, agent):
    await _ads(db, "ad1", "spare")
    await _card(db, agent, _ln())
    meta.kill_before_write = True
    with pytest.raises(Killed):
        await apply_card(db, client, USER, "c1", by="s", source="d")
    meta.kill_before_write = False
    await _tick(db, client, minutes=7)
    assert (await _line(db))["state"] == "failed"


async def test_undo_window_runs_from_the_real_write(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _kill_after_write(mock_db, mock_mongo_client, meta, agent)
    await _tick(mock_db, mock_mongo_client, minutes=90)
    line = await _line(mock_db)
    assert line["state"] == "applied"
    assert line["applied_at"] == line["write_started_at"]
    assert line["undo_until"] - line["write_started_at"] == UNDO_AUTO_WINDOW


async def test_a_line_with_two_ads_is_refused(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "a", "b", "spare")
    await _card(mock_db, agent, {**_ln(), "meta_ids": ["a", "b"]})
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == []
    assert (await _line(mock_db))["reason_code"] == "several_ads"


async def test_stuck_lines_recover_oldest_first_and_a_hung_read_does_not_starve_the_queue(
        mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "ad3", "spare")
    old = datetime.utcnow() - timedelta(hours=1)
    stuck = {"state": "applying", "applying_at": old, "write_started_at": old, "decision": {"actor": "user"}}
    await _card(mock_db, agent, _ln(1, ad="ad1", **stuck), _id="young", updated_at=old + timedelta(minutes=5))
    await _card(mock_db, agent, _ln(1, ad="ad2", **stuck), _id="eldest", run_id="r0", updated_at=old)
    await _card(mock_db, agent, _ln(1, ad="ad3", state="queued", decision={"actor": "user"}), _id="waiting", run_id="r3",
                gid=GID)
    seen = []
    real = meta.state

    async def hang_on_stuck(user, ad, kind, client):
        seen.append(ad)
        if ad in ("ad1", "ad2"):
            await asyncio.sleep(5)
        return await real(user, ad, kind, client)

    monkeypatch.setattr(apply_mod, "get_object_state", hang_on_stuck)
    monkeypatch.setattr(apply_mod, "RECOVERY_READ_TIMEOUT_S", 0.01)
    await process_queued_lines(mock_db, mock_mongo_client, _later(), deadline=time.monotonic() + 5)
    assert seen[:2] == ["ad2", "ad1"]
    assert (await _line(mock_db, "l1", "waiting"))["state"] == "applied"
    assert (await _line(mock_db, "l1", "eldest"))["state"] == "applying"  # still can't confirm


async def test_creatives_tick_has_a_hard_budget_and_a_cut_line_recovers(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setattr(apply_mod, "MIN_TIME_FOR_LINE_S", 0)  # the backstop, which normal running doesn't reach
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    meta.write_delay = 0.5  # stands for a 30 s Graph timeout

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    seen = {}

    async def noop_posts(db, c, now, *, deadline):
        return {}

    async def track(db, c, now, *, deadline):
        seen["left"] = deadline - time.monotonic()
        return {}

    async def noop(*a, **k):
        return {}

    monkeypatch.setattr(creative_tracking, "get_mongo_client", fake_client)
    monkeypatch.setattr(creative_tracking, "run_post_jobs", noop_posts)
    monkeypatch.setattr(creative_tracking, "track_due_tests", track)
    monkeypatch.setattr(creative_tracking, "reconcile_all", noop)
    monkeypatch.setattr(creative_tracking, "cleanup_archived_media", noop)
    monkeypatch.setattr(creative_tracking, "TICK_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(creative_tracking, "STOCK_LINES_BUDGET_SECONDS", 0.1)
    result = await creative_tracking.run_creatives_tick()
    assert seen["left"] > 0.1 and result["stock_lines"] == {"timed_out": True}
    line = await _line(mock_db)
    assert line["state"] == "applying" and line["write_started_at"]  # cut off mid-send
    meta.write_delay = 0
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "failed" and meta.writes == []


async def test_card_with_all_lines_flagged_by_a_retry_is_no_longer_applicable(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(), _ln(2, mode="flag"))
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.modes.remove": "flag"}})
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert (await _get(mock_db))["status"] == "flagged"


async def test_candidates_and_facts_carry_ad_sets(mock_db):
    window = Window(start=date(2026, 10, 27), end=date(2026, 11, 2), prev_start=date(2026, 10, 20),
                    prev_end=date(2026, 10, 26))
    agent = await _agent(mock_db)
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"ad_account_currency": "GBP"}})
    await _ads(mock_db, "a", "b", adset="s1")
    await _ads(mock_db, "c", "d", "e", adset="s2", off=["z"])
    await mock_db[AD_DAYS].insert_many([
        {"user_id": USER, "client_group_id": GID, "ad_id": "a", "campaign_id": "c1", "ad_name": "a",
         "date_start": "2026-11-01", "insight_data": {"spend": "90", "actions": []}},
        {"user_id": USER, "client_group_id": GID, "ad_id": "c", "campaign_id": "c1", "ad_name": "c",
         "date_start": "2026-11-01", "insight_data": {"spend": "10", "actions": []}}])
    group = await mock_db.client_groups.find_one({"id": GID})
    facts = await client_facts(mock_db, agent, group, window)
    assert facts["ads_live_by_adset"] == {"s1": 2, "s2": 3} and facts["ads_live"] == 5
    assert facts["main_adset_id"] == "s1"  # most spend, though s2 has more ads
    cands = await build_candidates(mock_db, agent, group, window)
    assert {c["ad_id"]: c["adset_id"] for c in cands["remove"]}["c"] == "s2"


# ---------------------------------------------------------------------------
# Second review: budgets, unconfirmed sends, the lease per client, stale mirror rows
# ---------------------------------------------------------------------------

def _inflight(meta, *, land=True, reply_delay=0.3):
    """A POST that reaches Meta (or doesn't) straight away, and whose reply is slow enough to be cut."""
    async def execute(mc, user, action):
        ad = action["targets"][0]["object_id"]
        desired = "PAUSED" if action["type"] == "pause_ads" else "ACTIVE"
        meta.writes.append((ad, desired))
        if land:
            meta.set(ad, desired)
        await asyncio.sleep(reply_delay)
        return {"succeeded": [ad], "failed": []}
    return execute


def _lost_send(meta):
    """Graph times out and the write isn't visible yet."""
    async def execute(mc, user, action):
        ad = action["targets"][0]["object_id"]
        meta.writes.append((ad, "PAUSED"))
        return {"succeeded": [], "failed": [{"object_id": ad, "error": "Failed to reach Meta: timed out"}]}
    return execute


async def test_cron_starts_no_line_without_time_to_finish_it(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    res = await process_queued_lines(mock_db, mock_mongo_client, _later(), deadline=time.monotonic() + 3.9)
    assert res["processed"] == 0 and meta.reads == [] and (await _line(mock_db))["state"] == "queued"
    res = await process_queued_lines(mock_db, mock_mongo_client, _later(), deadline=time.monotonic() + 4.5)
    assert res["processed"] == 1 and (await _line(mock_db))["state"] == "applied"


async def test_apply_all_at_its_real_budget_queues_every_line_and_cuts_none(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2), slack={"channel_id": "C1", "ts": "1.1"})
    await mock_db[RUNS].insert_one(_run(agent))
    res = await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="slack")
    assert res["outcome"] == "queued" and res["queued"] == 2 and meta.writes == [] and meta.adset_reads == []
    await _tick(mock_db, mock_mongo_client)
    assert len(meta.writes) == 2


async def test_a_line_cut_mid_send_is_read_on_the_next_tick(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setattr(apply_mod, "execute_action", _inflight(meta))
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0.05)
    line = await _line(mock_db)
    assert line["state"] == "applying" and line["cut_at"] and meta.writes == [("ad1", "PAUSED")]
    await _tick(mock_db, mock_mongo_client)  # no STUCK_AFTER wait
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["wrote"] is True and len(await _applied_rows(mock_db)) == 1
    assert len(meta.writes) == 1
    monkeypatch.setattr(apply_mod, "execute_action", meta.execute)
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"
    assert meta.ads["ad1"]["status"] == "ACTIVE"


async def test_a_retry_of_a_cut_line_is_not_read_as_cut_while_it_is_alive(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setattr(apply_mod, "execute_action", _inflight(meta, land=False))
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0.05)
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "failed"  # the read says it hasn't landed
    monkeypatch.setattr(apply_mod, "execute_action", _lost_send(meta))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    line = await _line(mock_db)
    assert line["state"] == "applying" and not line.get("cut_at")
    await _tick(mock_db, mock_mongo_client)  # too soon for this try
    assert (await _line(mock_db))["state"] == "applying"


async def test_lost_sends_count_toward_the_pause_cap(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setattr(apply_mod, "execute_action", _lost_send(meta))
    agent = await _agent(mock_db, guardrails={"max_pauses_per_client_day": 2})
    await _ads(mock_db, "a1", "a2", "a3", "a4", "spare")
    await _card(mock_db, agent, *[_ln(i, ad=f"a{i}") for i in range(1, 5)])
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert len(meta.writes) == 2 and await _applied_rows(mock_db) == []
    assert [ln.get("reason_code") for ln in (await _get(mock_db))["lines"]] == [None, None, "pause_cap", "pause_cap"]


async def test_cut_sends_count_toward_the_pause_cap_across_ticks(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setattr(apply_mod, "MIN_TIME_FOR_LINE_S", 0)
    monkeypatch.setattr(apply_mod, "execute_action", _inflight(meta, land=False))
    monkeypatch.setattr(apply_mod, "_recover_stuck", _no_recovery)
    agent = await _agent(mock_db, guardrails={"max_pauses_per_client_day": 3})
    ads = [f"a{i}" for i in range(1, 7)]
    await _ads(mock_db, *ads, "spare")
    await _card(mock_db, agent, *[_ln(i, ad=a) for i, a in enumerate(ads, 1)])
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    for _ in range(6):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process_queued_lines(mock_db, mock_mongo_client, _later(),
                                                        deadline=time.monotonic() + 0.05), 0.05)
    assert len(meta.writes) == 3


async def _no_recovery(*a):
    return 0


async def test_a_line_with_its_row_logged_counts_once(mock_db, mock_mongo_client, meta):
    """Killed between writing the activity row and marking the line."""
    agent = await _agent(mock_db, guardrails={"max_pauses_per_client_day": 2})
    await _ads(mock_db, "a1", "a2", "spare")
    now = datetime.utcnow()
    logged = _ln(1, ad="a1", state="applying", write_started_at=now, applying_at=now, decision={"actor": "user"})
    done = await _card(mock_db, agent, logged, _id="c0", gid=GID, run_id="r0")
    await apply_mod._log_once(mock_db, done, logged, KIND_ACTION_APPLIED, label="x", title="x", decision={"actor": "user"},
                              action_type=ACTION_PAUSE_ADS)
    await _card(mock_db, agent, _ln(2, ad="a2"), created=now)
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert res["outcome"] == "applied" and meta.writes == [("a2", "PAUSED")]


async def test_a_retry_does_not_count_its_own_old_send(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, guardrails={"max_pauses_per_client_day": 1})
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(state="failed", write_started_at=datetime.utcnow() - timedelta(hours=1)))
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert res["outcome"] == "applied" and meta.writes == [("ad1", "PAUSED")]


async def test_an_unconfirmed_pause_counts_as_off_for_the_last_live_ad(mock_db, mock_mongo_client, meta, monkeypatch):
    monkeypatch.setattr(apply_mod, "execute_action", _lost_send(meta))
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    assert meta.writes == [("ad1", "PAUSED")]
    assert (await _line(mock_db, "l2"))["reason_code"] == "last_live_ad"


async def test_an_unconfirmed_undo_of_bring_back_counts_as_off(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", off=["old"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(1, kind="revive", ad="old"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    monkeypatch.setattr(apply_mod, "execute_action", _lost_send(meta))
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "queued"
    monkeypatch.setattr(apply_mod, "execute_action", meta.execute)
    await _card(mock_db, agent, _ln(1), _id="c2", created=datetime.utcnow() - timedelta(minutes=30), run_id="r2")
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"created_at": datetime.utcnow() - timedelta(hours=2)}})
    res = await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert res["outcome"] == "blocked" and (await _line(mock_db, "l1", "c2"))["reason_code"] == "last_live_ad"


async def test_undo_for_one_client_goes_through_while_another_client_drains(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db, gids=(GID, "g2"))
    await _ads(mock_db, "b1", "b2", adset="s9", gid="g2")
    await _card(mock_db, agent, _ln(1, ad="b1"), _id="c2", gid="g2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    await mock_db[apply_mod.LEASES].insert_one({"_id": apply_mod._lease_key(agent["_id"], GID), "token": "drain",
                                                "expires_at": datetime.utcnow() + timedelta(minutes=1)})
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c2", "l1", by="s", source="d"))["outcome"] == "undone"


async def test_a_line_that_lost_the_lease_is_not_processed(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", budget_s=0)
    await mock_db[apply_mod.LEASES].insert_one({"_id": apply_mod._lease_key(agent["_id"], GID), "token": "other",
                                                "expires_at": datetime.utcnow() + timedelta(minutes=1)})
    assert (await _tick(mock_db, mock_mongo_client))["processed"] == 0
    assert (await _line(mock_db))["state"] == "queued"


async def test_a_spare_turned_on_in_ads_manager_lets_the_retry_through(mock_db, mock_mongo_client, meta):
    """The mirror's "off" is old, so Meta's live read decides."""
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", off=["b"])
    await _card(mock_db, agent, _ln(1))
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == "blocked"
    meta.set("b", "ACTIVE")
    res = await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    assert res["outcome"] == "applied" and meta.writes == [("ad1", "PAUSED")]


async def test_auto_trusts_metas_spare_over_an_old_mirror_row(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", off=["b"])
    await _card(mock_db, agent, _ln(1, mode="auto"))
    meta.set("b", "ACTIVE")
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert (await _line(mock_db))["state"] == "applied"


async def test_undo_of_bring_back_trusts_metas_spare_over_an_old_mirror_row(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, off=["old", "b"])
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _ln(1, kind="revive", ad="old"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    meta.set("b", "ACTIVE")
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"


@pytest.mark.parametrize("age,outcome", [(timedelta(minutes=4), "blocked"), (timedelta(minutes=6), "applied")])
async def test_a_mirror_off_only_outranks_meta_for_five_minutes(age, outcome, mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", off=["spare"])
    await mock_db["meta_ad_state"].update_one({"ad_id": "spare"}, {"$set": {"updated_at": datetime.utcnow() - age}})
    meta.set("spare", "ACTIVE")  # Meta's read still lags Birdy's own pause, or someone turned it on
    await _card(mock_db, agent, _ln())
    assert (await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d"))["outcome"] == outcome


async def test_undo_is_not_trapped_by_a_lesson_taught_after_the_pause(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d")
    await mock_db["agent_memory"].insert_one({"_id": "m", "agent_id": agent["_id"], "text": "Leave ad1 alone",
                                              "active": True, "filter": {"ad_name_contains": "ad1"}})
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"
    assert meta.ads["ad1"]["status"] == "ACTIVE"


async def _cut_then_confirmed(db, client, meta, monkeypatch):
    real = meta.execute
    monkeypatch.setattr(apply_mod, "execute_action", _inflight(meta))
    agent = await _agent(db)
    await _ads(db, "ad1", "spare")
    await _card(db, agent, _ln())
    await apply_card(db, client, USER, "c1", by="s", source="d", budget_s=0.05)
    assert (await _line(db))["cut_at"]
    await _tick(db, client)
    line = await _line(db)
    assert line["state"] == "applied" and not line.get("cut_at")
    monkeypatch.setattr(apply_mod, "execute_action", real)
    meta.writes.clear()
    return agent


async def test_a_tick_during_the_send_of_an_undo_after_a_cut_leaves_it_alone(mock_db, mock_mongo_client, meta, monkeypatch):
    await _cut_then_confirmed(mock_db, mock_mongo_client, meta, monkeypatch)
    seen = []

    async def tick_during_send(mc, user, action):
        ad = action["targets"][0]["object_id"]
        meta.writes.append((ad, "ACTIVE"))
        await _tick(mock_db, mock_mongo_client)
        seen.append((await _line(mock_db))["state"])
        meta.set(ad, "ACTIVE")
        return {"succeeded": [ad], "failed": []}

    monkeypatch.setattr(apply_mod, "execute_action", tick_during_send)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert seen == ["undoing"] and res["outcome"] == "undone" and (await _line(mock_db))["state"] == "undone"
    assert meta.writes == [("ad1", "ACTIVE")]


async def test_a_tick_before_the_send_of_an_undo_after_a_cut_leaves_it_alone(mock_db, mock_mongo_client, meta, monkeypatch):
    await _cut_then_confirmed(mock_db, mock_mongo_client, meta, monkeypatch)
    real = apply_mod._guardrail_block
    seen = []

    async def tick_first(*a, **k):
        await _tick(mock_db, mock_mongo_client)
        seen.append((await _line(mock_db))["state"])
        return await real(*a, **k)

    monkeypatch.setattr(apply_mod, "_guardrail_block", tick_first)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert seen == ["undoing"] and res["outcome"] == "undone" and (await _line(mock_db))["state"] == "undone"
    assert meta.writes == [("ad1", "ACTIVE")]


async def test_the_undo_claim_clears_a_cut_at_left_on_an_applied_line(mock_db, mock_mongo_client, meta, monkeypatch):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    now = datetime.utcnow()
    meta.set("ad1", "PAUSED")
    await _card(mock_db, agent, _ln(state="applied", wrote=True, applied_at=now, undo_until=now + UNDO_AUTO_WINDOW, cut_at=now))
    claimed = {}
    real = apply_mod._guardrail_block

    async def look(*a, **k):
        claimed.update(await _line(mock_db))
        return await real(*a, **k)

    monkeypatch.setattr(apply_mod, "_guardrail_block", look)
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"
    assert claimed["state"] == "undoing" and claimed["cut_at"] is None


async def test_cut_at_only_skips_the_wait_for_an_applying_line(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    now = datetime.utcnow()
    await _card(mock_db, agent, _ln(state="undoing", undoing_at=now, undo_write_started_at=now, cut_at=now, wrote=True))
    res = await _tick(mock_db, mock_mongo_client)
    assert res["recovered"] == 0 and (await _line(mock_db))["state"] == "undoing" and meta.reads == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def api(mock_mongo_client, monkeypatch, meta):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(stock_router, "get_mongo_client", fake_client)
    state = {"user": USER}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def test_route_apply_undo_and_results(api, mock_db, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    base = f"/api/agents/{agent['_id']}/cards/c1"
    r = api.post(f"{base}/apply", json={})
    assert r.status_code == 200 and r.json()["outcome"] == "applied"
    assert r.json()["card"]["lines"][0]["state"] == "applied" and "user_id" not in r.json()["card"]
    assert (await _line(mock_db))["decision"]["source"] == "dashboard"
    r = api.post(f"{base}/undo", json={"line_id": "l1"})
    assert r.status_code == 200 and r.json()["outcome"] == "undone"
    assert meta.writes == [("ad1", "PAUSED"), ("ad1", "ACTIVE")]


async def test_route_apply_refusal_is_a_409_with_the_reason(api, mock_db, meta):
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(), status="superseded")
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/apply", json={})
    assert r.status_code == 409 and "newer" in r.json()["detail"]


async def test_route_apply_blocked_reports_the_reason(api, mock_db, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1")
    await _card(mock_db, agent, _ln())
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/apply", json={"line_ids": ["l1"]})
    assert r.status_code == 200 and r.json()["ok"] is False and "no live ads" in r.json()["reason"]
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/apply", json={"line_ids": ["nope"]})
    assert r.status_code == 400


async def test_route_undo_expired_is_410(api, mock_db, meta):
    agent = await _agent(mock_db)
    past = datetime.utcnow() - UNDO_AUTO_WINDOW - timedelta(minutes=1)
    await _card(mock_db, agent, _ln(state="applied", applied_at=past, undo_until=past + UNDO_AUTO_WINDOW, wrote=True))
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/undo", json={"line_id": "l1"})
    assert r.status_code == 410 and meta.writes == []


async def test_route_dismiss(api, mock_db, meta, monkeypatch):
    use_provider(monkeypatch, FakeProvider(reply='{"kind": "fact", "text": "Not now"}'))
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln())
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/dismiss", json={"reason": "Not now"})
    assert r.status_code == 200 and r.json()["card"]["status"] == "dismissed"
    assert (await _get(mock_db))["dismiss_reason"] == "Not now"


async def test_route_dismiss_conflicts_say_why(api, mock_db, meta):
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(state="applying"))
    await _card(mock_db, agent, _ln(state="applied"), _id="c2", run_id="r2", status="applied")
    base = f"/api/agents/{agent['_id']}/cards"
    r = api.post(f"{base}/c1/dismiss")
    assert r.status_code == 409 and "applying" in r.json()["detail"]
    r = api.post(f"{base}/c2/dismiss")
    assert r.status_code == 409 and "Undo" in r.json()["detail"]


async def test_recovery_sends_nothing_for_add_lines(mock_db, mock_mongo_client, meta, monkeypatch):
    """test_stock_add.py has the rest."""
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(1, kind="add", ad="", state="queued"),
                _ln(2, kind="add", ad="", state="applying", write_started_at=datetime.utcnow() - timedelta(hours=1)))
    await _tick(mock_db, mock_mongo_client, minutes=10)
    assert (await _line(mock_db, "l1"))["state"] == "flagged"
    assert (await _line(mock_db, "l2"))["state"] == "blocked"  # the sync's call, as nothing was posted and writes are off
    assert meta.reads == [] and meta.writes == []


async def test_activity_labels_say_who(mock_db, mock_mongo_client, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by=USER, source="dashboard", line_ids=["l1"])
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="Sam", source="slack", line_ids=["l2"])
    labels = sorted(r["label"] for r in await _applied_rows(mock_db))
    assert labels == ["Approved by Sam", "Approved by you"]


async def test_route_apply_all(api, mock_db, meta):
    agent = await _agent(mock_db, gids=("g1", "g2", "g3"))
    await _three_cards(mock_db, agent)
    r = api.post(f"/api/agents/{agent['_id']}/runs/r1/apply-all", json={})
    assert r.status_code == 200 and r.json()["outcome"] == "applied" and r.json()["applied"] == 3
    assert len(meta.writes) == 3
    assert api.post(f"/api/agents/{agent['_id']}/runs/nope/apply-all", json={}).status_code == 404


async def test_apply_run_default_budget_is_slacks_and_queues_everything(mock_db, mock_mongo_client, meta):
    assert inspect.signature(apply_run).parameters["budget_s"].default == 2.5
    agent = await _agent(mock_db, gids=("g1", "g2", "g3"))
    await _three_cards(mock_db, agent)
    res = await apply_run(mock_db, mock_mongo_client, USER, "r1", by="s", source="slack")
    assert res["outcome"] == "queued" and res["queued"] == 3 and res["applied"] == 0 and meta.writes == []


async def test_routes_stop_another_users_agent_and_another_agents_card(api, mock_db, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln())
    await mock_db[RUNS].insert_one(_run(agent))
    base = f"/api/agents/{agent['_id']}"
    calls = [(f"{base}/cards/c1/apply", {}), (f"{base}/cards/c1/dismiss", {}),
             (f"{base}/cards/c1/undo", {"line_id": "l1"}), (f"{base}/runs/r1/apply-all", {})]
    api.state["user"] = "u2"
    for path, body in calls:
        assert api.post(path, json=body).status_code == 404, path
    # The caller's own agent id can't reach a card or run of a different agent.
    other = await create_agent(mock_db, "u2", type=TYPE_STOCK)
    other_base = f"/api/agents/{other['_id']}"
    for path, body in calls:
        assert api.post(path.replace(base, other_base), json=body).status_code == 404, path
    assert meta.writes == [] and (await _line(mock_db))["state"] == "proposed"


async def test_route_says_when_a_newer_card_replaced_one(api, mock_db, meta):
    """Retry and Upload need this: can_apply can't tell a replaced card from one with only failed lines left."""
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(1, state="applied"), _ln(2, state="failed", error="x"), status="partly_applied")
    await mock_db[RUNS].insert_one(_run(agent))
    base = f"/api/agents/{agent['_id']}"
    [shown] = api.get(f"{base}/runs/r1").json()["cards"]
    assert shown["has_newer"] is False and shown["can_apply"] is False
    await _card(mock_db, agent, _id="c2", run_id="r2", created=datetime.utcnow() + timedelta(hours=1))
    [shown] = api.get(f"{base}/runs/r1").json()["cards"]
    assert shown["has_newer"] is True and shown["can_apply"] is False


async def test_route_an_open_card_with_a_newer_one_cant_be_applied(api, mock_db, meta):
    """The window before the newer card closes the old one: the apply route refuses, so Apply isn't offered."""
    agent = await _agent(mock_db)
    await _card(mock_db, agent, _ln(1))
    await mock_db[RUNS].insert_one(_run(agent))
    base = f"/api/agents/{agent['_id']}"
    [shown] = api.get(f"{base}/runs/r1").json()["cards"]
    assert shown["has_newer"] is False and shown["can_apply"] is True
    await _card(mock_db, agent, _id="c2", run_id="r2", created=datetime.utcnow() + timedelta(hours=1))
    [shown] = api.get(f"{base}/runs/r1").json()["cards"]
    assert shown["has_newer"] is True and shown["can_apply"] is False
    assert api.post(f"{base}/cards/c1/apply").status_code == 409


async def test_route_can_apply_on_a_partly_applied_card(api, mock_db, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await mock_db[RUNS].insert_one(_run(agent))
    base = f"/api/agents/{agent['_id']}"
    r = api.post(f"{base}/cards/c1/apply", json={"line_ids": ["l1"]})
    assert r.json()["card"]["can_apply"] is True
    assert api.get(f"{base}/runs/r1").json()["cards"][0]["can_apply"] is True
    await _card(mock_db, agent, _id="c2", run_id="r2", created=datetime.utcnow() + timedelta(hours=1))
    assert api.get(f"{base}/runs/r1").json()["cards"][0]["can_apply"] is False


@pytest.mark.parametrize("replaced", ["superseded", "open_with_a_newer_card"])
async def test_route_undo_works_on_a_replaced_card(replaced, api, mock_db, meta):
    """A person must be able to reverse a live ad after a newer card replaced its card. Apply stays closed."""
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "ad2", "spare")
    await _card(mock_db, agent, _ln(1), _ln(2))
    await mock_db[RUNS].insert_one(_run(agent))
    base = f"/api/agents/{agent['_id']}"
    assert api.post(f"{base}/cards/c1/apply", json={"line_ids": ["l1"]}).status_code == 200
    assert meta.writes == [("ad1", "PAUSED")]
    await _card(mock_db, agent, _id="c2", run_id="r2", created=datetime.utcnow() + timedelta(hours=1))
    if replaced == "superseded":
        await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"status": "superseded"}})
    before = (await _get(mock_db))["status"]

    r = api.post(f"{base}/cards/c1/undo", json={"line_id": "l1"})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "undone"
    assert meta.writes == [("ad1", "PAUSED"), ("ad1", "ACTIVE")]
    card = await _get(mock_db)
    assert card["lines"][0]["state"] == "undone" and card["status"] == before
    assert api.post(f"{base}/cards/c1/apply", json={"line_ids": ["l2"]}).status_code == 409


async def test_apply_all_leaves_green_cards_alone_and_they_dont_wait_on_anyone(api, mock_db, meta):
    agent = await _agent(mock_db)
    await _ads(mock_db, "ad1", "spare")
    await _card(mock_db, agent, _ln(1), tag="changes")
    await _card(mock_db, agent, _id="c2", client_group_id="g2", tag="green")
    await mock_db[RUNS].insert_one(_run(agent))
    base = f"/api/agents/{agent['_id']}"
    assert api.get(f"{base}/runs/latest").json()["open_cards"] == 1
    assert api.post(f"{base}/runs/r1/apply-all").json()["applied"] == 1
    green = await mock_db[CARDS].find_one({"_id": "c2"})
    assert green["status"] == "open" and green["lines"] == []
    assert api.get(f"{base}/runs/latest").json()["open_cards"] == 0
