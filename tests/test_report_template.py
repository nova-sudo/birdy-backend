"""
The Slack report as a free-form message with live placeholders
(services/report_template.py): what a message may contain, what each
placeholder says, Slack's rules, and the copilot's tools.
"""
from datetime import datetime, timezone

import pytest

from core.crypto import encrypt
import services.report_template as rt
import services.slack_brief as sb
from ai.tools import report_designer_tools as tools

USER = "agency@x.com"
NAMES = {"g1": "Glow Clinic", "g2": "Bright Smiles"}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def _group(gid, name, spend, leads, *, won=None, opps=None, revenue=None, targets=None,
           preset="yesterday", status="Active", calls=None, extra_presets=()):
    insights = {"spend": spend, "results": leads, "clicks": leads * 10, "impressions": leads * 500}
    g = {"id": gid, "user_id": USER, "name": name, "client_status": status,
         "facebook_cache": {"presets": {p: {"metrics": {"insights": insights}} for p in (preset, *extra_presets)}}}
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


def T(body, frequency="daily", **extra):
    return rt.validate({"frequency": frequency, "time": "9:00 AM", "body": body, **extra}, NAMES)


async def text_of(db, body, now=utc(2026, 9, 30, 8), frequency="daily", previous=None):
    template = T(body, frequency)
    ctx = await rt.build_context(db, USER, template, now, previous=previous)
    return rt.render(template, ctx)


# ── writing freely, within Slack's rules ──────────────────────────────────────


def test_markdown_habits_become_slack_mrkdwn():
    t = T("## Weekly **pulse**\n- spend {{spend}}\n[docs](https://birdy.ai)\n---\nbye")
    assert t["body"] == "## Weekly *pulse*\n• spend {{spend}}\n<https://birdy.ai|docs>\n---\nbye"
    blocks = rt.to_blocks(t["body"])
    assert [b["type"] for b in blocks] == ["header", "section", "divider", "section"]
    assert blocks[0]["text"] == {"type": "plain_text", "text": "Weekly pulse", "emoji": True}


def test_long_messages_are_split_under_slacks_section_limit():
    blocks = rt.to_blocks("\n\n".join(["x" * 1500] * 5))
    assert all(len(b["text"]["text"]) <= rt.SECTION_LIMIT for b in blocks)
    assert "".join(b["text"]["text"] for b in blocks).count("x") == 7500


def test_too_many_blocks_is_refused():
    with pytest.raises(rt.TemplateError) as e:
        T("\n".join(["a", "---"] * 30))
    assert "Slack allows 50" in e.value.errors[0]


def test_bad_placeholders_are_named():
    with pytest.raises(rt.TemplateError) as e:
        T("{{vibes}} {{ctr show=target}} {{cpl client=\"Nobody\"}} {{ranking}} {{spend period=forever}} "
          "{{birdy_take}}{{birdy_take}} {{breakdown metrics=\"spend,nope\"}} {{ spend show=change period=this_month }}")
    joined = " | ".join(e.value.errors)
    for expected in ("{{vibes}}: unknown placeholder", "has no stored target", "no client called 'Nobody'",
                     "ranking needs metric", "period must be one of", "only one {{birdy_take}}",
                     "unknown metric 'nope'", "can't take a period"):
        assert expected in joined, expected


def test_user_text_is_escaped_but_slack_links_survive():
    assert rt._escape("R&D <3 <https://x.com|site> <@U123>") == "R&amp;D &lt;3 <https://x.com|site> <@U123>"


def test_legacy_and_section_briefs_convert_to_messages():
    legacy = rt.normalize({"frequency": "daily", "time": "7:00 AM", "items": {"spend": True, "top": True}})
    assert legacy["version"] == 3 and "{{spend}}" in legacy["body"] and "{{ranking metric=leads" in legacy["body"]
    v2 = rt.normalize({"version": 2, "frequency": "weekly", "time": "8:00 AM", "title": "Pulse",
                       "style": {"emojis": False}, "sections": [
                           {"type": "metric", "metric": "cpl", "scope": {"client_id": "g1"}, "compare": "target"},
                           {"type": "divider"}, {"type": "alerts"}]})
    assert v2["body"].startswith("# Pulse · {{period}}")
    assert "{{cpl client=g1 show=vs_target}}" in v2["body"] and "---" in v2["body"]
    assert "🔔" not in v2["body"]
    rt.validate({**v2, "time": "8:00 AM"}, NAMES)  # what it converts to is itself valid


# ── what placeholders say ─────────────────────────────────────────────────────


async def test_values_clients_targets_and_free_wording(mock_db):
    await _seed(mock_db, [
        _group("g1", "Glow Clinic", 120, 12, won=2, opps=10, targets={"cpl": 8, "monthly_wins": 30}),
        _group("g2", "Bright Smiles", 60, 3, won=1, opps=5),
    ])
    _, text = await text_of(mock_db,
        "Morning! We spent {{spend}} for {{leads}} leads.\n"
        "Glow is paying {{cpl client=\"glow clinic\"}} a lead, {{cpl client=\"Glow Clinic\" show=vs_target}}.\n"
        "Wins vs pace: {{ghl_won_opps client=g1}} vs {{ghl_won_opps client=g1 show=target}}")
    assert "Morning! We spent £180 for 15 leads." in text
    assert "Glow is paying £10.00 a lead, off target (£8.00)." in text
    assert "Wins vs pace: 2 vs 1" in text            # 30 a month is 1 a day


async def test_rankings_and_breakdowns(mock_db):
    await _seed(mock_db, [
        _group("g1", "Glow Clinic", 120, 12), _group("g2", "Bright Smiles", 60, 3), _group("g3", "Quiet Co", 0, 0),
    ])
    _, text = await text_of(mock_db,
        "Cheapest leads:{{ranking metric=cpl order=best limit=3}}\n"
        "Needs help: {{ranking metric=cpl order=worst limit=1}}\n"
        "{{breakdown metrics=\"spend,leads\" sort=spend}}")
    assert "Cheapest leads:\n1. Glow Clinic · £10.00\n2. Bright Smiles · £20.00" in text
    assert "Needs help: Bright Smiles (£20.00)" in text
    assert "\n\n•" not in text                       # a list on its own line gets no blank line
    assert "• Glow Clinic · £120 ad spend · 12 leads\n• Bright Smiles · £60 ad spend · 3 leads" in text
    assert "Quiet Co" not in text


async def test_other_windows_and_lifetime_never_leaks(mock_db):
    g = _group("g1", "Glow Clinic", 120, 12, extra_presets=("this_month",))
    g["hotprospector_call_cache"] = {"maximum": {"total_calls": 5000}}
    await _seed(mock_db, [g])
    _, text = await text_of(mock_db, "Month: {{spend period=this_month}} · calls {{hp_total_calls}}")
    assert "Month: £120 · calls no data" in text


async def test_combined_metrics(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10, won=4, opps=8, revenue=500)])
    _, text = await text_of(mock_db, "{{cost_per_win}} per win, ROAS {{roas}}")
    assert "£25.00 per win, ROAS 5.00x" in text


async def test_alerts_flagged_and_specials(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)])
    await mock_db["alert_notifications"].insert_many([
        {"user_id": USER, "message": "A", "triggered_at": datetime(2026, 9, 29, 15)},
        {"user_id": USER, "message": "B", "triggered_at": datetime(2026, 9, 29, 16)}])
    _, text = await text_of(mock_db, "{{alerts show=count}} alerts:{{alerts show=list}}\n"
                                     "{{flagged_ads}} · {{period}} · {{client_count}} client")
    assert "2 alerts:\n• B\n• A" in text
    assert "none flagged · Yesterday · 1 client" in text


async def test_archived_client_reads_na(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 120, 12, status="Inactive"), _group("g2", "Bright Smiles", 1, 1)])
    _, text = await text_of(mock_db, "Glow: {{spend client=g1}}")
    assert "Glow: n/a" in text


# ── previous-period comparison ────────────────────────────────────────────────


def test_previous_only_when_it_is_the_period_just_before():
    snap = {"period": "2026-09-29", "frequency": "daily", "values": {}}
    assert rt.previous_for(snap, "2026-09-30", "daily") is snap
    assert rt.previous_for(snap, "2026-10-01", "daily") is None
    assert rt.previous_for(snap, "2026-10-06", "weekly") is None


@pytest.fixture
def slack(monkeypatch):
    posts = []

    async def fake_post(self, **kw):
        posts.append(kw)
        return {"ok": True}

    from slack_sdk.web.async_client import AsyncWebClient
    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    return posts


async def test_second_send_shows_the_change(mock_db, slack):
    brief = T("Spend {{spend}} {{spend show=change}}\nCPL {{cpl}} {{cpl show=change}}")
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)], brief=brief)
    await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 8))
    assert slack[0]["text"].startswith("Spend £100\nCPL £10.00")   # nothing to compare with yet

    await mock_db["client_groups"].update_one({"id": "g1"}, {"$set": {
        "facebook_cache.presets.yesterday.metrics.insights": {"spend": 150, "results": 10}}})
    await sb.send_due_briefs(mock_db, utc(2026, 10, 1, 8))
    assert "Spend £150 ▲ 50% vs the day before" in slack[1]["text"]
    assert "CPL £15.00 ▲ 50% vs the day before" in slack[1]["text"]


async def test_take_is_added_and_its_failure_never_blocks_the_brief(mock_db, slack, monkeypatch):
    seen = {}

    async def fake_take(db, user_id, template, report_text):
        seen["text"] = report_text
        return "Spend is steady; push budget to Glow Clinic."

    monkeypatch.setattr(rt, "write_take", fake_take)
    brief = T("Spend {{spend}}\n\nMy read: {{birdy_take}}")
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)], brief=brief)
    await sb.send_due_briefs(mock_db, utc(2026, 9, 30, 8))
    assert seen["text"] == "Spend £100\n\nMy read:"
    assert "My read: Spend is steady" in slack[0]["text"]

    async def broken_take(*a, **k):
        return None

    monkeypatch.setattr(rt, "write_take", broken_take)
    await sb.send_due_briefs(mock_db, utc(2026, 10, 1, 8))
    assert len(slack) == 2 and "Spend is steady" not in slack[1]["text"]


# ── the copilot's tools and the endpoints ─────────────────────────────────────


async def test_propose_report_saves_a_valid_draft_and_returns_errors_otherwise(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)])
    bad = await tools.propose_report(mock_db, USER, template={"frequency": "daily", "time": "9:00 AM",
                                                              "body": "{{spend client=\"Someone Else\"}}"})
    assert bad["ok"] is False and await mock_db["report_drafts"].count_documents({}) == 0

    good = await tools.propose_report(mock_db, USER, template={"frequency": "weekly", "day": "Friday",
        "time": "5:00 PM", "body": "No emojis here. Glow spent {{spend client=\"Glow Clinic\"}}."})
    assert good["ok"] is True
    stored = await mock_db["report_drafts"].find_one({"user_id": USER})
    assert stored["template"]["day"] == "Friday"


async def test_metric_catalog_and_guide(mock_db):
    await _seed(mock_db, [{**_group("g1", "Glow Clinic", 100, 10), "meta_ad_account_id": "act_1"}])
    out = await tools.list_report_metrics(mock_db, USER)
    rows = {m["id"]: m for m in out["metrics"]}
    assert rows["spend"]["available"] is True and rows["hp_total_calls"]["available"] is False
    assert rows["roas"]["available"] is False                  # needs the CRM too
    assert "{{ranking" in out["placeholders"]


async def test_nothing_linked_yet_offers_every_metric(mock_db):
    await _seed(mock_db, [{"id": "g1", "user_id": USER, "name": "New", "client_status": "Active"}])
    rows = (await tools.list_report_metrics(mock_db, USER))["metrics"]
    assert all("available" not in r for r in rows)
