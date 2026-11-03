"""The stock agent's cards and resumable run state machine (spec sections 6 and 7)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from agent_history_helpers import seed_history
from ai.agents import claims, history
from ai.agents.meta_state import MetaTokenError
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
        self.ai_calls, self.refreshed, self.health_reads, self.published, self.autos = [], [], [], [], []
        self.order = []
        self.draft = None        # dict or callable(facts) -> dict
        self.refresh_error = {}  # gid -> exception
        self.token = {"access_token": "t"}
        self.health = {"account_status": 1, "funding_source": "card", "is_prepay_account": False,
                       "timezone_name": "UTC", "currency": "GBP", "name": "x"}


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()

    async def token(user_id, mongo_client):
        return f.token

    async def refresh(db, mongo_client, user_id, gid, account, *, today):
        f.order.append(("refresh", gid))
        f.refreshed.append(gid)
        if gid in f.refresh_error:
            raise f.refresh_error[gid]
        return {}

    async def backfill(db, user_id, gid):
        return 0

    async def health(account, tok):
        f.order.append(("health", account))
        f.health_reads.append(account)
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

    monkeypatch.setattr(runner, "get_facebook_token", token)
    monkeypatch.setattr(runner, "refresh_meta_state", refresh)
    monkeypatch.setattr(runner, "backfill_paused_at", backfill)
    monkeypatch.setattr(alerts_mod, "fetch_account_health", health)
    monkeypatch.setattr(runner, "write_card", write)
    monkeypatch.setattr(runner, "publish_run", publish)
    monkeypatch.setattr(runner, "auto_apply_run", auto)
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


async def _run_all(db, client, agent, now=NOW, **kw):
    run = await get_or_create_run(db, agent, now, **kw)
    return await advance_run(db, client, run, agent, deadline=None)


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
    assert done["phase"] == "done" and done["attempts"] == 1 and len(await _cards(mock_db)) == 1


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
    agent = await _setup(mock_db, "g1", "g2", "g3")
    fakes.refresh_error["g1"] = RuntimeError("Meta said no")
    real = runner.client_facts

    async def facts(db, agent, group, window):
        if group["id"] == "g2":
            raise ValueError("bad row")
        return await real(db, agent, group, window)

    monkeypatch.setattr(runner, "client_facts", facts)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["phase"] == "done"
    assert run["clients"]["g1"]["state"] == "failed" and "Meta said no" in run["clients"]["g1"]["error"]
    assert run["clients"]["g2"]["state"] == "failed" and "bad row" in run["clients"]["g2"]["error"]
    assert run["clients"]["g3"]["state"] == "done"
    assert [c["client_group_id"] for c in await _cards(mock_db)] == ["g3"]
    assert {r["client_group_id"] for r in run["summary"]["could_not_check"]} == {"g1", "g2"}
    assert any(a["client_group_id"] == "g1" and a["code"] == "check_failed" for a in run["alerts"])


async def test_expired_token_records_reconnect_and_no_writes(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2")
    fakes.refresh_error["g1"] = MetaTokenError("expired")
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["phase"] == "done" and run["reconnect"] is True and run["summary"]["reconnect"] is True
    assert fakes.ai_calls == [] and fakes.autos == [] and await _cards(mock_db) == []
    assert all(c["state"] == "failed" and "Reconnect" in c["error"] for c in run["clients"].values())
    assert len(fakes.published) == 1  # the report and Slack say to reconnect
    assert fakes.refreshed == ["g1"]  # no point reading on with a dead token


async def test_missing_token_records_reconnect(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    fakes.token = None
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["reconnect"] is True and fakes.refreshed == [] and fakes.autos == []


async def test_state_is_refreshed_before_alerts_and_read_once_per_shared_account(mock_db, mock_mongo_client, fakes):
    clients = {**await _client(mock_db, "g1"), **await _client(mock_db, "g2")}
    await mock_db.client_groups.update_many({}, {"$set": {"meta_ad_account_id": "act_shared"}})
    agent = await _agent(mock_db, clients)
    await _run_all(mock_db, mock_mongo_client, agent)
    assert fakes.order[:3] == [("refresh", "g1"), ("refresh", "g2"), ("health", "act_shared")]
    assert fakes.health_reads == ["act_shared"]


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


async def test_late_data_finishes_with_a_reason_and_no_card(mock_db, mock_mongo_client, fakes):
    # Synced before midnight, so 2 Nov is incomplete. A false stopped or a thin CPL must not reach a card.
    agent = await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20), days=False)
    await mock_db[AD_DAYS].insert_one({
        "user_id": USER, "client_group_id": "g1", "ad_id": "ad_g1", "campaign_id": "c1", "ad_name": "x",
        "date_start": "2026-10-29", "insight_data": {"spend": "500", "actions": []}})
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "data_late" and "2026-11-02" in run["clients"]["g1"]["reason"]
    assert fakes.ai_calls == [] and await _cards(mock_db) == []
    assert any(a["code"] == "spend_data_late" for a in run["alerts"])


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
    assert run["clients"]["g1"]["state"] == "on_hold" and "g1" not in fakes.refreshed
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


async def test_client_held_by_another_agent_gets_no_card(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1", "g2", claim=False)
    await claims.claim_clients(mock_db, USER, agent["_id"], ["g2"])
    await claims.claim_clients(mock_db, USER, "other-agent", ["g1"])
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "held_elsewhere"
    assert [c["gid"] for c in fakes.ai_calls] == ["g2"]
    assert [c["client_group_id"] for c in await _cards(mock_db)] == ["g2"]
    assert "act_g1" in fakes.health_reads  # it can still be reported on


async def test_client_moved_after_checks_makes_no_ai_call(mock_db, mock_mongo_client, fakes, monkeypatch):
    agent = await _setup(mock_db, "g1")
    monkeypatch.setattr(runner, "_out_of_time", lambda d: False)
    real = runner._checks_phase

    async def then_moved(db, mongo_client, run, agent, deadline):
        done = await real(db, mongo_client, run, agent, deadline)
        await claims.claim_clients(db, USER, "other-agent", ["g1"], move=True)
        return done

    monkeypatch.setattr(runner, "_checks_phase", then_moved)
    run = await _run_all(mock_db, mock_mongo_client, agent)
    assert run["clients"]["g1"]["state"] == "held_elsewhere" and fakes.ai_calls == []


@pytest.mark.parametrize("scope_flag_only,other_holder", [(True, False), (True, True), (False, False)])
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


async def test_stale_run_marks_its_cards(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW, stale=True)
    assert run["stale"] is True
    await advance_run(mock_db, mock_mongo_client, run, agent, deadline=None)
    assert (await _cards(mock_db))[0]["stale"] is True


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


async def test_stale_data_waits_in_grace_then_runs_stale(mock_db, mock_mongo_client, fakes, monkeypatch):
    await _setup(mock_db, "g1")
    old = datetime(2026, 11, 2)
    await mock_db.client_groups.update_many({}, {"$set": {"last_meta_refresh": old}})
    queued = []

    async def enqueue(db, mongo_client, groups, now_utc):
        queued.append([g["id"] for g in groups])
        return len(groups)

    monkeypatch.setattr(agent_jobs, "_enqueue_refreshes", enqueue)
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert out["refreshed"] == 1 and out["ran"] == 0 and queued == [["g1"]]
    assert await mock_db[RUNS].count_documents({}) == 0
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + agent_jobs.GRACE)
    assert out["ran"] == 1 and (await mock_db[RUNS].find_one({}))["stale"] is True


async def test_on_hold_client_does_not_hold_up_the_run(mock_db, mock_mongo_client, fakes):
    clients = {**await _client(mock_db, "g1", on_hold=True), **await _client(mock_db, "g2")}
    await mock_db.client_groups.update_one({"id": "g1"}, {"$set": {"last_meta_refresh": datetime(2026, 11, 1)}})
    await _agent(mock_db, clients)
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert out["ran"] == 1


async def test_late_history_asks_for_a_top_up_and_waits_in_grace(mock_db, mock_mongo_client, fakes):
    await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert out["topups"] == 1 and out["ran"] == 0 and await mock_db[RUNS].count_documents({}) == 0
    doc = await mock_db[history.HISTORY].find_one({"_id": "g1"})
    assert doc["topup_requested_at"] is not None

    # The top-up lands: the next tick runs on complete days.
    await mock_db[history.HISTORY].update_one({"_id": "g1"}, {"$set": {"last_topup_at": datetime(2026, 11, 3, 7, 5),
                                                                       "topup_requested_at": None}})
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(minutes=15))
    assert out["ran"] == 1 and len(await _cards(mock_db)) == 1


async def test_late_history_past_grace_finishes_with_a_reason(mock_db, mock_mongo_client, fakes):
    await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20))
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + agent_jobs.GRACE)
    run = await mock_db[RUNS].find_one({})
    assert out["ran"] == 1 and run["clients"]["g1"]["state"] == "data_late" and await _cards(mock_db) == []


async def test_history_in_error_is_not_waited_for(mock_db, mock_mongo_client, fakes):
    await _setup(mock_db, "g1", synced=datetime(2026, 11, 2, 20), history_status="error")
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert out["ran"] == 1 and out["topups"] == 0


async def test_a_run_left_from_yesterday_ends_and_today_starts(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    mon = await get_or_create_run(mock_db, agent, NOW - timedelta(days=1))
    out = await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    old = await mock_db[RUNS].find_one({"_id": mon["_id"]})
    assert old["phase"] == "done" and old["summary"]["missing"] == ["g1"]
    assert out["ran"] == 1 and await mock_db[RUNS].count_documents({"local_date": "2026-11-03"}) == 1


async def test_windows_are_stable_across_ticks(mock_db, mock_mongo_client, fakes):
    agent = await _setup(mock_db, "g1")
    run = await get_or_create_run(mock_db, agent, NOW)
    assert runner._run_now(run) == NOW
