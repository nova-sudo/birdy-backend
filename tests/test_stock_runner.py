"""The stock agent's cards and resumable run state machine (spec sections 6 and 7)."""

import asyncio
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from agent_history_helpers import seed_history
from ai.agents import claims, history
from ai.agents.meta_state import MetaRateLimit, MetaTokenError
from ai.agents.snapshot import AdState
from ai.agents.stock import alerts as alerts_mod
from ai.agents.stock import runner
from ai.agents.stock.cards import CARDS, build_card, supersede_open
from ai.agents.stock.checks import follow_ups
from ai.agents.stock.runner import RUNS, advance_run, get_or_create_run, run_kind
from ai.agents.stock.settings import DEFAULT_SETTINGS
from ai.agents.store import (AD_DAYS, AGENTS, MEMORY, TYPE_STOCK, create_agent, create_agent_indexes,
                             delete_draft_agent)
from jobs import agent_jobs

UTC = timezone.utc
USER = "u1"
NOW = datetime(2026, 11, 3, 7, tzinfo=UTC)  # a Tuesday: Batch B
SYNCED = datetime(2026, 11, 3, 2)
ZONES = {"currency": "GBP", "green_under": 8, "red_over": 10}


def test_run_kind_by_weekday():
    assert run_kind(DEFAULT_SETTINGS, date(2026, 11, 3)) == ("batch", "b")
    assert run_kind(DEFAULT_SETTINGS, date(2026, 11, 4)) == ("alert_only", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class Fakes:
    def __init__(self):
        self.ai_calls, self.reads, self.stored, self.health_reads, self.published, self.autos = [], [], [], [], [], []
        self.urgent = []
        self.order = []
        self.draft = None        # dict or callable(facts) -> dict
        self.read_error = {}     # account -> exception, or a list of them used up in turn
        self.health_error = {}   # account -> exception, or a list of them used up in turn
        self.token = {"access_token": "t"}
        self.health = {"account_status": 1, "funding_source": "card", "is_prepay_account": False,
                       "timezone_name": "UTC", "currency": "GBP", "name": "x"}


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()

    async def token(user_id, mongo_client):
        return f.token

    async def read(account, tok):
        f.order.append(("read", account))
        f.reads.append(account)
        err = f.read_error.get(account)
        if isinstance(err, list):
            err = err.pop(0) if err else None
        if err is not None:
            raise err
        return [], []

    async def store(db, user_id, gid, account, ads, adsets, *, today):
        f.stored.append((gid, today))
        return {}

    async def backfill(db, user_id, gid):
        return 0

    async def health(account, tok):
        f.order.append(("health", account))
        f.health_reads.append(account)
        err = f.health_error.get(account)
        if isinstance(err, list):
            err = err.pop(0) if err else None
        if err is not None:
            raise err
        return dict(f.health)

    async def write(db, agent, facts, candidates, lessons, *, last_card=None, open_questions=None):
        f.ai_calls.append({"gid": facts["client_group_id"], "lessons": lessons, "last_card": last_card,
                           "open_questions": open_questions, "candidates": candidates})
        if callable(f.draft):
            return f.draft(facts)
        return f.draft or {"add": [], "remove": [candidates["remove"][0]["ad_id"]], "revive": [],
                           "additional_changes": "", "thought_process": "Swap the worst ad.",
                           "next_steps": "Check on Friday.", "flags": [], "failed": False}

    async def publish(db, run, agent, cards):
        f.published.append({"run": run, "cards": cards})

    async def auto(db, mongo_client, run, agent):
        f.autos.append(run["_id"])

    async def urgent(db, run, agent):
        f.order.append(("urgent",))
        f.urgent.append([a["code"] for a in run["alerts"]])

    monkeypatch.setattr(runner, "get_facebook_token", token)
    monkeypatch.setattr(runner, "read_meta_state", read)
    monkeypatch.setattr(runner, "store_meta_state", store)
    monkeypatch.setattr(runner, "backfill_paused_at", backfill)
    monkeypatch.setattr(alerts_mod, "fetch_account_health", health)
    monkeypatch.setattr(runner, "write_card", write)
    monkeypatch.setattr(runner, "publish_run", publish)
    monkeypatch.setattr(runner, "auto_apply_run", auto)
    monkeypatch.setattr(runner, "post_urgent_alerts", urgent)
    return f


async def _client(db, gid, *, spend=120, leads=10, prev=(120, 10), batch="b", synced=SYNCED, currency="GBP",
                  history_status="ready", days=True, **client):
    """A client with one live ad. Defaults give a CPL of 12 in both windows: red, so it proposes."""
    await db.client_groups.insert_one({"id": gid, "user_id": USER, "name": f"Client {gid}",
                                       "meta_ad_account_id": f"act_{gid}", "ad_account_currency": currency,
                                       "meta_campaign_ids": None, "last_meta_refresh": datetime(2026, 11, 3, 6, 30)})
    await seed_history(db, gid, status=history_status, last_topup_at=synced)
    ad = f"ad_{gid}"
    await db["meta_ad_state"].insert_one({"client_group_id": gid, "ad_id": ad, "name": f"Ad {gid}",
                                          "campaign_id": "c1", "effective_status": "ACTIVE", "creative": {}})
    if days:
        for day, s, l in (("2026-11-01", spend, leads), ("2026-10-25", prev[0], prev[1])):
            await db[AD_DAYS].insert_one({
                "user_id": USER, "client_group_id": gid, "ad_id": ad, "campaign_id": "c1", "ad_name": f"Ad {gid}",
                "date_start": day, "insight_data": {"spend": str(s),
                                                    "actions": [{"action_type": "lead", "value": str(l)}]}})
    return {gid: {"batch_id": batch, "treatment_ids": [], **client}}


async def _agent(db, clients: dict, *, claim=True, status="active", **settings):
    agent = await create_agent(db, USER, type=TYPE_STOCK)
    await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {
        "status": status,
        "scope": {"client_group_ids": list(clients), "schedule": {"time": "07:00", "tz": "UTC"}},
        "settings": {**DEFAULT_SETTINGS, "zones": ZONES, "clients": clients, **settings}}})
    if claim:
        await claims.claim_clients(db, USER, agent["_id"], list(clients))
    return await db[AGENTS].find_one({"_id": agent["_id"]})


async def _setup(db, *gids, claim=True, **kw):
    clients = {}
    for gid in gids:
        clients.update(await _client(db, gid, **kw))
    return await _agent(db, clients, claim=claim)


async def _tick(db, client, run, agent, now=NOW):
    return await advance_run(db, client, run, agent, deadline=None, now_utc=now)


async def _run_all(db, client, agent, now=NOW, **kw):
    """Ticks every 15 minutes until the run is done, as the cron would."""
    run = await get_or_create_run(db, agent, now, **kw)
    for n in range(20):
        run = await _tick(db, client, run, agent, now + timedelta(minutes=15 * n))
        if run["phase"] == "done":
            break
    return run


async def _cards(db, **q):
    return await db[CARDS].find(q).to_list(None)


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

async def test_runs_once_per_day(mock_db, mock_mongo_client, fakes):
    await create_agent_indexes(mock_mongo_client)
    agent = await _setup(mock_db, "g1")
    a = await get_or_create_run(mock_db, agent, NOW)
    b = await get_or_create_run(mock_db, agent, NOW + timedelta(hours=3))
    assert a["_id"] == b["_id"] and await mock_db[RUNS].count_documents({}) == 1
    assert a["local_date"] == "2026-11-03" and (a["kind"], a["batch_id"]) == ("batch", "b")
    # A second writer racing past the find hits the unique index.
    with pytest.raises(Exception, match="(?i)duplicate"):
        await mock_db[RUNS].insert_one({"_id": "x", "agent_id": agent["_id"], "local_date": "2026-11-03",
                                        "scheduled": True})


async def test_preview_does_not_take_the_days_run(mock_db, mock_mongo_client, fakes):
    await create_agent_indexes(mock_mongo_client)
    agent = await _setup(mock_db, "g1")
    preview = await get_or_create_run(mock_db, agent, NOW, kind="preview")
    assert (await get_or_create_run(mock_db, agent, NOW, kind="preview"))["_id"] == preview["_id"]
    real = await get_or_create_run(mock_db, agent, NOW)
    assert real["_id"] != preview["_id"] and real["kind"] == "batch"


async def test_a_second_batch_on_the_same_weekday_is_not_dropped(mock_db, mock_mongo_client, fakes):
    clients = {**await _client(mock_db, "g1", batch="b"), **await _client(mock_db, "g2", batch="c")}
    agent = await _agent(mock_db, clients, batches=[{"id": "b", "name": "B", "weekdays": [1]},
                                                    {"id": "c", "name": "C", "weekdays": [1]}])
    await _run_all(mock_db, mock_mongo_client, agent)
    assert {c["client_group_id"] for c in await _cards(mock_db)} == {"g1", "g2"}


async def test_resumes_after_deadline(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1", "g2", "g3")
    monkeypatch.setattr(runner, "_out_of_time", lambda d: len(fakes.ai_calls) >= 1)
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=1.0)
    assert run["phase"] == "writing" and len(fakes.ai_calls) == 1 and run["claimed_at"] is None
    assert len(await _cards(mock_db)) == 1
    first = fakes.ai_calls[0]["gid"]

    monkeypatch.setattr(runner, "_out_of_time", lambda d: False)
    run = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=1.0)
    assert run["phase"] == "done"
    gids = [c["gid"] for c in fakes.ai_calls]
    assert sorted(gids) == ["g1", "g2", "g3"] and gids.count(first) == 1
    assert len(await _cards(mock_db)) == 3 and len(fakes.published) == 1


async def test_killed_between_saving_a_card_and_its_state_makes_no_second_ai_call(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    await _run_all(mock_db, mock_mongo_client, agent)
    assert len(fakes.ai_calls) == 1
    # Rewind to the instant after the card was saved, before the client's state was.
    await mock_db[RUNS].update_many({}, {"$set": {"phase": "writing", "clients.g1.state": "needs_writing",
                                                  "clients.g1.facts": {"client_group_id": "g1"},
                                                  "claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    run = await mock_db[RUNS].find_one({})
    run = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
    assert run["phase"] == "done" and len(fakes.ai_calls) == 1 and len(await _cards(mock_db)) == 1
    assert run["clients"]["g1"]["state"] == "done"


async def test_killed_run_retries_then_ends_with_whats_missing(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1", "g2")

    def killed(*a, **k):
        raise BaseException("function killed")

    run = await get_or_create_run(mock_db, agent, NOW)
    long_ago = datetime.utcnow() - timedelta(hours=1)
    for attempt in (1, 2):
        monkeypatch.setattr(runner, "_alerts_phase", killed)
        with pytest.raises(BaseException, match="killed"):
            await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
        stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
        assert stored["claimed_at"] is not None  # a killed invocation can't release its claim
        assert stored["attempts"] == attempt - 1
        # A fresh claim is left alone by an overlapping tick.
        same = await advance_run(mock_db, mock_mongo_client, stored, agent, deadline=None)
        assert same["attempts"] == attempt - 1
        await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": long_ago}})

    with pytest.raises(BaseException, match="killed"):
        await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": long_ago}})

    done = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
    assert done["phase"] == "done" and done["attempts"] == 3 and done["claimed_at"] is None
    assert sorted(done["summary"]["missing"]) == ["g1", "g2"]
    assert all(c["state"] == "failed" and "ran out of time" in c["error"] for c in done["clients"].values())
    assert fakes.ai_calls == [] and fakes.autos == []


async def test_a_retried_run_finishes_when_the_kill_does_not_repeat(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    done = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
    assert done["phase"] == "done" and len(await _cards(mock_db)) == 1


async def test_a_run_level_error_counts_as_an_attempt_and_cannot_retry_forever(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")

    async def boom(*a, **k):
        raise RuntimeError("bad phase")

    monkeypatch.setattr(runner, "_alerts_phase", boom)
    run = await get_or_create_run(mock_db, agent, NOW)
    for n in (1, 2, 3):
        run = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
        assert run["attempts"] == n and run["claimed_at"] is None
    run = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
    assert run["phase"] == "done" and run["summary"]["missing"] == ["g1"]


async def test_one_client_failure_doesnt_stop_others(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1", "g2")
    real = runner.client_facts
    calls = []

    async def facts(db, agent, group, window):
        calls.append(group["id"])
        if group["id"] == "g1":
            raise ValueError("bad row")
        return await real(db, agent, group, window)

    monkeypatch.setattr(runner, "client_facts", facts)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["phase"] == "done"
    assert run["clients"]["g1"]["state"] == "failed" and "bad row" in run["clients"]["g1"]["error"]
    assert calls.count("g1") == runner.CLIENT_ATTEMPTS and calls.count("g2") == 1
    assert [c["client_group_id"] for c in await _cards(mock_db)] == ["g2"]
    assert [r["client_group_id"] for r in run["summary"]["could_not_check"]] == ["g1"]


async def test_a_client_error_is_retried_on_a_later_tick_not_failed(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1", "g2")
    real, seen = runner.client_facts, []

    async def facts(db, agent, group, window):
        seen.append(group["id"])
        if group["id"] == "g1" and seen.count("g1") == 1:
            raise ValueError("blip")
        return await real(db, agent, group, window)

    monkeypatch.setattr(runner, "client_facts", facts)
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["clients"]["g1"]["state"] == "alerted" and run["clients"]["g1"]["attempts"] == 1
    assert run["clients"]["g2"]["state"] == "done" and run["phase"] == "waiting"
    assert seen.count("g1") == 1  # not again in the same tick
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "done"
    assert run["clients"]["g1"]["attempts"] == 0
    assert len(fakes.published) == 2  # each card goes out as soon as it exists


async def test_rate_limit_stops_the_tick_and_leaves_clients_pending(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2", "g3")
    fakes.read_error["act_g2"] = [MetaRateLimit("slow down")]
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["phase"] == "alerts" and run["claimed_at"] is None
    assert fakes.reads == ["act_g1", "act_g2"]  # g3 wasn't tried
    assert run["clients"]["g2"]["state"] == "pending" and run["clients"]["g3"]["state"] == "pending"
    assert run["clients"]["g2"]["attempts"] == 0 and run["attempts"] == 0
    assert fakes.urgent == [[]]  # the hook still runs, with what the pass has
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "done" and len(await _cards(mock_db)) == 3
    assert fakes.reads.count("act_g1") == 1  # finished work isn't repeated


async def test_rate_limit_on_the_health_read_also_leaves_clients_pending(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    fakes.health_error["act_g1"] = MetaRateLimit("slow down")
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["clients"]["g1"]["state"] == "pending" and run["clients"]["g1"]["refreshed"] is True
    assert not any(a["code"] == "check_failed" for a in run["alerts"])
    fakes.health_error.clear()
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "done" and fakes.reads == ["act_g1"]


async def test_failed_refresh_still_reads_health_and_retries(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1")
    fakes.read_error["act_g1"] = [RuntimeError("blip")]
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert any(a["code"] == "account_disabled" for a in run["alerts"]) and fakes.urgent
    assert run["clients"]["g1"]["state"] == "waiting" and run["phase"] == "waiting"
    assert await _cards(mock_db) == []
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    [card] = await _cards(mock_db)
    assert run["phase"] == "done" and card["stale"] is False
    assert fakes.health_reads == ["act_g1"] and len(fakes.urgent) == 2 and "account_disabled" in fakes.urgent[-1]


async def test_one_stale_client_leaves_the_others_not_stale(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    fakes.read_error["act_g1"] = RuntimeError("Meta said no")
    run = await _run_all(mock_db, mock_mongo_client, agent)
    cards = {c["client_group_id"]: c for c in await _cards(mock_db)}
    assert cards["g1"]["stale"] is True and cards["g2"]["stale"] is False
    assert fakes.reads.count("act_g1") == runner.CLIENT_ATTEMPTS and fakes.reads.count("act_g2") == 1
    assert run["summary"]["stale_clients"] == ["g1"]
    assert any(a["client_group_id"] == "g1" and "Meta said no" in a["detail"] for a in run["alerts"])


async def test_an_old_group_refresh_stamp_does_not_make_a_card_stale(mock_db, mock_mongo_client, fakes):
    await _setup(mock_db, "g1")
    await mock_db.client_groups.update_many({}, {"$set": {"last_meta_refresh": datetime(2026, 10, 1)}})
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    [card] = await _cards(mock_db)
    assert card["stale"] is False


async def test_expired_token_records_reconnect_and_no_writes(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    fakes.read_error["act_g1"] = MetaTokenError("expired")
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["phase"] == "done" and run["reconnect"] is True and run["summary"]["reconnect"] is True
    assert fakes.ai_calls == [] and fakes.autos == [] and await _cards(mock_db) == []
    assert all(c["state"] == "failed" and "Reconnect" in c["error"] for c in run["clients"].values())
    assert len(fakes.published) == 1  # the report and Slack say to reconnect
    assert fakes.reads == ["act_g1"]  # no point reading on with a dead token


async def test_token_error_from_the_health_read_is_a_reconnect(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    fakes.health_error["act_g1"] = MetaTokenError("expired")
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["reconnect"] is True and fakes.autos == []
    assert not any(a["code"] == "check_failed" for a in run["alerts"])
    assert any(a["code"] == "meta_reconnect" for a in run["alerts"])


async def test_dead_token_reports_reconnect_on_the_first_tick(mock_db, mock_mongo_client, fakes):
    await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))
    fakes.token = None
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    run = await mock_db[RUNS].find_one({})
    assert out["ran"] == 1 and run["reconnect"] is True and run["phase"] == "done"
    assert fakes.reads == [] and len(fakes.published) == 1


async def test_missing_token_records_reconnect(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    fakes.token = None
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["reconnect"] is True and fakes.reads == [] and fakes.autos == []


async def test_shared_account_is_read_once_and_stored_for_each_client(mock_db, mock_mongo_client, fakes):
    clients = {**await _client(mock_db, "g1"), **await _client(mock_db, "g2")}
    await mock_db.client_groups.update_many({}, {"$set": {"meta_ad_account_id": "act_shared"}})
    agent = await _agent(mock_db, clients)
    await _run_all(mock_db, mock_mongo_client, agent)
    assert fakes.order[:3] == [("read", "act_shared"), ("health", "act_shared"), ("urgent",)]
    assert fakes.reads == ["act_shared"] and fakes.health_reads == ["act_shared"]
    assert [g for g, _ in fakes.stored] == ["g1", "g2"]


async def test_deadline_is_checked_inside_a_shared_account_and_progress_is_kept(mock_db, mock_mongo_client, fakes, monkeypatch):
    clients = {**await _client(mock_db, "g1"), **await _client(mock_db, "g2")}
    await mock_db.client_groups.update_many({}, {"$set": {"meta_ad_account_id": "act_shared"}})
    agent = await _agent(mock_db, clients)
    monkeypatch.setattr(runner, "_out_of_time", lambda d: len(fakes.stored) >= 1)
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert [g for g, _ in fakes.stored] == ["g1"] and fakes.health_reads == []
    assert run["clients"]["g1"]["refreshed"] is True and run["claimed_at"] is None
    monkeypatch.setattr(runner, "_out_of_time", lambda d: False)
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert [g for g, _ in fakes.stored] == ["g1", "g2"] and run["phase"] == "done"


class Killed(BaseException):
    pass


async def test_repeated_kills_on_one_client_dont_report_later_clients_missing(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2", "g3")
    fakes.read_error["act_g2"] = Killed("function killed")
    run = await get_or_create_run(mock_db, agent, NOW)
    for _ in range(runner.CLIENT_ATTEMPTS):
        with pytest.raises(Killed):
            await _tick(mock_db, mock_mongo_client, run, agent)
        await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    fakes.read_error.clear()
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["phase"] == "done" and run["summary"]["missing"] == []
    assert run["clients"]["g2"]["state"] == "failed" and "ran out of time" in run["clients"]["g2"]["error"]
    assert run["clients"]["g1"]["state"] == run["clients"]["g3"]["state"] == "done"
    assert run["attempts"] == 0 and not run.get("ended_early")


async def test_kills_on_different_clients_with_progress_between_cost_nobody_else(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1", "g2")
    real, kills = runner.write_card, []

    async def write(db, agent, facts, candidates, lessons, *, last_card=None, open_questions=None):
        if facts["client_group_id"] not in kills:
            kills.append(facts["client_group_id"])
            raise Killed("function killed")
        return await real(db, agent, facts, candidates, lessons, last_card=last_card, open_questions=open_questions)

    monkeypatch.setattr(runner, "write_card", write)
    run = await get_or_create_run(mock_db, agent, NOW)
    for _ in range(2):
        with pytest.raises(Killed):
            await _tick(mock_db, mock_mongo_client, run, agent)
        await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == run["clients"]["g2"]["state"] == "done"


async def test_urgent_hook_gets_alerts_on_a_batch_day_and_alert_only_day(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))  # late on spend data
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    run = await mock_db[RUNS].find_one({})
    assert run["kind"] == "batch" and fakes.urgent == [["account_disabled"]]
    assert run["clients"]["g1"]["state"] == "waiting"

    await mock_db[RUNS].delete_many({})
    fakes.urgent.clear()
    await mock_db[AGENTS].update_many({}, {"$set": {"last_run_at": None}})
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(days=1))
    run = await mock_db[RUNS].find_one({})
    assert run["kind"] == "alert_only" and fakes.urgent == [["account_disabled", "spend_data_late"]]
    assert run["phase"] == "done"  # never waits


async def test_one_late_client_doesnt_hold_others(mock_db, mock_mongo_client, fakes):
    clients = {**await _client(mock_db, "late", synced=datetime(2026, 11, 2, 20)), **await _client(mock_db, "ok")}
    await _agent(mock_db, clients)
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    run = await mock_db[RUNS].find_one({})
    assert out["ran"] == 1 and run["clients"]["late"]["state"] == "waiting" and run["phase"] == "waiting"
    assert [c["client_group_id"] for c in await _cards(mock_db)] == ["ok"] and len(fakes.published) == 1
    assert (await mock_db[history.HISTORY].find_one({"_id": "late"}))["topup_requested_at"] is not None
    assert (await mock_db[AGENTS].find_one({})).get("last_run_at") is None

    # Nothing changed: a tick does nothing and posts nothing.
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(minutes=15))
    assert len(fakes.published) == 1 and (await mock_db[RUNS].find_one({}))["phase"] == "waiting"

    # The top-up lands: the late client gets its card, once, on complete days.
    await mock_db[history.HISTORY].update_one({"_id": "late"}, {"$set": {"last_topup_at": datetime(2026, 11, 3, 7, 5),
                                                                         "topup_requested_at": None}})
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(minutes=30))
    run = await mock_db[RUNS].find_one({})
    assert run["phase"] == "done" and len(await _cards(mock_db)) == 2 and len(fakes.published) == 2
    assert sorted(c["gid"] for c in fakes.ai_calls) == ["late", "ok"]
    assert (await mock_db[AGENTS].find_one({})).get("last_run_at") is not None


async def test_late_client_past_grace_finishes_with_a_reason_and_no_card(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["clients"]["g1"]["state"] == "waiting"
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + runner.GRACE)
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "data_late"
    assert "2026-11-02" in run["clients"]["g1"]["reason"] and await _cards(mock_db) == []
    assert any(a["code"] == "spend_data_late" for a in run["alerts"]) and fakes.ai_calls == []


async def test_alert_only_day_never_waits_for_late_data(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", synced=datetime(2026, 11, 3, 2))  # late for Wednesday
    run = await get_or_create_run(mock_db, agent, NOW + timedelta(days=1))
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(days=1))
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "alerts_only"
    assert any(a["code"] == "spend_data_late" for a in run["alerts"])
    assert (await mock_db[history.HISTORY].find_one({"_id": "g1"})).get("topup_requested_at") is None


async def test_history_in_error_is_not_waited_for(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20), history_status="error")
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "data_late"


async def test_check_failed_alerts_with_different_details_both_stay(mock_db, mock_mongo_client, fakes):
    run = {"_id": "r", "alerts": [], "clients": {}}
    await mock_db[RUNS].insert_one(dict(run))
    make = lambda code, detail: {"client_group_id": "g", "code": code, "detail": detail}  # noqa: E731
    await runner._add_alerts(mock_db, run, [make("check_failed", "a"), make("check_failed", "b"),
                                            make("spend_drop", "x")])
    await runner._add_alerts(mock_db, run, [make("check_failed", "a"), make("spend_drop", "y")])
    assert sorted(a["detail"] for a in run["alerts"]) == ["a", "b", "y"]


async def test_alerts_are_deduped_per_client_and_code(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "is_prepay_account": True}
    agent = await _setup(mock_db, "g1", "g2")
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
    # The same alerts coming round again, as they do when the phase is retried.
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {
        "phase": "alerts", "clients.g1.state": "pending", "clients.g2.state": "pending"}})
    run = await advance_run(mock_db, mock_mongo_client, await mock_db[RUNS].find_one({"_id": run["_id"]}),
                            agent, deadline=None)
    codes = [(a["client_group_id"], a["code"]) for a in run["alerts"]]
    assert codes.count(("g1", "prepaid_no_topups")) == 1 and codes.count(("g2", "prepaid_no_topups")) == 1
    assert len(codes) == len(set(codes))


# ---------------------------------------------------------------------------
# What each client gets
# ---------------------------------------------------------------------------

async def test_green_card_makes_no_ai_call(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", spend=50, prev=(50, 10))
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert fakes.ai_calls == [] and card["tag"] == "green" and card["lines"] == []
    assert "£5.00" in card["thought_process"]


async def test_no_changes_on_red_states_reason_and_cpl(mock_db, mock_mongo_client, fakes):
    # CPL 12 down from 20: red, but a 40% drop is a major downtrend.
    agent = await _setup(mock_db, "g1", spend=120, leads=10, prev=(200, 10))
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert fakes.ai_calls == [] and card["tag"] == "no_changes" and card["lines"] == []
    assert "£12.00" in card["thought_process"] and "downtrend" in card["thought_process"]
    assert card["next_steps"]  # says what goes in next if it stalls


async def test_ai_choosing_nothing_still_states_reason_and_cpl(mock_db, mock_mongo_client, fakes):
    fakes.draft = {"add": [], "remove": [], "revive": [], "additional_changes": "", "thought_process": "Hold.",
                   "next_steps": "", "flags": [], "failed": False}
    agent = await _setup(mock_db, "g1")
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert card["tag"] == "no_changes" and "£12.00" in card["thought_process"] and "Hold." in card["thought_process"]


async def test_spend_stopped_says_so_with_no_judgement(mock_db, mock_mongo_client, fakes):
    clients = await _client(mock_db, "g1", days=False)
    await mock_db[AD_DAYS].insert_one({
        "user_id": USER, "client_group_id": "g1", "ad_id": "ad_g1", "campaign_id": "c1", "ad_name": "x",
        "date_start": "2026-10-28", "insight_data": {"spend": "500", "actions": []}})
    agent = await _agent(mock_db, clients)
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert fakes.ai_calls == [] and card["tag"] == "no_changes" and "stopped" in card["thought_process"].lower()
    assert "CPL is" not in card["thought_process"]


async def test_new_client_with_no_history_finishes_as_no_data(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", history_status="running", synced=None, days=False)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "no_data"
    assert "history" in run["clients"]["g1"]["reason"]
    assert fakes.ai_calls == [] and await _cards(mock_db) == []


async def test_synced_client_with_no_rows_gets_a_no_data_card(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", days=False)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert run["phase"] == "done" and fakes.ai_calls == []
    assert card["tag"] == "no_changes" and card["previous_cpl"] is None and "No data" in card["thought_process"]


async def test_late_data_makes_no_false_stopped_card(mock_db, mock_mongo_client, fakes):
    # Synced before midnight, so 2 Nov is incomplete. A false stopped or a thin CPL must not reach a card.
    agent = await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20), days=False)
    await mock_db[AD_DAYS].insert_one({
        "user_id": USER, "client_group_id": "g1", "ad_id": "ad_g1", "campaign_id": "c1", "ad_name": "x",
        "date_start": "2026-10-29", "insight_data": {"spend": "500", "actions": []}})
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + runner.GRACE)
    assert run["clients"]["g1"]["state"] == "data_late" and await _cards(mock_db) == []


async def test_no_history_reason_says_when_the_doc_is_in_error(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", history_status="error", synced=None, days=False)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "no_data"
    assert "syncing" not in run["clients"]["g1"]["reason"] and "couldn't read" in run["clients"]["g1"]["reason"]


async def test_supersede_error_is_retried_and_never_leaves_two_open_cards(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")
    await mock_db[CARDS].insert_one({"_id": "old", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r0",
                                     "status": "open", "lines": [], "created_at": datetime(2026, 10, 30)})
    real = runner.supersede_open
    fails = [RuntimeError("db blip")]

    async def supersede(db, agent_id, gid, run_id):
        if fails:
            raise fails.pop()
        return await real(db, agent_id, gid, run_id)

    monkeypatch.setattr(runner, "supersede_open", supersede)
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["clients"]["g1"]["state"] == "needs_writing"
    assert await mock_db[CARDS].count_documents({"status": "open"}) == 1  # the new one isn't open yet
    assert await _cards(mock_db, status="closing") and not fakes.published[0]["cards"]
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "done" and len(fakes.ai_calls) == 1
    assert (await mock_db[CARDS].find_one({"_id": "old"}))["status"] == "superseded"
    assert (await mock_db[CARDS].find_one({"run_id": run["_id"]}))["status"] == "open"


async def test_supersede_error_on_a_card_needing_no_ai_is_retried_too(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1", spend=50, prev=(50, 10))
    real = runner.supersede_open
    fails = [RuntimeError("db blip")]

    async def supersede(db, agent_id, gid, run_id):
        if fails:
            raise fails.pop()
        return await real(db, agent_id, gid, run_id)

    monkeypatch.setattr(runner, "supersede_open", supersede)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "done" and (await _cards(mock_db))[0]["status"] == "open"


async def test_end_incomplete_opens_a_stored_card_and_closes_the_old_one(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1", "g2")
    await mock_db[CARDS].insert_one({"_id": "old", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r0",
                                     "status": "open", "lines": [], "created_at": datetime(2026, 10, 30)})
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[CARDS].insert_one({"_id": "new", "agent_id": agent["_id"], "client_group_id": "g1",
                                     "run_id": run["_id"], "status": "closing", "final_status": "open", "lines": []})
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {
        "clients.g1.state": "needs_writing", "attempts": 3, "phase": "writing"}})
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "done"
    assert run["clients"]["g2"]["state"] == "failed" and run["summary"]["missing"] == ["g2"]
    assert (await mock_db[CARDS].find_one({"_id": "old"}))["status"] == "superseded"
    assert (await mock_db[CARDS].find_one({"_id": "new"}))["status"] == "open"


async def test_end_incomplete_supersede_failure_leaves_one_open_card(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")
    await mock_db[CARDS].insert_one({"_id": "old", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r0",
                                     "status": "open", "lines": [], "created_at": datetime(2026, 10, 30)})
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[CARDS].insert_one({"_id": "new", "agent_id": agent["_id"], "client_group_id": "g1",
                                     "run_id": run["_id"], "status": "closing", "final_status": "open", "lines": []})
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"clients.g1.state": "needs_writing", "attempts": 3}})

    async def boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(runner, "supersede_open", boom)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "failed"
    assert await mock_db[CARDS].count_documents({"status": "open"}) == 1


async def test_follow_ups_error_doesnt_waste_the_ai_call(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")
    await mock_db[CARDS].insert_one({"_id": "old", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r0",
                                     "status": "open", "lines": [], "created_at": datetime(2026, 10, 30)})

    async def boom(*a, **k):
        raise RuntimeError("bad state")

    monkeypatch.setattr(runner, "load_state", boom)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    card = await mock_db[CARDS].find_one({"run_id": run["_id"]})
    assert card["follow_ups"] == [] and card["status"] == "open" and len(fakes.ai_calls) == 1
    assert run["clients"]["g1"]["state"] == "done"


async def test_new_card_supersedes_open_card_for_that_client(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    old = [{"_id": "old1", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r0", "status": "open",
            "lines": [], "created_at": datetime(2026, 10, 30)},
           {"_id": "done1", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r00", "status": "applied",
            "lines": [], "created_at": datetime(2026, 10, 27)},
           {"_id": "other", "agent_id": agent["_id"], "client_group_id": "g9", "run_id": "r0", "status": "open",
            "lines": [], "created_at": datetime(2026, 10, 30)}]
    await mock_db[CARDS].insert_many(old)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert (await mock_db[CARDS].find_one({"_id": "old1"}))["status"] == "superseded"
    assert (await mock_db[CARDS].find_one({"_id": "done1"}))["status"] == "applied"
    assert (await mock_db[CARDS].find_one({"_id": "other"}))["status"] == "open"
    assert (await mock_db[CARDS].find_one({"run_id": run["_id"], "client_group_id": "g1"}))["status"] == "open"


async def test_supersede_open_ignores_this_runs_card(mock_db):
    await mock_db[CARDS].insert_many([
        {"_id": "a", "agent_id": "ag", "client_group_id": "g", "run_id": "r1", "status": "open"},
        {"_id": "b", "agent_id": "ag", "client_group_id": "g", "run_id": "r2", "status": "open"}])
    await supersede_open(mock_db, "ag", "g", "r2")
    assert (await mock_db[CARDS].find_one({"_id": "a"}))["status"] == "superseded"
    assert (await mock_db[CARDS].find_one({"_id": "b"}))["status"] == "open"


async def test_alert_only_day_makes_no_cards(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    wed = NOW + timedelta(days=1)
    run = await _run_all(mock_db, mock_mongo_client, agent, now=wed)
    assert run["kind"] == "alert_only" and run["phase"] == "done"
    assert run["clients"]["g1"]["state"] == "alerts_only"
    assert await _cards(mock_db) == [] and fakes.ai_calls == [] and fakes.health_reads == ["act_g1"]
    assert len(fakes.published) == 1


async def test_client_in_another_batch_gets_alerts_only(mock_db, mock_mongo_client, fakes):
    clients = {**await _client(mock_db, "g1"), **await _client(mock_db, "g2", batch="a")}
    agent = await _agent(mock_db, clients)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g2"]["state"] == "alerts_only"
    assert [c["client_group_id"] for c in await _cards(mock_db)] == ["g1"]


async def test_on_hold_client_is_skipped(mock_db, mock_mongo_client, fakes):
    clients = {**await _client(mock_db, "g1", on_hold=True), **await _client(mock_db, "g2")}
    agent = await _agent(mock_db, clients)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "on_hold" and "act_g1" not in fakes.reads
    assert not any(a["client_group_id"] == "g1" for a in run["alerts"])
    assert [c["client_group_id"] for c in await _cards(mock_db)] == ["g2"]


async def test_client_with_no_zones_is_flagged_not_carded(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", currency="USD")  # default zones are in GBP
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "no_zones" and "zones" in run["clients"]["g1"]["reason"]
    assert any(a["client_group_id"] == "g1" and a["code"] == "no_zones" for a in run["alerts"])
    assert fakes.ai_calls == [] and await _cards(mock_db) == []


async def test_writer_gets_last_card_questions_and_lessons(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    await mock_db[CARDS].insert_many([
        {"_id": "older", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r0", "status": "applied",
         "lines": [], "created_at": datetime(2026, 10, 20)},
        {"_id": "last", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r1", "status": "open",
         "lines": [], "open_questions": ["Is the offer over?"], "created_at": datetime(2026, 10, 30)},
        {"_id": "prev", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "rp", "status": "open",
         "preview": True, "lines": [], "created_at": datetime(2026, 11, 2)}])
    for text, gid, active, created in (("for all", None, True, 1), ("for me", "g1", True, 2),
                                       ("for another", "g2", True, 3), ("overruled", None, False, 4)):
        await mock_db[MEMORY].insert_one({"_id": text, "agent_id": agent["_id"], "text": text, "kind": "lesson",
                                          "active": active, "client_group_id": gid,
                                          "created_at": datetime(2026, 10, created)})
    await _run_all(mock_db, mock_mongo_client, agent)
    [call] = fakes.ai_calls
    assert call["last_card"]["_id"] == "last" and call["open_questions"] == ["Is the offer over?"]
    assert call["lessons"] == ["for me", "for all"]  # newest first, this client's and agency-wide


async def test_failed_draft_has_no_changes_and_never_reaches_auto(mock_db, mock_mongo_client, fakes):
    fakes.draft = lambda facts: {"add": [], "remove": ["ad_g1"], "revive": [], "additional_changes": "x",
                                 "thought_process": "Birdy couldn't write this one, so please review.",
                                 "next_steps": "", "flags": [], "failed": True}
    agent = await _setup(mock_db, "g1")
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert card["lines"] == [] and card["ai_failed"] is True and card["tag"] == "no_changes"
    assert card["additional_changes"] == ""
    assert "couldn't write" in card["thought_process"]
    assert len(fakes.ai_calls) == 1  # finished: never asked again


async def test_client_held_by_another_agent_gets_an_all_flag_card(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2", claim=False)
    await claims.claim_clients(mock_db, USER, agent["_id"], ["g2"])
    await claims.claim_clients(mock_db, USER, "other-agent", ["g1"])
    run = await _run_all(mock_db, mock_mongo_client, agent)
    cards = {c["client_group_id"]: c for c in await _cards(mock_db)}
    assert cards["g1"]["status"] == "flagged" and all(ln["mode"] == "flag" for ln in cards["g1"]["lines"])
    assert cards["g2"]["status"] == "open" and cards["g2"]["lines"][0]["mode"] == "ask"
    assert run["clients"]["g1"]["state"] == "done"


async def test_client_moved_after_checks_gets_flags(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")
    real = runner._checks_phase

    async def then_moved(db, mongo_client, run, agent, deadline):
        done = await real(db, mongo_client, run, agent, deadline)
        await claims.claim_clients(db, USER, "other-agent", ["g1"], move=True)
        return done

    monkeypatch.setattr(runner, "_checks_phase", then_moved)
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert card["status"] == "flagged" and card["lines"][0]["mode"] == "flag"


@pytest.mark.parametrize("scope_flag_only,other_holder", [(True, False), (True, True), (False, False), (False, True)])
async def test_only_flag_and_unclaimed_clients_get_flag_lines_only(mock_db, mock_mongo_client, fakes, scope_flag_only,
                                                                   other_holder):
    agent = await _setup(mock_db, "g1", claim=False)
    if scope_flag_only:
        await mock_db[AGENTS].update_one({"_id": agent["_id"]},
                                         {"$set": {"scope.flag_only_client_group_ids": ["g1"]}})
        agent = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    if other_holder:
        await claims.claim_clients(mock_db, USER, "other-agent", ["g1"])
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert card["status"] == "flagged" and card["lines"]
    assert all(ln["mode"] == "flag" and ln["state"] == "flagged" for ln in card["lines"])


async def test_preview_touches_nothing_live(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", claim=False, batch="a")  # not in today's batch: a preview checks it anyway
    await mock_db[CARDS].insert_one({"_id": "live", "agent_id": agent["_id"], "client_group_id": "g1",
                                     "run_id": "r0", "status": "open", "lines": []})
    run = await _run_all(mock_db, mock_mongo_client, agent, kind="preview")
    assert run["kind"] == "preview" and run["phase"] == "done"
    assert fakes.published == [] and fakes.autos == []
    assert (await mock_db[CARDS].find_one({"_id": "live"}))["status"] == "open"
    card = await mock_db[CARDS].find_one({"run_id": run["_id"]})
    assert card["preview"] is True and card["status"] == "open"
    assert card["lines"][0]["mode"] == "ask"  # the mode it would have once the agent is on


async def test_preview_shows_a_client_held_elsewhere_as_flags(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", claim=False)
    await claims.claim_clients(mock_db, USER, "other-agent", ["g1"])
    run = await _run_all(mock_db, mock_mongo_client, agent, kind="preview")
    card = await mock_db[CARDS].find_one({"run_id": run["_id"]})
    assert card["status"] == "flagged" and card["lines"][0]["mode"] == "flag"


async def test_card_carries_follow_ups_on_the_last_card(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    await mock_db[CARDS].insert_one({
        "_id": "last", "agent_id": agent["_id"], "client_group_id": "g1", "run_id": "r0", "status": "flagged",
        "created_at": datetime(2026, 10, 30), "additional_changes": "",
        "lines": [{"id": "l1", "kind": "remove", "ad_name": "Ad g1", "state": "flagged", "meta_ids": ["ad_g1"]}]})
    run = await _run_all(mock_db, mock_mongo_client, agent)
    card = await mock_db[CARDS].find_one({"run_id": run["_id"]})
    assert card["follow_ups"] == [{"line_id": "l1", "text": "Ad g1: still live", "ok": False}]


async def test_deleting_a_draft_agent_removes_its_runs_and_cards(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", claim=False)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "draft"}})
    await _run_all(mock_db, mock_mongo_client, agent, kind="preview")
    assert await delete_draft_agent(mock_db, USER, agent["_id"])
    assert await mock_db[RUNS].count_documents({}) == 0 and await _cards(mock_db) == []


async def test_publishing_failure_is_recorded_and_auto_still_runs(mock_db, mock_mongo_client, fakes, monkeypatch):
    async def bad(db, run, agent, cards):
        raise RuntimeError("slack down")

    monkeypatch.setattr(runner, "publish_run", bad)
    agent = await _setup(mock_db, "g1")
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["phase"] == "done" and "slack down" in run["publish_error"] and len(fakes.autos) == 1


# ---------------------------------------------------------------------------
# build_card
# ---------------------------------------------------------------------------

FACTS = {"client_group_id": "g1", "name": "Acme", "currency": "GBP", "zone": "red", "decision": "propose",
         "window": {"start": "2026-10-27", "end": "2026-11-02", "prev_start": "2026-10-20", "prev_end": "2026-10-26"},
         "spend": 120, "leads": 10, "cpl": 12.0, "prev_spend": 120, "prev_leads": 10, "prev_cpl": 12.0,
         "ads_live": 2, "ads": [{"ad_id": "o1", "name": "Old one", "spend": 100, "leads": 8, "cpl": 12.5}],
         "zones": {"green_under": 8, "red_over": 10}, "spend_stopped": False}
CANDS = {"add": [{"candidate_id": "g2:n1", "source_client_group_id": "g2", "source_name": "Glow", "ad_id": "n1",
                  "ad_name": "New one", "creative_key": "img:h", "creative": {"image_hash": "h"},
                  "treatment_ids": ["t"], "currency": "USD", "spend": 50, "leads": 10, "cpl": 5.0,
                  "record": {"won": 1, "lost": 0, "moves": 1}}],
         "remove": [{"ad_id": "o1", "ad_name": "Old one", "spend": 100, "leads": 8, "cpl": 12.5}],
         "revive": [{"ad_id": "r1", "ad_name": "Winner", "spend": 80, "leads": 20, "cpl": 4.0}]}
DRAFT = {"add": ["g2:n1"], "remove": ["o1"], "revive": ["r1"], "additional_changes": "Lift the budget.",
         "thought_process": "Swap.", "next_steps": "Check Friday.",
         "flags": [{"id": "g2:n1", "kind": "city", "note": "Says Leeds"}], "failed": False}


def _build(settings=None, draft=DRAFT, decision="propose", **kw):
    return build_card(FACTS, decision, draft, settings or DEFAULT_SETTINGS, "run1", candidates=CANDS, **kw)


def test_build_card_shape():
    card = _build()
    assert card["tag"] == "changes" and card["status"] == "open" and card["run_id"] == "run1"
    assert (card["previous_cpl"], card["current_cpl"], card["currency"]) == (12.0, 12.0, "GBP")
    assert card["client_group_id"] == "g1" and card["window"] == FACTS["window"]
    assert card["slack"] == {"channel_id": None, "ts": None} and card["decided_by"] is None
    assert card["ai_failed"] is False
    add, remove, revive = card["lines"]
    assert (add["kind"], remove["kind"], revive["kind"]) == ("add", "remove", "revive")
    assert len({ln["id"] for ln in card["lines"]}) == 3
    assert add["ad_name"] == "New one" and add["source"] == "Glow" and add["record"]["won"] == 1
    assert add["flags"] == [{"kind": "city", "note": "Says Leeds"}]
    assert add["candidate_id"] == "g2:n1" and add["creative_key"] == "img:h" and add["currency"] == "USD"


def test_every_line_has_state_and_remove_and_revive_have_the_ad_id():
    lines = {ln["kind"]: ln for ln in _build()["lines"]}
    assert all(ln["state"] == "proposed" and ln["mode"] == "ask" and ln["error"] is None for ln in lines.values())
    assert lines["remove"]["meta_ids"] == ["o1"] and lines["revive"]["meta_ids"] == ["r1"]
    assert lines["add"]["meta_ids"] == []  # the ad doesn't exist yet


def test_mode_resolves_per_line_and_flag_lines_start_flagged(monkeypatch):
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    settings = {**DEFAULT_SETTINGS, "modes": {"remove": "auto", "add": "auto", "revive": "ask"},
                "clients": {"g1": {"modes": {"revive": "flag"}}}}
    card = _build(settings)
    lines = {ln["kind"]: ln for ln in card["lines"]}
    assert (lines["remove"]["mode"], lines["remove"]["state"]) == ("auto", "proposed")
    assert (lines["add"]["mode"], lines["add"]["state"]) == ("flag", "flagged")  # Add writes are off
    assert (lines["revive"]["mode"], lines["revive"]["state"]) == ("flag", "flagged")
    assert card["status"] == "open"


def test_all_flag_lines_make_a_flagged_card():
    settings = {**DEFAULT_SETTINGS, "modes": {"remove": "flag", "add": "flag", "revive": "flag"}}
    assert _build(settings)["status"] == "flagged"


def test_original_is_a_separate_copy():
    card = _build()
    card["lines"][0]["ad_name"] = "edited"
    card["next_steps"] = "edited"
    assert card["original"]["lines"][0]["ad_name"] == "New one" and card["original"]["next_steps"] == "Check Friday."


def test_failed_draft_builds_no_lines_whatever_it_holds():
    card = _build(draft={**DRAFT, "failed": True})
    assert card["lines"] == [] and card["additional_changes"] == "" and card["ai_failed"] is True
    assert card["tag"] == "no_changes"


def test_a_decision_that_makes_no_changes_ignores_a_draft():
    assert _build(decision="hold_downtrend")["lines"] == []


def test_built_lines_are_readable_by_follow_ups():
    settings = {**DEFAULT_SETTINGS, "modes": {"remove": "flag", "add": "flag", "revive": "flag"}}
    card = {**_build(settings), "created_at": datetime(2026, 11, 3), "additional_changes": ""}
    state = AdState(ad_id="o1", adset_id="s", campaign_id="c", name="Old one", effective_status="PAUSED",
                    created_time=None, paused_at=None)
    out = {o["line_id"]: o for o in follow_ups(card, {"o1": state}, {})}
    remove = next(ln for ln in card["lines"] if ln["kind"] == "remove")
    add = next(ln for ln in card["lines"] if ln["kind"] == "add")
    assert out[remove["id"]]["ok"] is True  # found through meta_ids
    assert out[add["id"]]["ok"] is None     # flagged Add: not done yet


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

async def test_run_due_agents_never_runs_a_stock_agent(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")
    custom = await create_agent(mock_db, USER)
    await mock_db[AGENTS].update_one({"_id": custom["_id"]}, {"$set": {
        "status": "active", "scope": {"client_group_ids": [], "schedule": {"time": "07:00", "tz": "UTC"}}}})
    seen = []

    async def fake_run(db, mongo_client, a, *, as_of, mode="live", near_miss=False, force_stale=False):
        seen.append(a["_id"])
        from ai.agents.runner import RunResult
        return RunResult()

    async def no_scan(*a, **k):
        return None

    monkeypatch.setattr(agent_jobs, "run_agent", fake_run)
    monkeypatch.setattr(agent_jobs.learning, "scan_proposals", no_scan)
    out = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=NOW)
    assert seen == [custom["_id"]] and out["ran"] == 1
    assert (await mock_db[AGENTS].find_one({"_id": agent["_id"]})).get("last_run_at") is None


async def test_tick_dispatches_custom_and_stock_separately(mock_db, mock_mongo_client, fakes, monkeypatch):
    calls = []

    async def custom(mongo_client, *, now_utc, deadline=None):
        calls.append(("custom", deadline))
        return {"ran": 1, "skipped": 0, "refreshed": 0, "errors": 0, "deferred": 0}

    async def stock(mongo_client, *, now_utc, deadline=None):
        calls.append(("stock", deadline))
        return {"ran": 2}

    class Ctx:
        async def __aenter__(self):
            return mock_mongo_client

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(agent_jobs, "get_mongo_client", lambda: Ctx())
    monkeypatch.setattr(agent_jobs, "run_due_agents", custom)
    monkeypatch.setattr(agent_jobs, "run_due_stock_agents", stock)
    out = await agent_jobs.run_agents_tick()
    assert [c[0] for c in calls] == ["custom", "stock"] and calls[0][1] == calls[1][1] is not None
    assert out["ran"] == 1 and out["stock"] == {"ran": 2}


async def test_stock_failure_leaves_the_custom_result_alone(mock_db, mock_mongo_client, monkeypatch):
    async def custom(mongo_client, *, now_utc, deadline=None):
        return {"ran": 1}

    async def stock(mongo_client, *, now_utc, deadline=None):
        raise RuntimeError("bad")

    class Ctx:
        async def __aenter__(self):
            return mock_mongo_client

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(agent_jobs, "get_mongo_client", lambda: Ctx())
    monkeypatch.setattr(agent_jobs, "run_due_agents", custom)
    monkeypatch.setattr(agent_jobs, "run_due_stock_agents", stock)
    out = await agent_jobs.run_agents_tick()
    assert out["ran"] == 1 and out["stock"] == {"error": "bad"}


async def test_due_stock_agent_runs_once_and_stamps_the_day(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    early = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW - timedelta(minutes=15))
    assert early["ran"] == 0 and await mock_db[RUNS].count_documents({}) == 0
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert out["ran"] == 1 and len(fakes.ai_calls) == 1
    doc = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    assert doc["last_run"]["status"] == "ok" and doc["last_run_at"] is not None
    again = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(minutes=15))
    assert again["ran"] == 0 and len(fakes.ai_calls) == 1


async def test_tick_resumes_an_unfinished_run_past_the_deadline_check(mock_db, mock_mongo_client, fakes, monkeypatch):
    await _setup(mock_db, "g1", "g2")
    monkeypatch.setattr(runner, "_out_of_time", lambda d: len(fakes.ai_calls) >= 1)
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW, deadline=None)
    assert (await mock_db[RUNS].find_one({}))["phase"] == "writing"
    assert (await mock_db[AGENTS].find_one({})).get("last_run_at") is None
    monkeypatch.setattr(runner, "_out_of_time", lambda d: False)
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(minutes=15))
    assert out["ran"] == 1 and (await mock_db[RUNS].find_one({}))["phase"] == "done"
    assert len(fakes.ai_calls) == 2 and await mock_db[RUNS].count_documents({}) == 1


async def test_tick_out_of_time_defers(mock_db, mock_mongo_client, fakes):
    await _setup(mock_db, "g1")
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW, deadline=0.0)
    assert out["deferred"] == 1 and await mock_db[RUNS].count_documents({}) == 0


async def test_a_run_still_going_after_local_midnight_keeps_going(mock_db, mock_mongo_client, fakes, monkeypatch):
    clients = await _client(mock_db, "g1", synced=datetime(2026, 11, 3, 2))
    agent = await _agent(mock_db, clients)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"scope.schedule.time": "23:30"}})
    late_evening = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
    monkeypatch.setattr(runner, "_out_of_time", lambda d: len(fakes.health_reads) >= 1)
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=late_evening)
    assert (await mock_db[RUNS].find_one({}))["phase"] != "done"

    monkeypatch.setattr(runner, "_out_of_time", lambda d: False)
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=late_evening + timedelta(minutes=45))
    run = await mock_db[RUNS].find_one({})
    assert run["phase"] == "done" and not run.get("ended_early") and run["local_date"] == "2026-11-03"
    assert await mock_db[RUNS].count_documents({}) == 1

    # Finishing after midnight doesn't use up the new day's run.
    fresh = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    assert agent_jobs._is_due(fresh, late_evening + timedelta(days=1))


async def test_a_run_ends_24_hours_after_its_scheduled_time(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    sched = NOW - timedelta(days=1)
    mon = await get_or_create_run(mock_db, agent, sched + timedelta(minutes=10), scheduled_utc=sched)
    await runner.end_overdue_runs(mock_db, agent, sched + timedelta(hours=23, minutes=59))
    assert (await mock_db[RUNS].find_one({"_id": mon["_id"]}))["phase"] != "done"

    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    old = await mock_db[RUNS].find_one({"_id": mon["_id"]})
    assert old["phase"] == "done" and old["summary"]["missing"] == ["g1"]
    assert out["ran"] == 1 and await mock_db[RUNS].count_documents({"local_date": "2026-11-03"}) == 1


async def test_agents_advance_least_recently_first(mock_db, mock_mongo_client, fakes, monkeypatch):
    a = await _agent(mock_db, await _client(mock_db, "g1"))
    b = await create_agent(mock_db, "u2", type=TYPE_STOCK)
    await mock_db[AGENTS].update_one({"_id": b["_id"]}, {"$set": {
        "status": "active", "scope": {"client_group_ids": [], "schedule": {"time": "07:00", "tz": "UTC"}},
        "stock_advanced_at": datetime(2026, 11, 3, 6)}})
    await mock_db[AGENTS].update_one({"_id": a["_id"]}, {"$set": {"stock_advanced_at": datetime(2026, 11, 3, 6, 15)}})
    order = []
    real = runner.advance_run

    async def spy(db, mc, run, agent, *, deadline, now_utc=None):
        order.append(agent["_id"])
        return await real(db, mc, run, agent, deadline=deadline, now_utc=now_utc)

    monkeypatch.setattr(runner, "advance_run", spy)
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert order == [b["_id"], a["_id"]]
    assert (await mock_db[AGENTS].find_one({"_id": a["_id"]}))["stock_advanced_at"] > datetime(2026, 11, 3, 6, 15)


async def test_a_run_another_tick_holds_doesnt_count_as_ran(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": datetime.utcnow()}})
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert out["ran"] == 0 and out["skipped"] == 1 and fakes.reads == []


async def test_custom_agent_with_no_type_field_still_runs(mock_db, mock_mongo_client, monkeypatch):
    await mock_db[AGENTS].insert_one({"_id": "old", "user_id": USER, "status": "active",
                                      "scope": {"client_group_ids": [], "schedule": {"time": "07:00", "tz": "UTC"}}})
    seen = []

    async def fake_run(db, mongo_client, a, *, as_of, mode="live", near_miss=False, force_stale=False):
        seen.append(a["_id"])
        from ai.agents.runner import RunResult
        return RunResult()

    async def no_scan(*a, **k):
        return None

    monkeypatch.setattr(agent_jobs, "run_agent", fake_run)
    monkeypatch.setattr(agent_jobs.learning, "scan_proposals", no_scan)
    await agent_jobs.run_due_agents(mock_mongo_client, now_utc=NOW)
    assert seen == ["old"]
    assert (await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW))["ran"] == 0


async def _tz_agent(db, tz, time="07:00"):
    agent = await _setup(db, "g1", synced=datetime(2026, 11, 3, 2))
    await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"scope.schedule": {"time": time, "tz": tz}}})
    return await db[AGENTS].find_one({"_id": agent["_id"]})


async def test_non_utc_schedule_uses_the_agencys_date_everywhere(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    await _tz_agent(mock_db, "Asia/Tokyo")
    # 07:00 Tuesday in Tokyo is still Monday in UTC. Batch B runs Tuesdays.
    tokyo = datetime(2026, 11, 2, 22, tzinfo=UTC)
    assert (await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=tokyo - timedelta(minutes=15)))["ran"] == 0
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=tokyo)
    run = await mock_db[RUNS].find_one({})
    assert run["local_date"] == "2026-11-03" and (run["kind"], run["batch_id"]) == ("batch", "b")
    assert fakes.stored[0][1] == date(2026, 11, 3)  # what paused_at is dated from
    assert next(a for a in run["alerts"] if a["code"] == "account_disabled")["since"] == "2026-11-03"


async def test_dst_gap_runs_once_on_the_day(mock_db, mock_mongo_client, fakes):
    await _tz_agent(mock_db, "America/New_York", time="02:30")  # 02:30 doesn't exist on 8 March 2026
    before = datetime(2026, 3, 8, 6, 45, tzinfo=UTC)  # 01:45 EST
    assert (await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=before))["ran"] == 0
    first = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)    # 03:00 EDT
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=first)
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=first + timedelta(minutes=15))
    runs = await mock_db[RUNS].find({}).to_list(None)
    assert [r["local_date"] for r in runs] == ["2026-03-08"] and runs[0]["kind"] == "alert_only"


async def test_dst_fall_back_doesnt_double_the_run(mock_db, mock_mongo_client, fakes):
    await _tz_agent(mock_db, "America/New_York", time="01:30")  # 01:30 happens twice on 1 Nov 2026
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=first)
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=first + timedelta(hours=1))  # 01:30 EST
    assert [r["local_date"] for r in await mock_db[RUNS].find({}).to_list(None)] == ["2026-11-01"]


async def test_lost_create_race_returns_the_winners_run(mock_db, fakes):
    agent = await _setup(mock_db, "g1")
    winner = {"_id": "winner", "agent_id": agent["_id"], "local_date": "2026-11-03", "scheduled": True}

    class Racing:
        def __init__(self, coll):
            self.coll = coll

        async def update_one(self, *a, **k):
            await self.coll.insert_one(winner)
            raise DuplicateKeyError("E11000 duplicate key error")

        def __getattr__(self, name):
            return getattr(self.coll, name)

    class Db:
        def __getitem__(self, name):
            return Racing(mock_db[name]) if name == RUNS else mock_db[name]

    assert (await get_or_create_run(Db(), agent, NOW))["_id"] == "winner"


async def test_other_create_errors_are_raised(mock_db, fakes):
    agent = await _setup(mock_db, "g1")

    class Db:
        def __getitem__(self, name):
            class Broken:
                async def update_one(self, *a, **k):
                    raise RuntimeError("db down")
            return Broken()

    with pytest.raises(RuntimeError, match="db down"):
        await get_or_create_run(Db(), agent, NOW)


async def test_windows_are_stable_across_ticks(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")
    monkeypatch.setattr(runner, "_out_of_time", lambda d: True)
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    monkeypatch.setattr(runner, "_out_of_time", lambda d: False)
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(days=1))  # a much later tick
    assert runner._run_now(run) == NOW
    [card] = await _cards(mock_db)
    assert card["window"] == {"start": "2026-10-27", "end": "2026-11-02",
                              "prev_start": "2026-10-20", "prev_end": "2026-10-26"}


# ---------------------------------------------------------------------------
# Rate limits with a scope
# ---------------------------------------------------------------------------

def _limit(scope="account", retry_after_s=None):
    return MetaRateLimit("slow down", scope=scope, retry_after_s=retry_after_s)


async def _share_account(db, account, *gids):
    await db.client_groups.update_many({"id": {"$in": list(gids)}}, {"$set": {"meta_ad_account_id": account}})


def _naive(dt):
    return dt.replace(tzinfo=None)


async def test_one_throttled_account_leaves_the_other_accounts_running(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1", "g2", "g3", "g4", "g5")
    await _share_account(mock_db, "act_g1", "g1", "g2", "g3")
    fakes.read_error["act_g1"] = _limit()
    fakes.health_error["act_g1"] = _limit()
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)

    assert fakes.reads == ["act_g1", "act_g4", "act_g5"]
    assert "act_g1" in fakes.health_reads  # tried even though its ads read was throttled
    assert {a["client_group_id"] for a in run["alerts"] if a["code"] == "account_disabled"} == {"g4", "g5"}
    assert {c["client_group_id"] for c in await _cards(mock_db)} == {"g4", "g5"}
    for gid in ("g1", "g2", "g3"):
        c = run["clients"][gid]
        assert c["state"] == "pending" and c["attempts"] == 0 and c["rate_limited"]
        assert c["retry_after"] == _naive(NOW) + runner.RETRY_AFTER
    assert run["attempts"] == 0 and not run.get("rate_limited") and run["phase"] == "waiting"
    assert sorted(run["summary"]["waiting"]) == ["g1", "g2", "g3"]


async def test_throttled_account_waits_for_metas_time_then_carries_on(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    fakes.read_error["act_g1"] = _limit(retry_after_s=3600)
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["clients"]["g1"]["retry_after"] == _naive(NOW) + timedelta(hours=1)
    assert run["clients"]["g2"]["state"] == "done"

    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=30))
    assert fakes.reads.count("act_g1") == 1 and run["clients"]["g1"]["state"] == "pending"

    fakes.read_error.clear()
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=61))
    assert run["phase"] == "done" and run["clients"]["g1"]["state"] == "done"
    assert not run["clients"]["g1"]["rate_limited"] and len(await _cards(mock_db)) == 2


async def test_throttled_ads_read_still_gets_the_health_alerts_once(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1")
    fakes.read_error["act_g1"] = [_limit()]
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert [a["code"] for a in run["alerts"]] == ["account_disabled"] and fakes.urgent[-1] == ["account_disabled"]
    assert run["clients"]["g1"]["state"] == "pending"
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "done" and fakes.health_reads == ["act_g1"] and len(await _cards(mock_db)) == 1


async def test_throttled_health_read_alone_is_tried_again_at_no_cost(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1")
    fakes.health_error["act_g1"] = [_limit()]
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["clients"]["g1"]["state"] == "pending" and run["clients"]["g1"]["attempts"] == 0
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "done" and "account_disabled" in [a["code"] for a in run["alerts"]]


async def test_app_wide_limit_still_stops_the_agents_tick(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    fakes.read_error["act_g1"] = _limit(scope="app")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert fakes.reads == ["act_g1"] and fakes.health_reads == [] and run["rate_limited"]
    assert run["phase"] == "alerts" and run["clients"]["g1"]["attempts"] == 0
    assert not run["clients"]["g1"].get("retry_after") and await _cards(mock_db) == []


async def test_app_wide_limit_on_the_health_read_stops_the_tick_too(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    fakes.health_error["act_g1"] = _limit(scope="app")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert fakes.reads == ["act_g1"] and run["rate_limited"] and run["phase"] == "alerts"
    assert not run["clients"]["g1"].get("retry_after")


async def test_urgent_hook_ends_every_alerts_pass_and_covers_spend_stopped(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")  # spent on the 1st, nothing on the 2nd
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert any(a["code"] == "spend_stopped" for a in run["alerts"])
    assert fakes.urgent[0] == [a["code"] for a in run["alerts"]] and "spend_stopped" in fakes.urgent[0]
    assert "urgent_posted" not in run


async def test_urgent_hook_runs_each_pass_not_once_a_run(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1", "g2")
    fakes.read_error["act_g2"] = _limit()
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert [c for c in fakes.urgent[-1] if c == "account_disabled"] == ["account_disabled"] * 2
    calls = len(fakes.urgent)
    fakes.read_error.clear()
    await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert len(fakes.urgent) == calls + 1


async def test_urgent_hook_runs_when_a_rate_limit_stops_the_pass(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1", "g2")
    fakes.read_error["act_g2"] = _limit(scope="app")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["rate_limited"] and fakes.urgent == [["account_disabled"]]


async def test_urgent_hook_runs_when_the_deadline_stops_the_pass(mock_db, mock_mongo_client, fakes, monkeypatch):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1", "g2")
    fast = alerts_mod.fetch_account_health

    async def slow(account, token):
        await asyncio.sleep(0.08)
        return await fast(account, token)

    monkeypatch.setattr(alerts_mod, "fetch_account_health", slow)
    run = await get_or_create_run(mock_db, agent, NOW)
    run = await advance_run(mock_db, mock_mongo_client, run, agent, deadline=time.monotonic() + 0.04, now_utc=NOW)
    assert fakes.health_reads == ["act_g1"] and run["phase"] == "alerts"
    assert fakes.urgent == [["account_disabled"]]


async def test_urgent_hook_runs_when_a_run_ends_incomplete(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"alerts": [
        {"client_group_id": "g1", "code": "account_disabled", "detail": "x"}]}})
    await runner.end_overdue_runs(mock_db, agent, NOW + timedelta(hours=24))
    assert fakes.urgent == [["account_disabled"]] and (await mock_db[RUNS].find_one({}))["phase"] == "done"


async def test_a_hook_error_never_stops_the_run(mock_db, mock_mongo_client, fakes, monkeypatch):
    async def broken(db, run, agent):
        raise RuntimeError("slack down")

    monkeypatch.setattr(runner, "post_urgent_alerts", broken)
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["phase"] == "done" and run["attempts"] == 0 and "slack down" in run["publish_error"]
    assert len(await _cards(mock_db)) == 1 and len(fakes.published) == 1


async def test_a_hook_error_doesnt_stop_an_incomplete_run_publishing(mock_db, mock_mongo_client, fakes, monkeypatch):
    async def broken(db, run, agent):
        raise RuntimeError("slack down")

    monkeypatch.setattr(runner, "post_urgent_alerts", broken)
    agent = await _setup(mock_db, "g1")
    await get_or_create_run(mock_db, agent, NOW)
    await runner.end_overdue_runs(mock_db, agent, NOW + timedelta(hours=24))
    assert (await mock_db[RUNS].find_one({}))["phase"] == "done" and len(fakes.published) == 1


async def test_previews_never_call_the_hook(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    agent = await _setup(mock_db, "g1")
    run = await _run_all(mock_db, mock_mongo_client, agent, kind="preview")
    assert run["phase"] == "done" and any(a["code"] == "account_disabled" for a in run["alerts"])
    assert fakes.urgent == []
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"phase": "alerts", "attempts": 3}})
    await _tick(mock_db, mock_mongo_client, {**run, "phase": "alerts"}, agent)  # ends it incomplete
    assert fakes.urgent == []


async def test_urgent_hook_runs_when_the_pass_raises(mock_db, mock_mongo_client, fakes, monkeypatch):
    fakes.health = {**fakes.health, "account_status": 2}

    async def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(runner, "_spend_stage", broken)
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert fakes.urgent == [["account_disabled"]] and run["attempts"] == 1 and "db down" in run["last_error"]


async def test_transient_health_read_is_retried_without_holding_the_card(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    fakes.health_error["act_g1"] = [RuntimeError("500")]
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert not any(a["code"] == "check_failed" for a in run["alerts"])
    assert run["clients"]["g1"]["state"] == "done" and len(await _cards(mock_db)) == 1
    assert run["phase"] == "waiting" and len(fakes.published) == 1

    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert "account_disabled" in [a["code"] for a in run["alerts"]]
    assert not any(a["code"] == "check_failed" for a in run["alerts"])
    assert "account_disabled" in fakes.urgent[-1]
    assert run["phase"] == "done" and fakes.health_reads == ["act_g1", "act_g1"]
    assert len(fakes.published) == 2 and run["summary"]["alerts"] == len(run["alerts"])
    assert len(await _cards(mock_db)) == 1 and fakes.autos  # the card wasn't made again


async def test_health_read_that_keeps_failing_says_so_after_three_tries(mock_db, mock_mongo_client, fakes):
    fakes.health_error["act_g1"] = RuntimeError("500")
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    for n in range(3):
        run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15 * n))
    assert fakes.health_reads == ["act_g1"] * runner.CLIENT_ATTEMPTS and run["phase"] == "done"
    assert [a["code"] for a in run["alerts"] if a["code"] == "check_failed"] == ["check_failed"]
    assert all("read_failed" not in a for a in run["alerts"])
    assert run["clients"]["g1"]["state"] == "done" and len(await _cards(mock_db)) == 1


async def test_retry_waits_for_a_later_tick(mock_db, mock_mongo_client, fakes):
    fakes.health_error["act_g1"] = [RuntimeError("500")]
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=5))
    assert fakes.health_reads == ["act_g1"] and run["phase"] == "waiting"


async def test_alert_only_day_retries_a_transient_health_read(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    fakes.health_error["act_g1"] = [RuntimeError("500")]
    agent = await _setup(mock_db, "g1")
    wed = NOW + timedelta(days=1)
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, wed), agent, wed)
    assert run["kind"] == "alert_only" and run["phase"] == "waiting"
    run = await _tick(mock_db, mock_mongo_client, run, agent, wed + timedelta(minutes=15))
    assert run["phase"] == "done" and "account_disabled" in [a["code"] for a in run["alerts"]]


async def test_preview_doesnt_wait_to_retry_a_health_read(mock_db, mock_mongo_client, fakes):
    fakes.health_error["act_g1"] = RuntimeError("500")
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW, kind="preview"), agent)
    assert run["phase"] == "done" and fakes.health_reads == ["act_g1"]
    assert any(a["code"] == "check_failed" for a in run["alerts"])


async def test_run_ending_with_a_retry_pending_reports_the_failed_check(mock_db, mock_mongo_client, fakes):
    fakes.health_error["act_g1"] = RuntimeError("500")
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    await runner.end_overdue_runs(mock_db, agent, NOW + timedelta(hours=24))
    run = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert run["phase"] == "done" and any(a["code"] == "check_failed" for a in run["alerts"])
    assert "check_failed" in fakes.urgent[-1] and run["clients"]["g1"]["state"] == "done"


async def test_dead_token_during_a_health_retry_ends_the_retries(mock_db, mock_mongo_client, fakes):
    fakes.health_error["act_g1"] = [RuntimeError("500")]
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    fakes.token = None
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "done" and run["reconnect"] and run["clients"]["g1"]["state"] == "done"
    assert "meta_reconnect" in [a["code"] for a in run["alerts"]]


async def test_throttled_health_retry_waits_at_no_cost(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "account_status": 2}
    fakes.health_error["act_g1"] = [RuntimeError("500"), _limit(retry_after_s=3600)]
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["phase"] == "waiting" and run["clients"]["g1"]["health_attempts"] == 1
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=30))
    assert len(fakes.health_reads) == 2
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=80))
    assert run["phase"] == "done" and "account_disabled" in [a["code"] for a in run["alerts"]]


async def test_health_retry_of_a_throttled_pending_client_waits_for_the_limit(mock_db, mock_mongo_client, fakes):
    fakes.read_error["act_g1"] = _limit(retry_after_s=3600)
    fakes.health_error["act_g1"] = [RuntimeError("500")]
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["clients"]["g1"]["state"] == "pending" and run["clients"]["g1"]["health_due"]
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert fakes.health_reads == ["act_g1"]  # due, but its account is still throttled


async def test_health_retries_stop_at_the_deadline(mock_db, mock_mongo_client, fakes, monkeypatch):
    fakes.health_error = {"act_g1": [RuntimeError("500")], "act_g2": [RuntimeError("500")]}
    agent = await _setup(mock_db, "g1", "g2")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert len(fakes.health_reads) == 2
    fast = alerts_mod.fetch_account_health

    async def slow(account, token):
        await asyncio.sleep(0.08)
        return await fast(account, token)

    monkeypatch.setattr(alerts_mod, "fetch_account_health", slow)
    await advance_run(mock_db, mock_mongo_client, run, agent, deadline=time.monotonic() + 0.04,
                      now_utc=NOW + timedelta(minutes=15))
    assert len(fakes.health_reads) == 3


async def test_prepaid_alerts_come_with_the_health_pass(mock_db, mock_mongo_client, fakes):
    fakes.health = {**fakes.health, "is_prepay_account": True}
    await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))  # late on spend data, so it waits
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    run = await mock_db[RUNS].find_one({})
    assert run["clients"]["g1"]["state"] == "waiting"
    assert "prepaid_no_topups" in [a["code"] for a in run["alerts"]] and "prepaid_no_topups" in fakes.urgent[0]


async def test_prepaid_spend_read_failure_is_retried_like_a_health_read(mock_db, mock_mongo_client, fakes, monkeypatch):
    calls = []

    async def spend(account, token, since, until):
        calls.append(account)
        if len(calls) == 1:
            raise RuntimeError("500")
        return {"2026-11-02": 100.0, "2026-11-01": 100.0, "2026-10-31": 100.0}

    monkeypatch.setattr(alerts_mod, "fetch_account_spend", spend)
    fakes.health = {**fakes.health, "is_prepay_account": None}
    clients = await _client(mock_db, "g1", prepaid={"topups": [{"date": "2026-10-28", "amount": 200}]})
    agent = await _agent(mock_db, clients)
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert not any(a["code"] == "check_failed" for a in run["alerts"]) and run["phase"] == "waiting"
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert len(calls) == 2 and "prepaid_low" in [a["code"] for a in run["alerts"]] and run["phase"] == "done"


def _closing_card(run, gid):
    return {"_id": f"card_{gid}", "run_id": run["_id"], "client_group_id": gid, "agent_id": run["agent_id"],
            "user_id": USER, "status": "closing", "final_status": "open", "created_at": datetime(2026, 11, 3)}


async def test_giving_up_on_a_client_opens_its_stored_card_first(mock_db, mock_mongo_client, fakes, monkeypatch):
    real, left = runner.supersede_open, [1, 1, 1]

    async def flaky(*a, **k):
        if left:
            left.pop()
            raise RuntimeError("db blip")
        return await real(*a, **k)

    monkeypatch.setattr(runner, "supersede_open", flaky)
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    for n in range(3):
        run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15 * n))
    [card] = await _cards(mock_db)
    assert run["clients"]["g1"]["state"] == "done" and card["status"] == "open" and len(fakes.ai_calls) == 1


async def test_giving_up_stays_failed_when_the_card_cant_open(mock_db, mock_mongo_client, fakes, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(runner, "supersede_open", broken)
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    for n in range(3):
        run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15 * n))
    [card] = await _cards(mock_db)
    assert run["clients"]["g1"]["state"] == "failed" and card["status"] == "closing" and run["phase"] != "alerts"


async def test_takeover_giving_up_opens_the_stored_card(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[CARDS].insert_one(_closing_card(run, "g1"))
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {
        "phase": "writing", "in_flight": ["g1"], "clients.g1.state": "needs_writing", "clients.g1.attempts": 2,
        "claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    [card] = await _cards(mock_db)
    assert run["clients"]["g1"]["state"] == "done" and card["status"] == "open"


async def test_reconnect_giving_up_opens_the_stored_card(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[CARDS].insert_one(_closing_card(run, "g1"))
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"clients.g1.state": "needs_writing"}})
    fakes.token = None
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    [card] = await _cards(mock_db)
    assert run["clients"]["g1"]["state"] == "done" and card["status"] == "open"
    assert run["clients"]["g2"]["state"] == "failed" and run["clients"]["g2"]["error"] == runner.RECONNECT


async def test_catch_up_killed_once_moves_on_and_charges_nobody(mock_db, mock_mongo_client, fakes, monkeypatch):
    calls, in_flight = [], []

    async def killed(db, mongo_client, run, agent):
        calls.append(run["_id"])
        in_flight.append((await db[RUNS].find_one({"_id": run["_id"]}))["in_flight"])
        raise Killed("function killed")

    monkeypatch.setattr(runner, "catch_up_run", killed)
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    with pytest.raises(Killed):
        await _tick(mock_db, mock_mongo_client, run, agent)
    assert in_flight == [[]]
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert len(calls) == 1 and run["phase"] == "done" and not run.get("ended_early")
    assert run["clients"]["g1"]["state"] == "done" and len(await _cards(mock_db)) == 1


async def test_catch_up_error_still_moves_on_once(mock_db, mock_mongo_client, fakes, monkeypatch):
    calls = []

    async def broken(db, mongo_client, run, agent):
        calls.append(1)
        raise RuntimeError("scoring failed")

    monkeypatch.setattr(runner, "catch_up_run", broken)
    agent = await _setup(mock_db, "g1")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["phase"] == "done" and calls == [1] and len(await _cards(mock_db)) == 1


async def test_error_after_the_ai_call_doesnt_repeat_it(mock_db, mock_mongo_client, fakes, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("bad draft")

    monkeypatch.setattr(runner, "build_card", broken)
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    for n in range(4):
        run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15 * n))
    assert len(fakes.ai_calls) == 1 and run["clients"]["g1"]["state"] == "failed"
    assert not run["clients"]["g1"].get("draft")


async def test_stored_draft_builds_the_card_on_the_retry(mock_db, mock_mongo_client, fakes, monkeypatch):
    real, left = runner.build_card, [1]

    def flaky(*a, **k):
        if a[2] is not None and left:
            left.pop()
            raise RuntimeError("bad draft")
        return real(*a, **k)

    monkeypatch.setattr(runner, "build_card", flaky)
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    for n in range(2):
        run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15 * n))
    [card] = await _cards(mock_db)
    assert len(fakes.ai_calls) == 1 and card["thought_process"] == "Swap the worst ad."
    assert run["clients"]["g1"]["state"] == "done" and not run["clients"]["g1"].get("draft")


@pytest.mark.parametrize("scope", ["app", "account"])
async def test_rate_limited_run_doesnt_say_it_ran_out_of_time(mock_db, mock_mongo_client, fakes, scope):
    agent = await _setup(mock_db, "g1")
    fakes.read_error["act_g1"] = _limit(scope=scope)
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    await runner.end_overdue_runs(mock_db, agent, NOW + timedelta(hours=24))
    run = await mock_db[RUNS].find_one({"_id": run["_id"]})
    error = run["clients"]["g1"]["error"]
    assert run["clients"]["g1"]["state"] == "failed" and "ran out of time" not in error and "limit" in error
    assert run["summary"]["could_not_check"] == [{"client_group_id": "g1", "reason": error}]


async def test_rate_limit_is_cleared_when_it_passes(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    fakes.read_error["act_g1"] = [_limit(scope="app")]
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["rate_limited"]
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(minutes=15))
    assert run["rate_limited"] is None and run["phase"] == "done"


async def test_top_up_is_requested_even_when_grace_has_passed(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))
    fakes.read_error["act_g1"] = [_limit(retry_after_s=3 * 3600)]
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert not (await mock_db[history.HISTORY].find_one({"_id": "g1"})).get("topup_requested_at")
    run = await _tick(mock_db, mock_mongo_client, run, agent, NOW + timedelta(hours=3, minutes=1))
    assert run["clients"]["g1"]["state"] == "data_late"
    assert (await mock_db[history.HISTORY].find_one({"_id": "g1"}))["topup_requested_at"]


async def test_newer_card_closes_old_flagged_cards_too(mock_db):
    rows = [{"_id": s, "agent_id": "ag", "client_group_id": "g1", "run_id": "old", "status": s}
            for s in ("open", "flagged", "applied", "dismissed", "partly_applied")]
    rows.append({"_id": "preview", "agent_id": "ag", "client_group_id": "g1", "run_id": "old", "status": "flagged",
                 "preview": True})
    rows.append({"_id": "mine", "agent_id": "ag", "client_group_id": "g1", "run_id": "new", "status": "flagged"})
    await mock_db[CARDS].insert_many(rows)
    assert await supersede_open(mock_db, "ag", "g1", "new") == 2
    got = {c["_id"]: c["status"] for c in await _cards(mock_db)}
    assert got == {"open": "superseded", "flagged": "superseded", "applied": "applied", "dismissed": "dismissed",
                   "partly_applied": "partly_applied", "preview": "flagged", "mine": "flagged"}


async def test_flagged_card_still_gets_its_follow_ups_from_the_next_run(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", claim=False)  # flag-only, so its card is flagged
    await _run_all(mock_db, mock_mongo_client, agent)
    [first] = await _cards(mock_db)
    assert first["status"] == "flagged"
    await seed_history(mock_db, "g1", last_topup_at=datetime(2026, 11, 10, 2))
    await mock_db[AD_DAYS].insert_one({
        "user_id": USER, "client_group_id": "g1", "ad_id": "ad_g1", "campaign_id": "c1", "ad_name": "Ad g1",
        "date_start": "2026-11-08", "insight_data": {"spend": "120", "actions": [{"action_type": "lead", "value": "10"}]}})
    await _run_all(mock_db, mock_mongo_client, agent, NOW + timedelta(days=7))
    assert fakes.ai_calls[-1]["last_card"]["_id"] == first["_id"]
    assert (await mock_db[CARDS].find_one({"_id": first["_id"]}))["status"] == "superseded"


# ---------------------------------------------------------------------------
# Behaviour the mutation run found untested
# ---------------------------------------------------------------------------

async def test_batch_client_without_a_card_doesnt_wait_for_late_rows(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", batch="a", synced=datetime(2026, 11, 2, 20))
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["kind"] == "batch" and run["clients"]["g1"]["state"] == "alerts_only"
    assert "spend_data_late" in [a["code"] for a in run["alerts"]]


async def test_last_card_ignores_a_card_left_closing(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    base = {"agent_id": agent["_id"], "client_group_id": "g1", "run_id": "old"}
    await mock_db[CARDS].insert_one({**base, "_id": "real", "status": "applied", "created_at": datetime(2026, 10, 1)})
    await mock_db[CARDS].insert_one({**base, "_id": "orphan", "run_id": "older", "status": "closing",
                                     "created_at": datetime(2026, 10, 20)})
    await _run_all(mock_db, mock_mongo_client, agent)
    assert fakes.ai_calls[0]["last_card"]["_id"] == "real"


async def test_moving_to_a_new_state_gives_the_client_fresh_attempts(mock_db, mock_mongo_client, fakes, monkeypatch):
    real_check, real_write = runner._check_client, runner.write_card
    check_fails, write_fails = [1], [1, 1]

    async def check(*a, **k):
        if check_fails:
            check_fails.pop()
            raise RuntimeError("blip")
        return await real_check(*a, **k)

    async def write(*a, **k):
        if write_fails:
            write_fails.pop()
            raise RuntimeError("blip")
        return await real_write(*a, **k)

    monkeypatch.setattr(runner, "_check_client", check)
    monkeypatch.setattr(runner, "write_card", write)
    agent = await _setup(mock_db, "g1")
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "done" and len(await _cards(mock_db)) == 1


async def test_progress_on_one_client_resets_the_runs_attempts(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"attempts": 1}})
    fakes.read_error["act_g2"] = Killed("function killed")
    with pytest.raises(Killed):
        await _tick(mock_db, mock_mongo_client, run, agent)
    assert (await mock_db[RUNS].find_one({"_id": run["_id"]}))["attempts"] == 0


async def test_hung_ads_read_is_cut_off_and_retried(mock_db, mock_mongo_client, fakes, monkeypatch):
    fast = runner.read_meta_state

    async def hung(account, token):
        if account == "act_g1":
            await asyncio.sleep(0.5)
        return await fast(account, token)

    monkeypatch.setattr(runner, "read_meta_state", hung)
    monkeypatch.setattr(runner, "CLIENT_READ_TIMEOUT_S", 0.01)
    agent = await _setup(mock_db, "g1", "g2")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["clients"]["g1"]["state"] == "waiting" and run["clients"]["g1"]["attempts"] == 1
    assert run["clients"]["g2"]["state"] == "done"


async def test_deadline_after_the_ads_read_skips_the_health_read(mock_db, mock_mongo_client, fakes, monkeypatch):
    async def slow(*a, **k):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(runner, "store_meta_state", slow)
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    await advance_run(mock_db, mock_mongo_client, run, agent, deadline=time.monotonic() + 0.02, now_utc=NOW)
    assert fakes.stored == [] and fakes.health_reads == []


async def test_takeover_only_charges_clients_still_unfinished(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {
        "in_flight": ["g1", "g2"], "clients.g1.state": "done", "clients.g1.attempts": 2,
        "claimed_at": datetime.utcnow() - timedelta(hours=1)}})
    run = await _tick(mock_db, mock_mongo_client, run, agent)
    assert run["clients"]["g1"]["state"] == "done" and run["clients"]["g1"]["attempts"] == 2


async def test_takeover_clears_in_flight_so_a_client_isnt_charged_twice(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    await mock_db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"in_flight": ["g1"]}})
    run = await mock_db[RUNS].find_one({"_id": run["_id"]})
    await runner._count_takeover(mock_db, run)
    stored = await mock_db[RUNS].find_one({"_id": run["_id"]})
    assert stored["in_flight"] == [] and stored["clients"]["g1"]["attempts"] == 1


async def test_failed_store_is_retried_and_others_go_on(mock_db, mock_mongo_client, fakes, monkeypatch):
    fast = runner.store_meta_state

    async def store(db, user_id, gid, account, ads, adsets, *, today):
        if gid == "g1":
            raise RuntimeError("write failed")
        return await fast(db, user_id, gid, account, ads, adsets, today=today)

    monkeypatch.setattr(runner, "store_meta_state", store)
    agent = await _setup(mock_db, "g1", "g2")
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, NOW), agent)
    assert run["clients"]["g1"]["state"] == "waiting" and run["clients"]["g1"]["attempts"] == 1
    assert run["clients"]["g2"]["state"] == "done" and run["attempts"] == 0


async def test_client_without_an_account_fails_and_says_why(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    await mock_db.client_groups.update_one({"id": "g1"}, {"$set": {"meta_ad_account_id": None}})
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "failed" and "no Meta ad account" in run["clients"]["g1"]["error"]
    assert run["clients"]["g2"]["state"] == "done" and fakes.reads == ["act_g2"]


async def test_no_ai_card_is_flag_only_for_a_client_the_agent_doesnt_hold(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", spend=40, prev=(40, 10), claim=False)  # green: no AI call
    await _run_all(mock_db, mock_mongo_client, agent)
    [card] = await _cards(mock_db)
    assert fakes.ai_calls == [] and card["status"] == "flagged"


async def test_spend_alerts_are_dated_in_the_agencys_day(mock_db, mock_mongo_client, fakes):
    at = datetime(2026, 11, 3, 23, tzinfo=UTC)  # already the 4th in Tokyo
    agent = await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"scope.schedule.tz": "Asia/Tokyo"}})
    agent = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    run = await _tick(mock_db, mock_mongo_client, await get_or_create_run(mock_db, agent, at), agent, at)
    late = next(a for a in run["alerts"] if a["code"] == "spend_data_late")
    assert run["local_date"] == "2026-11-04" and late["since"] == "2026-11-04"


async def test_run_keeps_the_scheduled_time_when_its_tick_is_late(mock_db, mock_mongo_client, fakes):
    await _setup(mock_db, "g1")
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(minutes=40))
    run = await mock_db[RUNS].find_one({})
    assert run["scheduled_at"] == _naive(NOW) and run["as_of"] == _naive(NOW) + timedelta(minutes=40)
