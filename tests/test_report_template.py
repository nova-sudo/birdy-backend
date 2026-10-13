"""
The Slack report template (services/report_template.py) and the copilot's
tools: what a template may contain, what each section says, and how a send
compares itself with the one before.
"""
from datetime import datetime, timezone

import pytest

from core.crypto import encrypt
import services.report_template as rt
import services.slack_brief as sb
from ai.tools import report_designer_tools as tools

USER = "agency@x.com"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def _group(gid, name, spend, leads, *, won=None, opps=None, revenue=None, targets=None,
           preset="yesterday", status="Active", calls=None):
    g = {"id": gid, "user_id": USER, "name": name, "client_status": status,
         "facebook_cache": {"presets": {preset: {"metrics": {"insights": {
             "spend": spend, "results": leads, "clicks": leads * 10, "impressions": leads * 500}}}}}}
    if won is not None:
        g["ghl_opp_cache"] = {preset: {"won": won, "total_opportunities": opps, "won_revenue": revenue or 0}}
    if targets:
        g["targets"] = targets
    if calls is not None:
        g["hotprospector_call_cache"] = {preset: {"total_calls": calls, "answered_calls": calls // 2}}
    return g


async def _seed(db, groups, brief=None, **slack_extra):
    slack = {"bot_token_encrypted": encrypt("xoxb-test"), "notify_channel_id": "C123", **slack_extra}
    if brief:
        slack["brief"] = brief
    await db["users"].insert_one({"user_id": USER, "default_currency": "GBP", "timezone": "Europe/London",
                                  "integrations": {"slack_bot": slack}})
    await db["client_groups"].insert_many(groups)


def T(*sections, frequency="daily", **extra):
    return rt.validate({"frequency": frequency, "time": "9:00 AM", "sections": list(sections), **extra})


# ── validation ────────────────────────────────────────────────────────────────


def test_validate_cleans_and_orders():
    t = rt.validate({
        "frequency": "weekly", "day": "tuesday", "time": "8:00 am", "title": "  Weekly pulse ",
        "sections": [
            {"type": "birdy_take", "focus": "cost"},
            {"type": "metric", "metric": "spend"},
            {"type": "ranking", "metric": "leads", "limit": 99},
        ],
    }, client_ids={"g1"})
    assert t["day"] == "Tuesday" and t["time"] == "8:00 AM" and t["title"] == "Weekly pulse"
    assert [s["type"] for s in t["sections"]] == ["metric", "ranking", "birdy_take"]   # take moves last
    assert t["sections"][0] == {"type": "metric", "metric": "spend", "scope": "portfolio", "compare": "none"}
    assert t["sections"][1]["limit"] == rt.MAX_RANKING and t["sections"][1]["direction"] == "best"


def test_validate_rejects_what_the_renderer_cannot_draw():
    with pytest.raises(rt.TemplateError) as e:
        rt.validate({"frequency": "hourly", "sections": [
            {"type": "metric", "metric": "vibes"},
            {"type": "metric", "metric": "spend", "scope": {"client_id": "someone-elses"}},
            {"type": "birdy_take"}, {"type": "birdy_take"},
            {"type": "chart"},
        ]}, client_ids={"g1"})
    joined = " | ".join(e.value.errors)
    assert "frequency" in joined
    assert "unknown metric 'vibes'" in joined
    assert "not one of this account's clients" in joined
    assert "only one birdy_take" in joined
    assert "type must be one of" in joined


def test_target_compare_dropped_where_no_target_exists():
    t = T({"type": "metric", "metric": "ctr", "compare": "target"})
    assert t["sections"][0]["compare"] == "none"


def test_too_many_sections():
    with pytest.raises(rt.TemplateError):
        T(*[{"type": "alerts"}] * (rt.MAX_SECTIONS + 1))


def test_legacy_brief_converts():
    t = rt.normalize({"frequency": "daily", "time": "7:00 AM", "items": {"spend": True, "top": True}})
    assert t["version"] == 2 and t["time"] == "7:00 AM"
    assert [s["type"] for s in t["sections"]] == ["metric", "ranking"]


# ── figures ───────────────────────────────────────────────────────────────────


async def test_client_scope_target_and_ranking(mock_db):
    await _seed(mock_db, [
        _group("g1", "Glow Clinic", 120, 12, won=2, opps=10, targets={"cpl": 8, "monthly_wins": 30}),
        _group("g2", "Bright Smiles", 60, 3, won=1, opps=5),
        _group("g3", "Quiet Co", 0, 0),
    ])
    t = T(
        {"type": "metric", "metric": "cpl", "scope": {"client_id": "g1"}, "compare": "target"},
        {"type": "metric", "metric": "ghl_won_opps", "scope": {"client_id": "g1"}, "compare": "target"},
        {"type": "ranking", "metric": "cpl", "direction": "best", "limit": 3},
        {"type": "ranking", "metric": "cpl", "direction": "worst", "limit": 1},
    )
    data = await rt.gather(mock_db, USER, t, utc(2026, 9, 30, 8))
    _, text = rt.render(t, data)

    assert "*Cost per lead · Glow Clinic:* £10.00 · ⚠️ off target (£8.00)" in text
    # 30 wins a month is 1 a day; 2 won yesterday beats it.
    assert "*Deals won · Glow Clinic:* 2 · ✅ on target (1)" in text
    # Cheapest leads first; the client with no leads is not ranked at all.
    assert "1. Glow Clinic · £10.00\n   2. Bright Smiles · £20.00" in text
    assert "Quiet Co" not in text
    assert "*🔻 Bottom by cost per lead:* Bright Smiles (£20.00)" in text


async def test_call_metrics_need_the_window_not_lifetime(mock_db):
    g = _group("g1", "Glow Clinic", 120, 12)
    g["hotprospector_call_cache"] = {"maximum": {"total_calls": 5000}}
    await _seed(mock_db, [g])
    t = T({"type": "metric", "metric": "hp_total_calls"})
    data = await rt.gather(mock_db, USER, t, utc(2026, 9, 30, 8))
    assert "no data for this period" in rt.render(t, data)[1]


async def test_archived_client_line_is_dropped(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 120, 12, status="Inactive"), _group("g2", "B", 1, 1)])
    t = T({"type": "metric", "metric": "spend", "scope": {"client_id": "g1"}}, {"type": "alerts"})
    data = await rt.gather(mock_db, USER, t, utc(2026, 9, 30, 8))
    text = rt.render(t, data)[1]
    assert "Glow Clinic" not in text and "Alerts" in text


# ── previous-period comparison ────────────────────────────────────────────────


def test_previous_only_when_it_is_the_period_just_before():
    snap = {"period": "2026-09-29", "frequency": "daily", "values": {"spend:portfolio": 100}}
    assert rt.previous_for(snap, "2026-09-30", "daily") is snap
    assert rt.previous_for(snap, "2026-10-01", "daily") is None      # a day was missed
    assert rt.previous_for(snap, "2026-10-06", "weekly") is None     # cadence changed
    weekly = {**snap, "frequency": "weekly"}
    assert rt.previous_for(weekly, "2026-10-06", "weekly") is weekly


async def test_second_send_shows_the_change(mock_db, monkeypatch):
    posts = []

    async def fake_post(self, **kw):
        posts.append(kw)
        return {"ok": True}

    from slack_sdk.web.async_client import AsyncWebClient
    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)

    brief = T({"type": "metric", "metric": "spend", "compare": "previous"},
              {"type": "metric", "metric": "cpl", "compare": "previous"})
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)], brief=brief)
    await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 8))
    assert "vs the day before" not in posts[0]["text"]                # nothing to compare with yet

    await mock_db["client_groups"].update_one({"id": "g1"}, {"$set": {
        "facebook_cache.presets.yesterday.metrics.insights": {"spend": 150, "results": 10}}})
    await sb.send_due_briefs(mock_db, utc(2026, 10, 1, 8))
    assert "*Ad spend:* £150 · ▲ 50% vs the day before" in posts[1]["text"]
    assert "*Cost per lead:* £15.00 · ▲ 50% vs the day before ⚠️" in posts[1]["text"]


# ── Birdy's take ──────────────────────────────────────────────────────────────


async def test_take_is_added_and_its_failure_never_blocks_the_brief(mock_db, monkeypatch):
    posts = []

    async def fake_post(self, **kw):
        posts.append(kw)
        return {"ok": True}

    from slack_sdk.web.async_client import AsyncWebClient
    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)

    seen = {}

    async def fake_take(db, user_id, template, lines):
        seen["lines"] = lines
        return "Spend is steady; push budget to Glow Clinic."

    monkeypatch.setattr(rt, "write_take", fake_take)
    brief = T({"type": "metric", "metric": "spend"}, {"type": "birdy_take"})
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)], brief=brief)
    await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 8))
    assert seen["lines"] == ["💰 *Ad spend:* £100"]
    assert "Birdy's take: Spend is steady" in posts[0]["text"]

    async def broken_take(*a, **k):
        return None

    monkeypatch.setattr(rt, "write_take", broken_take)
    await sb.send_due_briefs(mock_db, utc(2026, 10, 1, 8))
    assert len(posts) == 2 and "Birdy's take" not in posts[1]["text"]


# ── the copilot's tools ───────────────────────────────────────────────────────


async def test_propose_report_saves_a_valid_draft_and_returns_errors_otherwise(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)])
    bad = await tools.propose_report(mock_db, USER, template={"frequency": "daily", "sections": [
        {"type": "metric", "metric": "spend", "scope": {"client_id": "nope"}}]})
    assert bad["ok"] is False and await mock_db["report_drafts"].count_documents({}) == 0

    good = await tools.propose_report(mock_db, USER, template={"frequency": "weekly", "day": "Friday",
        "time": "5:00 PM", "sections": [{"type": "metric", "metric": "spend", "scope": {"client_id": "g1"}}]})
    assert good["ok"] is True
    stored = await mock_db["report_drafts"].find_one({"user_id": USER})
    assert stored["template"]["day"] == "Friday"


async def test_metric_catalog_marks_unconnected_sources(mock_db):
    await _seed(mock_db, [{**_group("g1", "Glow Clinic", 100, 10), "meta_ad_account_id": "act_1"}])
    rows = {m["id"]: m for m in (await tools.list_report_metrics(mock_db, USER))["metrics"]}
    assert rows["spend"]["available"] is True
    assert rows["hp_total_calls"]["available"] is False


async def test_nothing_linked_yet_offers_every_metric(mock_db):
    await _seed(mock_db, [{"id": "g1", "user_id": USER, "name": "New", "client_status": "Active"}])
    rows = (await tools.list_report_metrics(mock_db, USER))["metrics"]
    assert all("available" not in r for r in rows)


async def test_preview_uses_sample_figures_before_anything_has_synced(mock_db):
    from routers.report_designer import preview_payload
    await _seed(mock_db, [{"id": "g1", "user_id": USER, "name": "Glow Clinic", "client_status": "Active"}])
    out = await preview_payload(mock_db, USER, T({"type": "metric", "metric": "spend"}, {"type": "birdy_take"}))
    assert out["sample"] is True
    assert "£142" in out["text"]
    assert any("Birdy's take" in (b.get("text") or {}).get("text", "") for b in out["blocks"])


# ── the brief endpoint ────────────────────────────────────────────────────────


async def test_schedule_change_keeps_the_designed_sections(mock_db):
    from routers.onboarding import BriefConfigRequest, set_slack_brief
    designed = T({"type": "metric", "metric": "ghl_revenue"}, {"type": "birdy_take"}, title="Money")
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)], brief=designed)

    out = await set_slack_brief(BriefConfigRequest(frequency="weekly", time="8:00 AM", day="Friday"),
                                current_user=USER)
    assert out["brief"]["frequency"] == "weekly" and out["brief"]["day"] == "Friday"
    assert out["brief"]["title"] == "Money"
    assert [s["type"] for s in out["brief"]["sections"]] == ["metric", "birdy_take"]

    full = await set_slack_brief(BriefConfigRequest(template={"frequency": "daily", "time": "7:00 AM",
        "sections": [{"type": "alerts"}]}), current_user=USER)
    assert full["brief"]["sections"] == [{"type": "alerts"}]


async def test_slack_status_carries_the_report_for_settings(mock_db):
    from services.slack_bot_service import get_slack_bot_status
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)],
                brief={"frequency": "weekly", "day": "Friday", "time": "8:00 AM", "items": {"spend": True}},
                team_id="T1", bot_user_id="U1")
    status = await get_slack_bot_status(mock_db, USER)
    assert status["brief"]["frequency"] == "weekly" and status["brief"]["day"] == "Friday"
    assert status["brief"]["version"] == 2
