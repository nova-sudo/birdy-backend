"""
The scheduled Slack brief (services/slack_brief.py): when it is due, what it
says, and that each period is sent exactly once.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.crypto import encrypt
import services.slack_brief as sb
import services.report_template as rt

ALL_ITEMS = {k: True for k in sb.ITEM_KEYS}
DAILY_9AM = {"frequency": "daily", "time": "9:00 AM", "day": None, "items": ALL_ITEMS}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ── when a brief is due ───────────────────────────────────────────────────────

# 2026-09-30 is a Wednesday in British Summer Time: 09:00 London is 08:00 UTC.

def test_due_at_the_chosen_local_time_not_utc():
    assert sb.due_period(DAILY_9AM, "Europe/London", None, utc(2026, 9, 30, 7, 59)) is None
    assert sb.due_period(DAILY_9AM, "Europe/London", None, utc(2026, 9, 30, 8, 0)) == "2026-09-30"


def test_winter_time_moves_the_utc_hour():
    # 2026-12-02: GMT, so 09:00 London is 09:00 UTC.
    assert sb.due_period(DAILY_9AM, "Europe/London", None, utc(2026, 12, 2, 8, 30)) is None
    assert sb.due_period(DAILY_9AM, "Europe/London", None, utc(2026, 12, 2, 9, 0)) == "2026-12-02"


def test_other_timezones():
    # 9:00 AM New York (EDT, UTC-4) is 13:00 UTC.
    assert sb.due_period(DAILY_9AM, "America/New_York", None, utc(2026, 9, 30, 12, 0)) is None
    assert sb.due_period(DAILY_9AM, "America/New_York", None, utc(2026, 9, 30, 13, 0)) == "2026-09-30"


def test_missing_or_unknown_timezone_defaults_to_london():
    now = utc(2026, 9, 30, 8, 0)
    assert sb.due_period(DAILY_9AM, "", None, now) == "2026-09-30"
    assert sb.due_period(DAILY_9AM, "Not/AZone", None, now) == "2026-09-30"


def test_not_sent_twice_for_the_same_day():
    assert sb.due_period(DAILY_9AM, "Europe/London", "2026-09-30", utc(2026, 9, 30, 9, 0)) is None
    assert sb.due_period(DAILY_9AM, "Europe/London", "2026-09-29", utc(2026, 9, 30, 9, 0)) == "2026-09-30"


def test_a_missed_tick_catches_up_but_not_hours_later():
    assert sb.due_period(DAILY_9AM, "Europe/London", None, utc(2026, 9, 30, 10, 59)) == "2026-09-30"
    assert sb.due_period(DAILY_9AM, "Europe/London", None, utc(2026, 9, 30, 11, 0)) is None


def test_weekly_only_on_its_day():
    weekly = {**DAILY_9AM, "frequency": "weekly", "day": "Monday"}
    assert sb.due_period(weekly, "Europe/London", None, utc(2026, 9, 30, 8, 0)) is None   # Wednesday
    assert sb.due_period(weekly, "Europe/London", None, utc(2026, 10, 5, 8, 0)) == "2026-10-05"  # Monday


def test_afternoon_time_parses():
    five_pm = {**DAILY_9AM, "time": "5:00 PM"}
    assert sb.due_period(five_pm, "Europe/London", None, utc(2026, 9, 30, 15, 59)) is None
    assert sb.due_period(five_pm, "Europe/London", None, utc(2026, 9, 30, 16, 0)) == "2026-09-30"


def test_nothing_due_with_every_section_off_or_no_frequency():
    now = utc(2026, 9, 30, 8, 0)
    assert sb.due_period({**DAILY_9AM, "items": {k: False for k in sb.ITEM_KEYS}}, "Europe/London", None, now) is None
    assert sb.due_period({**DAILY_9AM, "frequency": None}, "Europe/London", None, now) is None


# ── what it says ──────────────────────────────────────────────────────────────


def _group(gid, name, spend, leads, won=None, opps=None, preset="yesterday", status="Active"):
    g = {
        "id": gid, "user_id": "agency@x.com", "name": name, "client_status": status,
        "facebook_cache": {preset: {"metrics": {"insights": {"spend": spend, "results": leads}}}},
    }
    if won is not None:
        g["ghl_opp_cache"] = {
            preset: {"won": won, "total_opportunities": opps},
            "maximum": {"won": 999, "total_opportunities": 9999},
        }
    return g


async def _seed(db, *, slack=True, brief=DAILY_9AM, groups=None, tz="Europe/London"):
    user = {"user_id": "agency@x.com", "default_currency": "GBP", "timezone": tz,
            "integrations": {"slack_bot": {"brief": brief}}}
    if slack:
        user["integrations"]["slack_bot"].update(
            bot_token_encrypted=encrypt("xoxb-test"), notify_channel_id="C123")
    await db["users"].insert_one(user)
    groups = groups if groups is not None else [
        _group("g1", "Glow Clinic", 120, 12, won=2, opps=10),
        _group("g2", "Bright Smiles", 60, 3, won=1, opps=5),
        _group("g3", "Old Client", 500, 50, status="Inactive"),
    ]
    if groups:
        await db["client_groups"].insert_many(groups)


async def _render(db, brief, now):
    template = rt.normalize(brief)
    ctx = await rt.build_context(db, "agency@x.com", template, now)
    return ctx, rt.render(template, ctx)[1]


async def test_legacy_brief_renders_the_chosen_sections_with_real_figures(mock_db):
    await _seed(mock_db)
    await mock_db["alert_notifications"].insert_many([
        {"user_id": "agency@x.com", "message": "[Glow Clinic] CPL above £10", "triggered_at": datetime(2026, 9, 29, 15)},
        {"user_id": "agency@x.com", "message": "too old", "triggered_at": datetime(2026, 9, 20)},
    ])
    await mock_db["ai_suggestions"].insert_one({
        "user_id": "agency@x.com", "status": "open", "action": {"type": "pause_ads"},
        "title": "Pause underperforming ad — Summer Promo", "client_name": "Glow Clinic"})

    ctx, text = await _render(mock_db, DAILY_9AM, utc(2026, 9, 30, 8, 0))

    assert len(ctx.groups) == 2                        # the inactive client is left out
    assert "Birdy brief · Yesterday" in text
    assert "*Ad spend:* £180" in text
    assert "*Leads:* 15" in text and "*Cost per lead:* £12.00" in text
    assert "*Close rate:* 20.0%" in text               # 3 won of 15 opportunities
    assert "Glow Clinic (12)" in text                   # top performer by leads
    assert "1 triggered · [Glow Clinic] CPL above £10" in text
    assert "1 flagged · Summer Promo" in text


async def test_never_reports_lifetime_closes_as_yesterdays(mock_db):
    """The metric reader falls back to 'maximum' when a preset is missing."""
    g = _group("g1", "Glow Clinic", 120, 12)
    g["ghl_opp_cache"] = {"maximum": {"won": 999, "total_opportunities": 9999}}
    await _seed(mock_db, groups=[g])

    _, text = await _render(mock_db, DAILY_9AM, utc(2026, 9, 30, 8, 0))

    assert "*Close rate:* no data" in text
    assert "999" not in text


async def test_empty_sections_say_so(mock_db):
    await _seed(mock_db, groups=[_group("g1", "Quiet", 0, 0)])
    brief = {"frequency": "weekly", "time": "9:00 AM", "items": {"top": True, "alerts": True, "underperform": True}}
    _, text = await _render(mock_db, brief, utc(2026, 9, 28, 8, 0))
    assert "Birdy brief · Last 7 days" in text
    assert "no client has data for this period" in text
    assert "none triggered" in text
    assert "none flagged" in text


# ── sending ───────────────────────────────────────────────────────────────────


@pytest.fixture
def slack(monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    posts = []

    async def fake_post(self, **kw):
        posts.append({"token": self.token, **kw})
        return {"ok": True, "ts": "1.1"}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    return posts


async def test_sends_once_per_period(mock_db, slack):
    await _seed(mock_db)
    now = utc(2026, 9, 30, 8, 0)

    first = await sb.send_due_briefs(mock_db, now)
    second = await sb.send_due_briefs(mock_db, now + timedelta(minutes=30))

    assert first["sent"] == 1 and second["sent"] == 0
    assert len(slack) == 1
    assert slack[0]["channel"] == "C123" and slack[0]["token"] == "xoxb-test"
    assert "Glow Clinic" in slack[0]["text"]
    user = await mock_db["users"].find_one({"user_id": "agency@x.com"})
    assert user["integrations"]["slack_bot"]["brief_last_sent"] == "2026-09-30"


async def test_sends_again_the_next_day(mock_db, slack):
    await _seed(mock_db)
    await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 8, 0))
    await sb.send_due_briefs(mock_db, utc(2026, 10, 1, 8, 0))
    assert len(slack) == 2


async def test_a_failed_post_is_retried_next_tick(mock_db, monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    calls = []

    async def flaky(self, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("ratelimited")
        return {"ok": True}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", flaky)
    await _seed(mock_db)

    first = await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 8, 0))
    user = await mock_db["users"].find_one({"user_id": "agency@x.com"})
    assert first["failed"] == 1
    assert user["integrations"]["slack_bot"].get("brief_last_sent") is None   # claim released

    second = await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 9, 0))
    assert second["sent"] == 1 and len(calls) == 2


async def test_no_channel_or_no_clients_is_skipped_not_retried(mock_db, slack):
    await _seed(mock_db, slack=False)
    out = await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 8, 0))
    assert out["skipped"] == 1 and slack == []
    again = await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 9, 0))
    assert again["due"] == 0


async def test_not_due_sends_nothing(mock_db, slack):
    await _seed(mock_db)
    out = await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 6, 0))
    assert out["due"] == 0 and slack == []


async def test_cron_endpoint_requires_the_secret_and_runs(mock_mongo_client, monkeypatch, slack):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.cron as cron

    monkeypatch.setenv("CRON_SECRET", "s3cret")
    app = FastAPI()
    app.include_router(cron.router)
    client = TestClient(app)

    assert client.get("/api/cron/slack-briefs").status_code in (401, 403)
    resp = client.get("/api/cron/slack-briefs", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    assert resp.json()["result"]["checked"] == 0
