"""
tests/test_stock_weekly.py
--------------------------
The stock agent's weekly report: built in code from the last full Monday to Sunday and posted to Slack.
Slack goes to FakeSlack; nothing reads Meta or calls an AI.
"""

import json
from datetime import date, datetime, timezone

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


async def _client(db, gid, *, this=(120, 10), last=(120, 10), name=None, currency="GBP", **group):
    """A client with one ad, and spend and leads for each of the two weeks. None skips a week."""
    await db.client_groups.insert_one({"id": gid, "user_id": USER, "name": name or f"Client {gid}",
                                       "meta_ad_account_id": f"act_{gid}", "ad_account_currency": currency,
                                       "meta_campaign_ids": None, **group})
    await seed_history(db, gid, status="ready", last_topup_at=SYNCED.replace(day=2, hour=3))
    for day, vals in ((THIS, this), (LAST, last)):
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


async def test_status_check_reads_last_weeks_client_set(mock_db):
    await _client(mock_db, "old1", this=(60, 10), last=(60, 10))              # green, green
    await _client(mock_db, "old2", this=(150, 10), last=(60, 10))             # red now, green last week
    await _client(mock_db, "stopped", this=None, last=(60, 10))               # green last week, no spend now
    await _client(mock_db, "new", this=(500, 10), last=None)                  # not there last week
    agent = await _weekly_agent(mock_db, ["old1", "old2", "stopped", "new"])
    result = await build_weekly(mock_db, agent, MONDAY)
    check = result["status_check"]
    assert check["accounts"] == 3
    assert check["last_week"] == {"green": 3, "yellow": 0, "red": 0}
    assert check["this_week"] == {"green": 1, "yellow": 0, "red": 1}
    assert check["stopped"] == 1
    assert "3 accounts" in result["values"]["status_check"]
    # The new account still shows in the lists, as red.
    assert "Client new" in _names(result["decreased"])


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
    lines = result["values"]["wins"].split("\n")
    assert [l.split(" ")[0] for l in lines] == ["🥇", "💪", "🚀"]
    assert "50%" in lines[0] and "£10.00" in lines[0] and "£5.00" in lines[0]


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
    await _client(mock_db, "a", this=(150, 10), last=(60, 10))
    agent = await _weekly_agent(mock_db, ["a"])
    await mock_db["agent_cards"].insert_one({
        "_id": "c1", "agent_id": agent["_id"], "user_id": USER, "run_id": "r0", "client_group_id": "a", "status": "open",
        "thought_process": secret, "additional_changes": secret, "next_steps": secret, "press_note": secret,
        "follow_ups": [{"text": secret}], "lines": [{"id": "l1", "kind": "remove", "ad_name": secret, "error": secret,
                                                     "flags": [{"note": secret}]}]})
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"slack_template_notices": {"weekly": {"message": secret}}}})
    run = _run(agent, run_id="w1", clients={"a": {"state": "failed", "error": "Meta error " + secret}},
               alerts=[{"title": secret}], weekly=None)
    run["local_date"] = "2026-11-02"
    await mock_db[RUNS].insert_one(run)
    result = await build_weekly(mock_db, agent, MONDAY)
    await mock_db[RUNS].update_one({"_id": "w1"}, {"$set": {"weekly": result}})
    run = await mock_db[RUNS].find_one({"_id": "w1"})
    await post_weekly(mock_db, run, agent, by="sam")
    assert secret not in _all_text(result)
    assert secret not in _all_text(slack.posts)


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
    run["local_date"] = "2026-11-02"
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
