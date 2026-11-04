"""
tests/test_stock_weekly.py
--------------------------
The stock agent's weekly report: built in code from the last full Monday to Sunday and posted to Slack.
Slack goes to FakeSlack; nothing reads Meta or calls an AI.
"""

import json
import time
from datetime import date, datetime, timedelta, timezone

import contextlib

import pytest
from fastapi.testclient import TestClient

from ai.agents.stock import runner
from ai.agents.stock import slack as stock_slack
from ai.agents.stock import weekly
from ai.agents.stock.settings import DEFAULT_SETTINGS
from ai.agents.stock.weekly import build_weekly, last_full_week, post_weekly
from ai.agents.store import AD_DAYS, AGENTS, RUNS
import routers.agent_stock as agent_stock
import routers.agents as agents_router
from dependencies import get_current_user
from main import app
from test_stock_apply import _agent, _run
from agent_history_helpers import seed_history
from test_stock_runner import SYNCED, USER, ZONES
from test_stock_slack import FakeSlack, installed, slack  # noqa: F401 - fixtures

UTC = timezone.utc
MONDAY = datetime(2026, 11, 2, 7, tzinfo=UTC)
# The week just ended is 26 Oct to 1 Nov; the one before is 19 to 25 Oct.
THIS, LAST = "2026-10-28", "2026-10-21"
SLACK_SETTINGS = {"channel_id": "C1", "channel_name": "ads", "also_tag_user_id": None, "templates": {}}


async def _client(db, gid, *, this=(120, 10), last=(120, 10), name=None, currency="GBP", extra=None, synced=None, **group):
    """A client with one ad, and spend and leads for each of the two weeks. None skips a week."""
    await db.client_groups.insert_one({"id": gid, "user_id": USER, "name": name or f"Client {gid}",
                                       "meta_ad_account_id": f"act_{gid}", "ad_account_currency": currency,
                                       "meta_campaign_ids": None, **group})
    await seed_history(db, gid, status="ready", last_topup_at=synced or SYNCED.replace(day=2, hour=3))
    for day, vals in ((THIS, this), (LAST, last), *(extra or {}).items()):
        if vals:
            await db[AD_DAYS].insert_one({
                "user_id": USER, "client_group_id": gid, "ad_id": f"ad_{gid}", "campaign_id": "c1", "ad_name": "x",
                "date_start": day, "insight_data": {"spend": str(vals[0]),
                                                    "actions": [{"action_type": "lead", "value": str(vals[1])}]}})


async def _weekly_agent(db, gids, *, weekly_settings=None, on_hold=(), slack_settings=SLACK_SETTINGS):
    clients = {g: {"batch_id": "b", "treatment_ids": [], **({"on_hold": True} if g in on_hold else {})} for g in gids}
    agent = await _agent(db, gids=gids, settings={
        "zones": ZONES, "clients": clients, "slack": slack_settings,
        "weekly": {**DEFAULT_SETTINGS["weekly"], "enabled": True, "weekday": 0, **(weekly_settings or {})}})
    return agent


def _all_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _names(entries) -> list[str]:
    return [e["name"] for e in entries]


# ---------------------------------------------------------------------------
# The week
# ---------------------------------------------------------------------------

def test_on_monday_morning_the_week_ended_yesterday():
    start, end, prev_start, prev_end = last_full_week(MONDAY, "UTC")
    assert (start, end) == (date(2026, 10, 26), date(2026, 11, 1))
    assert (prev_start, prev_end) == (date(2026, 10, 19), date(2026, 10, 25))


def test_on_sunday_the_week_is_not_over_yet():
    sunday = datetime(2026, 11, 1, 23, tzinfo=UTC)
    assert last_full_week(sunday, "UTC")[1] == date(2026, 10, 25)


def test_the_week_follows_the_timezone():
    # 23:30 Sunday UTC is already Monday in Auckland, so Sunday has ended there.
    now = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)
    assert last_full_week(now, "Pacific/Auckland")[1] == date(2026, 11, 1)
    assert last_full_week(now, "UTC")[1] == date(2026, 10, 25)


# ---------------------------------------------------------------------------
# Who goes where
# ---------------------------------------------------------------------------

async def test_only_red_is_under_decreased_even_when_it_improved(mock_db):
    await _client(mock_db, "red_better", this=(150, 10), last=(300, 10))      # CPL 30 -> 15: still red
    await _client(mock_db, "green_same", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["red_better", "green_same"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert _names(result["decreased"]) == ["Client red_better"]
    assert _names(result["improved"]) == ["Client green_same"]


async def test_a_green_account_with_fewer_leads_stays_under_improved(mock_db):
    await _client(mock_db, "g", this=(30, 5), last=(80, 10))                  # CPL 6 is green, leads halved
    agent = await _weekly_agent(mock_db, ["g"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert _names(result["improved"]) == ["Client g"]
    assert result["decreased"] == []


async def test_a_yellow_that_slipped_from_green_stays_under_improved(mock_db):
    await _client(mock_db, "y", this=(90, 10), last=(60, 10))                 # 9.0 yellow, was 6.0 green
    agent = await _weekly_agent(mock_db, ["y"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert result["decreased"] == []
    assert _names(result["improved"]) == ["Client y"]
    assert result["improved"][0]["zone"] == "yellow" and result["improved"][0]["prev_zone"] == "green"


async def test_on_hold_clients_are_left_out_everywhere(mock_db):
    await _client(mock_db, "held", this=(500, 10), last=(60, 10))
    await _client(mock_db, "live", this=(60, 10), last=(70, 10))
    agent = await _weekly_agent(mock_db, ["held", "live"], on_hold=("held",))
    result = await build_weekly(mock_db, agent, MONDAY)
    assert _names(result["improved"]) == ["Client live"]
    assert result["decreased"] == []
    assert "Client held" not in _all_text(result)


async def _last_weeks_weekly(db, agent, zones: dict, *, end="2026-10-25"):
    """The weekly stored on last week's run: {gid: zone} for the accounts it covered."""
    await db[RUNS].insert_one({
        "_id": "last-week", "agent_id": agent["_id"], "user_id": USER, "kind": "alert_only", "local_date": "2026-10-26",
        "as_of": datetime(2026, 10, 26, 7), "phase": "done", "clients": {}, "alerts": [],
        "weekly": {"week": {"start": "2026-10-19", "end": end}, "zones": {g: {"name": f"Client {g}", "zone": z} for g, z in zones.items()}}})


async def test_first_week_says_so_and_compares_nothing(mock_db):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert result["status_check"]["first_week"] is True
    assert "first week" in result["values"]["status_check"].lower()
    assert "→" not in result["values"]["status_check"]


async def test_status_check_reads_last_weeks_stored_set_and_names_changes(mock_db):
    await _client(mock_db, "old1", this=(60, 10), last=(60, 10))              # green, green
    await _client(mock_db, "old2", this=(150, 10), last=(60, 10))             # red now, was green
    await _client(mock_db, "stopped", this=None, last=(60, 10))               # no spend now
    await _client(mock_db, "new", this=(500, 10), last=(60, 10))              # not in last week's message
    agent = await _weekly_agent(mock_db, ["old1", "old2", "stopped", "new"])
    await _last_weeks_weekly(mock_db, agent, {"old1": "green", "old2": "green", "stopped": "green"})
    result = await build_weekly(mock_db, agent, MONDAY)
    check = result["status_check"]
    assert check["first_week"] is False and check["accounts"] == 3
    assert check["last_week"] == {"green": 3, "yellow": 0, "red": 0}
    assert check["this_week"] == {"green": 1, "yellow": 0, "red": 1}
    text = result["values"]["status_check"]
    assert "Client old2" in text and "Client stopped" in text and "Client old1" not in text and "Client new" not in text
    assert "not spending" in text.lower()
    assert "Client new" in _names(result["decreased"])      # still in the lists, not in the comparison


async def test_status_check_ignores_clients_added_unheld_and_names_those_now_held(mock_db):
    await _client(mock_db, "stay", this=(60, 10), last=(60, 10))
    await _client(mock_db, "added", this=(60, 10), last=(60, 10))             # joined this week
    await _client(mock_db, "unheld", this=(150, 10), last=(60, 10))           # was on hold last week
    await _client(mock_db, "held", this=(60, 10), last=(60, 10))              # put on hold this week
    agent = await _weekly_agent(mock_db, ["stay", "added", "unheld", "held"], on_hold=("held",))
    await _last_weeks_weekly(mock_db, agent, {"stay": "green", "held": "green"})
    result = await build_weekly(mock_db, agent, MONDAY)
    check = result["status_check"]
    assert check["accounts"] == 1 and check["this_week"]["green"] == 1
    text = result["values"]["status_check"]
    assert "Client held" in text and "Client unheld" not in text and "Client added" not in text


async def test_only_the_week_before_counts_as_last_week(mock_db):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    await _last_weeks_weekly(mock_db, agent, {"a": "green"}, end="2026-10-18")   # two weeks ago
    check = (await build_weekly(mock_db, agent, MONDAY))["status_check"]
    assert check["first_week"] is False and check["accounts"] == 0


async def test_the_stored_weekly_keeps_every_judged_account_zone(mock_db):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    await _client(mock_db, "b", this=None, last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a", "b"])
    zones = (await build_weekly(mock_db, agent, MONDAY))["zones"]
    assert zones["a"]["zone"] == "green" and zones["b"]["zone"] == "no_data"


async def test_clients_without_zones_or_ready_data_are_counted_not_judged(mock_db):
    await _client(mock_db, "ok", this=(60, 10), last=(60, 10))
    await _client(mock_db, "nozones", this=(60, 10), last=(60, 10), currency="EUR")   # default zones are GBP
    await _client(mock_db, "late", this=(60, 10), last=(60, 10))
    await mock_db["agent_ad_history"].update_one({"_id": "late"}, {"$set": {"last_topup_at": datetime(2026, 10, 30)}})
    agent = await _weekly_agent(mock_db, ["ok", "nozones", "late"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert _names(result["improved"]) == ["Client ok"]
    assert sorted(_names(result["not_judged"])) == ["Client late", "Client nozones"]
    assert "2 clients" in result["values"]["comparison"]


# ---------------------------------------------------------------------------
# Wins and the comparison
# ---------------------------------------------------------------------------

async def test_wins_are_the_three_biggest_cpl_drops_in_medal_order(mock_db):
    await _client(mock_db, "a", this=(50, 10), last=(100, 10))                # -50%
    await _client(mock_db, "b", this=(70, 10), last=(100, 10))                # -30%
    await _client(mock_db, "c", this=(80, 10), last=(100, 10))                # -20%
    await _client(mock_db, "d", this=(90, 10), last=(100, 10))                # -10%
    await _client(mock_db, "e", this=(110, 10), last=(100, 10))               # worse
    agent = await _weekly_agent(mock_db, list("abcde"))
    result = await build_weekly(mock_db, agent, MONDAY)
    assert _names(result["wins"]) == ["Client a", "Client b", "Client c"]
    blocks = result["values"]["wins"].split("\n\n")
    assert [b.split(" ")[0] for b in blocks] == ["🥇", "💪", "🚀"]
    assert all(len(b.split("\n")) == 3 for b in blocks)          # the name, then two short lines
    assert "50%" in blocks[0] and "£10.00" in blocks[0] and "£5.00" in blocks[0]


async def test_no_wins_says_so(mock_db):
    await _client(mock_db, "a", this=(110, 10), last=(100, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert result["wins"] == [] and result["values"]["wins"] == "No wins this week"


async def test_money_is_never_mixed_across_currencies(mock_db):
    await _client(mock_db, "gbp", this=(60, 10), last=(60, 10), currency="GBP")
    await _client(mock_db, "usd", this=(80, 10), last=(60, 10), currency="USD")
    clients = {g: {"batch_id": "b", "treatment_ids": []} for g in ("gbp", "usd")}
    agent = await _agent(mock_db, gids=("gbp", "usd"), settings={
        "zones": ZONES, "clients": {**clients, "usd": {"zones": {"currency": "USD", "green_under": 9, "red_over": 12}}},
        "weekly": {"enabled": True, "weekday": 0, "auto_post": False}})
    result = await build_weekly(mock_db, agent, MONDAY)
    text = result["values"]["comparison"]
    assert "GBP" in text and "USD" in text
    assert "£60.00" in text and "80.00 USD" in text


# ---------------------------------------------------------------------------
# What never appears
# ---------------------------------------------------------------------------

async def test_no_notes_follow_ups_or_meta_errors_in_any_string(mock_db, slack, installed):
    secret = "SECRET-INTERNAL-NOTE"
    await _client(mock_db, "a", this=(150, 10), last=(60, 10), notes=secret, ad_error=secret)
    agent = await _weekly_agent(mock_db, ["a"])
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {
        "settings.clients.a.notes": secret, "settings.clients.a.note": secret, "last_error": secret,
        "slack_template_notices": {"weekly": {"message": secret}}}})
    agent = await mock_db[AGENTS].find_one({"_id": agent["_id"]})
    await mock_db["meta_ad_state"].insert_one({"client_group_id": "a", "ad_id": "ad_a", "name": secret, "effective_status": "ACTIVE",
                                               "adset_id": "s1", "campaign_id": "c1", "error": secret})
    await mock_db["agent_cards"].insert_one({
        "_id": "c1", "agent_id": agent["_id"], "user_id": USER, "run_id": "w1", "client_group_id": "a", "status": "open",
        "client_name": secret, "thought_process": secret, "additional_changes": secret, "next_steps": secret, "press_note": secret,
        "follow_ups": [{"text": secret}], "lines": [{"id": "l1", "kind": "remove", "ad_name": secret, "error": secret,
                                                     "flags": [{"note": secret}]}]})
    run = _run(agent, run_id="w1", clients={"a": {"state": "failed", "error": "Meta error " + secret, "reason": secret}},
               alerts=[{"title": secret, "detail": secret}], summary={"could_not_check": [{"reason": secret}]},
               publish_error=secret, last_error=secret)
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    run = await mock_db[RUNS].find_one({"_id": "w1"})
    result = await build_weekly(mock_db, agent, MONDAY)
    await mock_db[RUNS].update_one({"_id": "w1"}, {"$set": {"weekly": result}})
    run = await mock_db[RUNS].find_one({"_id": "w1"})
    await post_weekly(mock_db, run, agent, by="sam")
    assert secret not in _all_text(result)
    assert secret not in _all_text(slack.posts)
    assert secret not in _all_text(weekly.weekly_view(await mock_db[RUNS].find_one({"_id": "w1"})))


async def test_names_are_escaped_in_the_posted_message(mock_db, slack, installed):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10), name="<!channel> & Co")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)
    await post_weekly(mock_db, run, agent, by="sam")
    sent = _all_text(slack.posts[0])
    assert "<!channel>" not in sent and "&lt;!channel&gt;" in sent


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def _stored_weekly_run(db, agent, **extra):
    run = _run(agent, run_id="w1", **extra)
    run["local_date"], run["as_of"] = "2026-11-02", MONDAY.replace(tzinfo=None)
    run["weekly"] = await build_weekly(db, agent, MONDAY)
    await db[RUNS].insert_one(run)
    return await db[RUNS].find_one({"_id": "w1"})


async def test_a_double_press_posts_once(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)
    first = await post_weekly(mock_db, run, agent, by="sam")
    second = await post_weekly(mock_db, run, agent, by="sam")
    assert first["outcome"] == "posted" and second["outcome"] == "already_posted"
    assert len(slack.posts) == 1
    stored = (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]
    assert stored["slack"]["ts"] == "100.1" and stored["posted_by"] == "sam"


async def test_a_press_while_another_is_posting_does_not_post(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)
    await mock_db[RUNS].update_one({"_id": "w1"}, {"$set": {"weekly.claimed_at": weekly._now()}})
    res = await post_weekly(mock_db, run, agent, by="sam")
    assert res["outcome"] == "in_progress" and slack.posts == []


async def test_a_dead_claim_is_taken_over(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)
    await mock_db[RUNS].update_one({"_id": "w1"}, {"$set": {"weekly.claimed_at": datetime(2026, 1, 1)}})
    assert (await post_weekly(mock_db, run, agent, by="sam"))["outcome"] == "posted"


async def test_a_retry_after_an_outage_posts_once(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)
    slack.fail["post"] = ["internal_error"]
    with pytest.raises(stock_slack.SlackPublishError):
        await post_weekly(mock_db, run, agent, by="sam")
    stored = (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]
    assert not stored.get("claimed_at") and not (stored.get("slack") or {}).get("ts")
    assert (await post_weekly(mock_db, run, agent, by="sam"))["outcome"] == "posted"
    assert (await post_weekly(mock_db, run, agent, by="sam"))["outcome"] == "already_posted"
    assert len(slack.posts) == 1


async def test_a_preview_never_posts(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent, kind="preview")
    assert (await post_weekly(mock_db, run, agent, by="sam"))["outcome"] == "preview"
    assert slack.posts == []


async def test_a_broken_saved_template_falls_back_and_leaves_a_notice(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"], slack_settings={
        **SLACK_SETTINGS, "templates": {"weekly": {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "{{gone}}"}}]}}})
    run = await _stored_weekly_run(mock_db, agent)
    await post_weekly(mock_db, run, agent, by="sam")
    assert slack.posts and "Weekly report" in _all_text(slack.posts[0])
    notices = (await mock_db[AGENTS].find_one({"_id": agent["_id"]}))[stock_slack.NOTICES]
    assert "weekly" in notices


# ---------------------------------------------------------------------------
# When it runs
# ---------------------------------------------------------------------------

def _weekly_posts(slack) -> list:
    return [p for p in slack.posts if "Weekly report" in _all_text(p)]


async def _end_of_run(db, agent, now):
    run = _run(agent, run_id="w1", kind="alert_only")
    run["local_date"] = now.date().isoformat()
    run["as_of"] = now.replace(tzinfo=None)
    await db[RUNS].insert_one(run)
    run = await db[RUNS].find_one({"_id": "w1"})
    cards: list = []
    await runner._weekly_step(db, run, agent, now)
    await stock_slack.publish_run(db, run, agent, cards)
    return await db[RUNS].find_one({"_id": "w1"})


async def test_weekly_is_built_on_its_weekday_even_on_an_alert_only_day(mock_db, slack, installed):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _end_of_run(mock_db, agent, MONDAY)
    assert run["kind"] == "alert_only" and run["weekly"]["improved"]
    assert _weekly_posts(slack) == []             # auto_post is off


async def test_weekly_is_not_built_on_other_days_or_when_off(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"], weekly_settings={"weekday": 3})
    assert not (await _end_of_run(mock_db, agent, MONDAY)).get("weekly")


async def test_weekly_is_not_built_when_disabled(mock_db, slack, installed):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"], weekly_settings={"enabled": False})
    run = await _end_of_run(mock_db, agent, MONDAY)
    assert not run.get("weekly")


async def test_auto_post_posts_at_the_end_of_the_run_once(mock_db, slack, installed):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"], weekly_settings={"auto_post": True})
    run = await _end_of_run(mock_db, agent, MONDAY)
    assert len(_weekly_posts(slack)) == 1 and run["weekly"]["slack"]["ts"]
    await stock_slack.publish_run(mock_db, run, agent, [])      # a later publish, or a retry
    assert len(_weekly_posts(slack)) == 1


async def test_auto_post_outage_fails_the_publish_so_it_is_retried_not_duplicated(mock_db, slack, installed):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"], weekly_settings={"auto_post": True})
    run = _run(agent, run_id="w1", kind="alert_only")
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    run = await mock_db[RUNS].find_one({"_id": "w1"})
    await runner._weekly_step(mock_db, run, agent, MONDAY)
    slack.fail["post"] = ["internal_error"]
    with pytest.raises(stock_slack.SlackPublishError):
        await stock_slack.publish_run(mock_db, run, agent, [])
    await stock_slack.publish_run(mock_db, run, agent, [])
    assert len(_weekly_posts(slack)) == 1


async def test_a_preview_run_builds_no_weekly(mock_db):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = _run(agent, run_id="w1", kind="preview")
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    await runner._weekly_step(mock_db, run, agent, MONDAY)
    assert not (await mock_db[RUNS].find_one({"_id": "w1"})).get("weekly")


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(agent_stock, "get_mongo_client", fake_client)
    state = {"user": USER}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def test_route_posts_once_and_a_second_press_says_already_posted(mock_db, slack, installed, api):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    await _stored_weekly_run(mock_db, agent)
    url = f"/api/agents/{agent['_id']}/runs/w1/weekly/post"
    one = api.post(url)
    two = api.post(url)
    assert one.status_code == 200 and one.json()["outcome"] == "posted"
    assert two.status_code == 200 and two.json()["outcome"] == "already_posted"
    assert len(slack.posts) == 1


async def test_route_without_a_weekly_is_404_and_other_owners_are_refused(mock_db, slack, installed, api):
    agent = await _weekly_agent(mock_db, [])
    await mock_db[RUNS].insert_one(_run(agent, run_id="w1"))
    assert api.post(f"/api/agents/{agent['_id']}/runs/w1/weekly/post").status_code == 404
    assert api.post(f"/api/agents/{agent['_id']}/runs/nope/weekly/post").status_code == 404


async def test_route_reports_a_slack_failure(mock_db, slack, installed, api):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    await _stored_weekly_run(mock_db, agent)
    slack.fail["post"] = ["internal_error"]
    res = api.post(f"/api/agents/{agent['_id']}/runs/w1/weekly/post")
    assert res.status_code == 502


async def test_run_view_carries_the_weekly_without_the_channel(mock_db, installed, api):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    await _stored_weekly_run(mock_db, agent)
    body = api.get(f"/api/agents/{agent['_id']}/runs/w1").json()
    assert body["weekly"]["values"]["week"] and body["weekly"]["posted"] is False
    assert "slack" not in body["weekly"] and "weekly" not in body["run"]


async def test_the_runner_builds_and_auto_posts_the_weekly_when_it_publishes(mock_db, mock_mongo_client, slack, installed, monkeypatch):
    async def no_auto(*a, **k):
        return None

    monkeypatch.setattr(runner, "auto_apply_run", no_auto)
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"], weekly_settings={"auto_post": True})
    run = _run(agent, run_id="w1", kind="alert_only", phase="publishing", as_of=MONDAY.replace(tzinfo=None))
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    done = await runner.advance_run(mock_db, mock_mongo_client, run, agent, deadline=None, now_utc=MONDAY)
    stored = await mock_db[RUNS].find_one({"_id": "w1"})
    assert done["kind"] == "alert_only" and stored["weekly"]["slack"]["ts"]
    assert len(_weekly_posts(slack)) == 1


# ---------------------------------------------------------------------------
# Review round 1: the account lines, one agency week, retries, claims
# ---------------------------------------------------------------------------

def test_every_weekday_gets_the_last_full_monday_to_sunday():
    for day in range(2, 9):                      # Mon 2 Nov to Sun 8 Nov
        now = datetime(2026, 11, day, 12, tzinfo=UTC)
        start, end, prev_start, prev_end = last_full_week(now, "UTC")
        expected_end = date(2026, 11, 1) if day < 8 else date(2026, 11, 1)
        assert end == expected_end and start.weekday() == 0 and end.weekday() == 6
        assert (end - start).days == 6 and prev_end == start - timedelta(days=1)
    assert last_full_week(datetime(2026, 11, 9, 6, tzinfo=UTC), "UTC")[1] == date(2026, 11, 8)


def test_near_midnight_flips_at_the_agency_midnight():
    assert last_full_week(datetime(2026, 11, 1, 23, 59, 59, tzinfo=UTC), "UTC")[1] == date(2026, 10, 25)
    assert last_full_week(datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC), "UTC")[1] == date(2026, 11, 1)


def test_dst_changeover_uses_the_real_offset():
    # US clocks went back on Sun 1 Nov 2026 at 02:00: Los Angeles is UTC-7 before, UTC-8 after.
    la = "America/Los_Angeles"
    assert last_full_week(datetime(2026, 11, 2, 7, 59, tzinfo=UTC), la)[1] == date(2026, 10, 25)   # Sun 23:59 PST
    assert last_full_week(datetime(2026, 11, 2, 8, 0, tzinfo=UTC), la)[1] == date(2026, 11, 1)     # Mon 00:00 PST
    # London went back on 25 Oct: Monday 26 Oct 00:00 GMT is 00:00 UTC.
    assert last_full_week(datetime(2026, 10, 25, 23, 59, tzinfo=UTC), "Europe/London")[1] == date(2026, 10, 18)
    assert last_full_week(datetime(2026, 10, 26, 0, 0, tzinfo=UTC), "Europe/London")[1] == date(2026, 10, 25)


def test_no_timezone_uses_the_runners_default():
    from ai.agents.stock.windows import DEFAULT_TZ, agency_tz
    assert agency_tz({}) == DEFAULT_TZ == "Europe/London"
    assert agency_tz({"scope": {"schedule": {"tz": "Africa/Cairo"}}}) == "Africa/Cairo"
    agent = {"scope": {}}
    # 23:30 UTC on a Sunday in November is Sunday in London too, but 00:30 Monday in Cairo
    now = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)
    assert runner._local_date(agent, now) == date(2026, 11, 1)


CAIRO_MONDAY = datetime(2026, 11, 2, 5, tzinfo=UTC)      # 07:00 in Cairo (UTC+2): Sunday evening in Los Angeles


async def _set_tz(db, agent, tz):
    await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"scope.schedule.tz": tz}})
    return await db[AGENTS].find_one({"_id": agent["_id"]})


async def test_one_agency_week_for_every_client_with_a_note_for_a_day_not_over(mock_db):
    la_synced = datetime(2026, 11, 2, 3)          # 20:00 on Sunday in Los Angeles: the account's Sunday isn't over
    await _client(mock_db, "cairo", this=(60, 10), last=(60, 10), extra={"2026-11-01": (20, 2)}, ad_account_timezone="Africa/Cairo",
                  synced=datetime(2026, 11, 2, 3))
    await _client(mock_db, "la", this=(60, 10), last=(60, 10), extra={"2026-11-01": (20, 2)}, ad_account_timezone="America/Los_Angeles",
                  synced=la_synced, name="LA")
    agent = await _set_tz(mock_db, await _weekly_agent(mock_db, ["cairo", "la"]), "Africa/Cairo")
    result = await build_weekly(mock_db, agent, CAIRO_MONDAY)
    assert result["week"]["end"] == "2026-11-01" and result["values"]["week"] == "26 Oct to 1 Nov"
    by_name = {e["name"]: e for e in result["improved"] + result["decreased"]}
    # Both are judged on 26 Oct to 1 Nov: Sunday's row is in the LA account's numbers too.
    assert by_name["LA"]["leads"] == by_name["Client cairo"]["leads"] == 12
    assert by_name["LA"]["note"] and "last day" in by_name["LA"]["note"].lower()
    assert not by_name["Client cairo"].get("note")
    assert "LA" in result["values"]["improved"] and "last day" in result["values"]["improved"].lower()


async def test_a_late_account_whose_day_is_over_is_not_judged(mock_db):
    await _client(mock_db, "late", this=(60, 10), last=(60, 10), synced=datetime(2026, 10, 30))
    agent = await _weekly_agent(mock_db, ["late"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert _names(result["not_judged"]) == ["Client late"] and result["improved"] == []


async def test_account_lines_show_leads_cpl_and_ad_spend(mock_db):
    await _client(mock_db, "a", this=(60, 10), last=(70, 14))
    agent = await _weekly_agent(mock_db, ["a"])
    text = (await build_weekly(mock_db, agent, MONDAY))["values"]["improved"]
    assert "Client a" in text
    assert "Leads: 14 → 10" in text and "CPL: £5.00 → £6.00" in text and "Ad Spend: £70.00 → £60.00" in text


async def test_a_red_account_gets_a_code_written_reason(mock_db):
    await _client(mock_db, "worse", this=(150, 10), last=(100, 10))
    await _client(mock_db, "better", this=(150, 10), last=(300, 10))
    await _client(mock_db, "none", this=(150, 0), last=(100, 10))
    agent = await _weekly_agent(mock_db, ["worse", "better", "none"])
    result = await build_weekly(mock_db, agent, MONDAY)
    by = {e["name"]: e for e in result["decreased"]}
    assert "up 50%" in by["Client worse"]["reason"]
    assert "still" in by["Client better"]["reason"].lower() and "improved" in by["Client better"]["reason"].lower()
    assert "no leads" in by["Client none"]["reason"].lower()
    text = result["values"]["decreased"]
    assert by["Client worse"]["reason"] in text and "Leads: 10 → 0" in text and "CPL: £10.00 → no leads" in text


async def test_zero_leads_last_week_has_no_cpl_arrow_and_is_never_a_win(mock_db):
    await _client(mock_db, "z", this=(60, 10), last=(60, 0))
    agent = await _weekly_agent(mock_db, ["z"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert result["wins"] == []
    assert "CPL: no leads → £6.00" in result["values"]["improved"]


async def test_a_client_with_no_rows_at_all_is_quiet(mock_db):
    await _client(mock_db, "empty", this=None, last=None)
    agent = await _weekly_agent(mock_db, ["empty"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert result["improved"] == [] and result["decreased"] == [] and result["zones"]["empty"]["zone"] == "no_data"


async def test_a_missing_client_group_is_counted_not_judged(mock_db):
    await _client(mock_db, "here", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["here", "ghost"])
    result = await build_weekly(mock_db, agent, MONDAY)
    assert [n["client_group_id"] for n in result["not_judged"]] == ["ghost"]
    assert "1 client" in result["values"]["comparison"]


# ---- a failed build, and an unposted weekly rebuilt ----

async def _fail_build(monkeypatch, times=1):
    calls = {"n": 0}
    real = weekly.build_weekly

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= times:
            raise RuntimeError("boom")
        return await real(*a, **k)

    monkeypatch.setattr(runner, "build_weekly", flaky)
    monkeypatch.setattr(weekly, "build_weekly", flaky)
    return calls


async def test_a_failed_build_is_recorded_and_changes_nothing_else(mock_db, slack, installed, monkeypatch):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"], weekly_settings={"auto_post": True})
    await _fail_build(monkeypatch)
    run = _run(agent, run_id="w1", kind="alert_only", phase="publishing")
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    run = await mock_db[RUNS].find_one({"_id": "w1"})
    await runner._weekly_step(mock_db, run, agent, MONDAY)          # must not raise
    stored = await mock_db[RUNS].find_one({"_id": "w1"})
    assert not stored.get("weekly") and stored["weekly_pending"] is True and "boom" in stored["weekly_error"]
    assert stored["phase"] == "publishing" and stored["kind"] == "alert_only" and stored.get("publish_error") is None


async def test_later_ticks_retry_the_build_and_auto_post_once(mock_db, slack, installed, monkeypatch):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"], weekly_settings={"auto_post": True})
    await _fail_build(monkeypatch)
    run = _run(agent, run_id="w1", kind="alert_only", phase="done", as_of=MONDAY.replace(tzinfo=None))
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    run = await mock_db[RUNS].find_one({"_id": "w1"})
    await runner._weekly_step(mock_db, run, agent, MONDAY)
    assert await weekly.retry_weeklies(mock_db, MONDAY.replace(tzinfo=None), time.monotonic() + 30) == 0      # not yet due
    later = MONDAY + timedelta(minutes=30)
    assert await weekly.retry_weeklies(mock_db, later, time.monotonic() + 30) == 1
    stored = await mock_db[RUNS].find_one({"_id": "w1"})
    assert stored["weekly"]["improved"] and stored["weekly_pending"] is False and stored["weekly"]["slack"]["ts"]
    assert await weekly.retry_weeklies(mock_db, later + timedelta(hours=1), time.monotonic() + 30) == 0
    assert len(_weekly_posts(slack)) == 1


async def test_a_build_that_keeps_failing_is_given_up_after_a_day(mock_db, slack, installed, monkeypatch):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    await _fail_build(monkeypatch, times=99)
    run = _run(agent, run_id="w1", kind="alert_only", phase="done")
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    await runner._weekly_step(mock_db, await mock_db[RUNS].find_one({"_id": "w1"}), agent, MONDAY)
    await weekly.retry_weeklies(mock_db, MONDAY.replace(tzinfo=None) + timedelta(days=2), time.monotonic() + 30)
    stored = await mock_db[RUNS].find_one({"_id": "w1"})
    assert stored["weekly_pending"] is False and stored["weekly_error"]


async def test_auto_post_goes_out_with_late_clients_shown_as_not_judged(mock_db, slack, installed):
    await _client(mock_db, "ok", this=(60, 10), last=(60, 10))
    await _client(mock_db, "late", this=(60, 10), last=(60, 10), synced=datetime(2026, 10, 30))
    agent = await _weekly_agent(mock_db, ["ok", "late"], weekly_settings={"auto_post": True})
    run = await _end_of_run(mock_db, agent, MONDAY)
    sent = _all_text(_weekly_posts(slack))
    assert "Client ok" in sent and "Not included: 1 client" in sent
    assert run["weekly"]["not_judged"][0]["name"] == "Client late"


async def test_an_unposted_weekly_is_rebuilt_before_it_is_posted(mock_db, slack, installed):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10), name="Fresh")
    agent = await _weekly_agent(mock_db, ["a"])
    run = _run(agent, run_id="w1")
    run["local_date"], run["as_of"] = "2026-11-02", MONDAY.replace(tzinfo=None)
    run["weekly"] = {"week": {"end": "2026-11-01"}, "values": {k: "STALE" for k in ("week", "comparison", "improved", "decreased", "status_check", "wins")}}
    await mock_db[RUNS].insert_one(run)
    await post_weekly(mock_db, await mock_db[RUNS].find_one({"_id": "w1"}), agent, by="sam")
    sent = _all_text(slack.posts)
    assert "Fresh" in sent and "STALE" not in sent
    assert (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]["improved"][0]["name"] == "Fresh"


async def test_a_posted_weekly_is_never_rebuilt(mock_db, slack, installed):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)
    await post_weekly(mock_db, run, agent, by="sam")
    before = (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]
    await mock_db[AD_DAYS].delete_many({})
    await runner._weekly_step(mock_db, await mock_db[RUNS].find_one({"_id": "w1"}), agent, MONDAY)
    await post_weekly(mock_db, run, agent, by="sam")
    assert (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]["values"] == before["values"]


# ---- the claim ----

async def test_a_slow_posters_release_does_not_free_another_claim(mock_db):
    agent = await _weekly_agent(mock_db, [])
    run = _run(agent, run_id="w1")
    run["weekly"] = {"claimed_at": weekly._now(), "claim_token": "B"}
    await mock_db[RUNS].insert_one(run)
    await weekly._release(mock_db, "w1", "A", post_error="x")
    stored = (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]
    assert stored["claim_token"] == "B" and stored["claimed_at"] is not None
    await weekly._release(mock_db, "w1", "B")
    assert (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]["claimed_at"] is None


async def test_a_post_that_lost_its_claim_still_records_that_it_posted(mock_db, slack, installed, monkeypatch):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)
    real_post = slack.chat_postMessage

    async def taken_over(**kw):
        await mock_db[RUNS].update_one({"_id": "w1"}, {"$set": {"weekly.claim_token": "B"}})
        return await real_post(**kw)

    monkeypatch.setattr(slack, "chat_postMessage", taken_over)
    await post_weekly(mock_db, run, agent, by="sam")
    stored = (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]
    assert stored["slack"]["ts"] and stored["claim_token"] == "B"      # B's claim is B's to release
    assert (await post_weekly(mock_db, run, agent, by="sam"))["outcome"] == "already_posted"


async def test_slack_answering_without_a_ts_still_counts_as_posted(mock_db, slack, installed, monkeypatch):
    await _client(mock_db, "a")
    agent = await _weekly_agent(mock_db, ["a"])
    run = await _stored_weekly_run(mock_db, agent)

    async def no_ts(**kw):
        slack.posts.append(kw)
        return {"ok": True}

    monkeypatch.setattr(slack, "chat_postMessage", no_ts)
    assert (await post_weekly(mock_db, run, agent, by="sam"))["outcome"] == "posted"
    assert (await post_weekly(mock_db, run, agent, by="sam"))["outcome"] == "already_posted"
    assert len(slack.posts) == 1
    assert weekly.weekly_view(await mock_db[RUNS].find_one({"_id": "w1"}))["posted"] is True


async def test_not_built_is_a_404_from_the_route(mock_db, slack, installed, api, monkeypatch):
    agent = await _weekly_agent(mock_db, [])
    await _client(mock_db, "a")
    await _stored_weekly_run(mock_db, agent)

    async def vanished(*a, **k):
        return {"outcome": "not_built"}

    monkeypatch.setattr(agent_stock.stock_weekly, "post_weekly", vanished)
    assert api.post(f"/api/agents/{agent['_id']}/runs/w1/weekly/post").status_code == 404


async def test_a_skipped_week_says_last_weeks_report_is_not_available(mock_db):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    await _last_weeks_weekly(mock_db, agent, {"a": "green"}, end="2026-10-18")
    text = (await build_weekly(mock_db, agent, MONDAY))["values"]["status_check"]
    assert "isn't available" in text and "first weekly report" not in text and "→" not in text


async def test_earlier_weeklies_without_zones_also_count_as_not_the_first(mock_db):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    await mock_db[RUNS].insert_one({"_id": "old", "agent_id": agent["_id"], "user_id": USER, "kind": "batch",
                                    "as_of": datetime(2026, 10, 26, 7), "weekly": {"week": {"end": "2026-10-25"}}})
    result = await build_weekly(mock_db, agent, MONDAY)
    assert result["status_check"]["first_week"] is False and "isn't available" in result["values"]["status_check"]


async def test_a_preview_or_the_same_weeks_own_weekly_does_not_make_it_a_later_week(mock_db):
    await _client(mock_db, "a", this=(60, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    await mock_db[RUNS].insert_one({"_id": "p", "agent_id": agent["_id"], "user_id": USER, "kind": "preview",
                                    "as_of": datetime(2026, 10, 26, 7), "weekly": {"week": {"end": "2026-10-25"}}})
    await mock_db[RUNS].insert_one({"_id": "same", "agent_id": agent["_id"], "user_id": USER, "kind": "batch",
                                    "as_of": datetime(2026, 11, 2, 7), "weekly": {"week": {"end": "2026-11-01"}}})
    assert (await build_weekly(mock_db, agent, MONDAY))["status_check"]["first_week"] is True


async def test_a_rebuild_after_the_accounts_day_ended_uses_the_time_of_the_rebuild(mock_db):
    await _client(mock_db, "la", this=(60, 10), last=(60, 10), ad_account_timezone="America/Los_Angeles",
                  synced=datetime(2026, 11, 2, 3), name="LA")
    agent = await _set_tz(mock_db, await _weekly_agent(mock_db, ["la"]), "Africa/Cairo")
    first = await build_weekly(mock_db, agent, CAIRO_MONDAY)
    assert _names(first["improved"]) == ["LA"] and first["improved"][0]["note"]
    # Twelve hours on the week is the same, but the account's Sunday is over and still hasn't synced.
    later = await build_weekly(mock_db, agent, CAIRO_MONDAY, now=CAIRO_MONDAY + timedelta(hours=12))
    assert later["week"] == first["week"]
    assert later["improved"] == [] and _names(later["not_judged"]) == ["LA"]


async def test_the_runs_stored_start_fixes_the_week_but_not_the_clock_of_a_rebuild(mock_db, slack, installed):
    await _client(mock_db, "la", this=(60, 10), last=(60, 10), ad_account_timezone="America/Los_Angeles",
                  synced=datetime(2026, 11, 2, 3), name="LA")
    agent = await _set_tz(mock_db, await _weekly_agent(mock_db, ["la"]), "Africa/Cairo")
    run = _run(agent, run_id="w1")
    run["local_date"], run["as_of"] = "2026-11-02", CAIRO_MONDAY.replace(tzinfo=None)
    run["weekly"] = {"week": {"end": "2026-11-01"}, "values": {}}
    await mock_db[RUNS].insert_one(run)
    await post_weekly(mock_db, await mock_db[RUNS].find_one({"_id": "w1"}), agent, by="sam")     # real time: long after
    stored = (await mock_db[RUNS].find_one({"_id": "w1"}))["weekly"]
    assert stored["week"]["end"] == "2026-11-01" and stored["improved"] == [] and stored["not_judged"][0]["name"] == "LA"


async def test_the_retry_query_has_a_partial_index(mock_db, mock_mongo_client):
    from ai.agents.store import create_agent_indexes
    await create_agent_indexes(mock_mongo_client)
    infos = (await mock_db[RUNS].index_information()).values()
    match = [i for i in infos if [k for k, _ in i["key"]] == ["weekly_pending", "weekly_retry_at"]]
    assert match and match[0]["partialFilterExpression"] == {"weekly_pending": True}
