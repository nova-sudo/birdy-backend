"""
The Slack report as a Block Kit message (services/report_blocks.py): Slack's
rules per block, live data in charts / tables / carousels, placeholders in any
text, and the endpoints and tools built on it.
"""
from datetime import datetime, timezone

import pytest

from core.crypto import encrypt
import services.report_blocks as rb
import services.slack_brief as sb
from ai.tools import report_designer_tools as tools

USER = "agency@x.com"
NAMES = {"g1": "Glow Clinic", "g2": "Bright Smiles"}
NOW = datetime(2026, 9, 30, 8, tzinfo=timezone.utc)   # a Wednesday; "yesterday" is 29 Sep


def _group(gid, name, spend, leads, *, won=None, opps=None, status="Active", daily=None, targets=None):
    insights = {"spend": spend, "results": leads, "clicks": leads * 10, "impressions": leads * 500}
    g = {"id": gid, "user_id": USER, "name": name, "client_status": status,
         "facebook_cache": {"presets": {"yesterday": {"metrics": {"insights": insights}},
                                        "this_month": {"metrics": {"insights": {"spend": spend * 20, "results": leads * 20}}}}}}
    if won is not None:
        g["ghl_opp_cache"] = {"yesterday": {"won": won, "total_opportunities": opps}}
    if daily is not None:
        g["meta_daily_spend"] = [{"date": d, "spend": v} for d, v in daily.items()]
    if targets:
        g["targets"] = targets
    return g


async def _seed(db, groups, brief=None, **slack_extra):
    slack = {"bot_token_encrypted": encrypt("xoxb-test"), "notify_channel_id": "C123", **slack_extra}
    if brief:
        slack["brief"] = brief
    await db["users"].insert_one({"user_id": USER, "default_currency": "GBP", "timezone": "Europe/London",
                                  "integrations": {"slack_bot": slack}})
    await db["client_groups"].insert_many(groups)


def V(*blocks, frequency="daily"):
    return rb.validate({"frequency": frequency, "time": "9:00 AM", "blocks": list(blocks)}, NAMES)


async def rendered(db, *blocks, previous=None):
    t = V(*blocks)
    ctx = await rb.build_context(db, USER, t, NOW, previous=previous)
    return rb.render(t, ctx)


def errors_of(*blocks):
    with pytest.raises(rb.TemplateError) as e:
        V(*blocks)
    return " | ".join(e.value.errors)


# ── Slack's rules ─────────────────────────────────────────────────────────────


def test_every_message_block_type_is_accepted():
    t = V(
        {"type": "header", "text": "Weekly *pulse*"},
        {"type": "section", "text": "Hi **team**", "fields": ["*Spend*\n{{spend}}", "*Leads*\n{{leads}}"],
         "accessory": {"type": "button", "text": "Open Birdy", "url": "https://birdy.ai"}},
        {"type": "divider"},
        {"type": "image", "image_url": "https://birdy.ai/logo.png", "alt_text": "logo"},
        {"type": "context", "elements": ["small print", {"type": "image", "image_url": "https://birdy.ai/i.png", "alt_text": "i"}]},
        {"type": "actions", "elements": [{"type": "button", "text": "Dashboard", "url": "https://birdy.ai/d", "style": "primary"}]},
        {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "hi"}]}]},
        {"type": "markdown", "text": "**Standard** markdown | with | tables"},
        {"type": "card", "title": "Glow", "body": "Spend {{spend client=g1}}",
         "actions": [{"type": "button", "text": "Open", "url": "https://birdy.ai/c/g1"}]},
        {"type": "carousel", "birdy_data": {"metrics": ["spend", "leads"]}},
        {"type": "table", "birdy_data": {"metrics": ["spend"]}},
        {"type": "data_table", "caption": "Clients", "birdy_data": {"metrics": ["spend", "cpl"], "total_row": True}},
        {"type": "data_visualization", "title": "Spend", "birdy_data": {"chart": "bar", "metrics": ["spend"]}},
    )
    kinds = [b["type"] for b in t["blocks"]]
    assert kinds[:3] == ["header", "section", "divider"] and kinds[-1] == "data_visualization"
    assert t["blocks"][0]["text"] == {"type": "plain_text", "text": "Weekly pulse"}     # header is plain text
    assert t["blocks"][1]["text"]["text"] == "Hi *team*"                                 # Markdown → mrkdwn
    assert t["blocks"][1]["accessory"]["action_id"].startswith("link_")


def test_modal_only_and_unknown_blocks_are_refused():
    joined = errors_of({"type": "alert", "text": "x"}, {"type": "input"}, {"type": "plan"})
    assert "alert blocks only work in modals" in joined
    assert "input blocks only work in modals" in joined
    assert "type 'plan' isn't available" in joined


def test_buttons_must_be_links_and_limits_hold():
    assert "must open a link" in errors_of({"type": "actions", "elements": [{"type": "button", "text": "Go"}]})
    assert "Slack allows 2 data_visualization" in errors_of(
        *[{"type": "data_visualization", "title": f"c{i}", "birdy_data": {"chart": "bar", "metrics": ["spend"]}} for i in range(3)])
    assert "Slack allows 50" in errors_of(*[{"type": "divider"}] * 51)
    assert "a card takes at most 3 buttons" in errors_of(
        {"type": "card", "title": "x", "actions": [{"type": "button", "text": "b", "url": "https://a.io"}] * 4})


def test_data_bindings_are_checked():
    joined = errors_of(
        {"type": "data_visualization", "title": "a", "birdy_data": {"chart": "pie", "metrics": ["spend", "leads"]}},
        {"type": "data_visualization", "title": "b", "birdy_data": {"chart": "line", "by": "day", "metrics": ["cpl"]}},
        {"type": "data_table", "caption": "c", "birdy_data": {"metrics": ["vibes"]}},
        {"type": "carousel", "birdy_data": {"metrics": ["spend"], "clients": ["Nobody"]}},
    )
    assert "a pie chart shows exactly one metric" in joined
    assert "by=day can plot" in joined
    assert "unknown ['vibes']" in joined
    assert "no client called 'Nobody'" in joined


def test_placeholders_anywhere_are_checked():
    assert "unknown placeholder" in errors_of({"type": "card", "title": "{{vibes}}"})


def test_blocks_can_arrive_as_a_json_string_or_as_free_text():
    t = rb.validate({"frequency": "daily", "time": "9am", "blocks": '[{"type":"divider"},{"type":"header","text":"x"}]'})
    assert [b["type"] for b in t["blocks"]] == ["divider", "header"]
    t = rb.validate({"frequency": "daily", "time": "9am", "body": "# Hi\nSpend {{spend}}"})
    assert [b["type"] for b in t["blocks"]] == ["header", "section"]


def test_every_older_shape_converts():
    legacy = rb.normalize({"frequency": "daily", "time": "7:00 AM", "items": {"spend": True}})
    v3 = rb.normalize({"version": 3, "frequency": "weekly", "time": "8:00 AM", "body": "# Hi\n---\nx {{spend}}"})
    assert legacy["version"] == 4 and legacy["blocks"][0]["type"] == "header"
    assert [b["type"] for b in v3["blocks"]] == ["header", "divider", "section"]
    rb.validate(v3, NAMES)


# ── live data ─────────────────────────────────────────────────────────────────


async def test_placeholders_fill_in_fields_cards_and_context(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 120, 12, targets={"cpl": 8}), _group("g2", "Bright Smiles", 60, 3)])
    blocks, text = await rendered(mock_db,
        {"type": "header", "text": "Brief · {{period}}"},
        {"type": "section", "fields": ["*Spend*\n{{spend}}", "*CPL Glow*\n{{cpl client=\"Glow Clinic\" show=vs_target}}"]},
        {"type": "card", "title": "Glow & co", "body": "{{leads client=g1}} leads"},
        {"type": "context", "elements": ["{{client_count}} clients"]})
    assert blocks[0]["text"]["text"] == "Brief · Yesterday"
    assert [f["text"] for f in blocks[1]["fields"]] == ["*Spend*\n£180", "*CPL Glow*\noff target (£8.00)"]
    assert blocks[2]["title"]["text"] == "Glow &amp; co" and blocks[2]["body"]["text"] == "12 leads"
    assert "2 clients" in text


async def test_bar_chart_by_client_multi_series(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 120, 12), _group("g2", "Bright Smiles", 60, 3), _group("g3", "Quiet", 0, 0)])
    blocks, _ = await rendered(mock_db,
        {"type": "data_visualization", "title": "Spend and leads", "birdy_data": {"chart": "bar", "metrics": ["spend", "leads"]}})
    chart = blocks[0]["chart"]
    assert chart["type"] == "bar"
    assert chart["axis_config"]["categories"] == ["Glow Clinic", "Bright Smiles"]       # no-data client left out
    assert [s["name"] for s in chart["series"]] == ["Ad spend", "Leads"]
    assert chart["series"][0]["data"] == [{"label": "Glow Clinic", "value": 120}, {"label": "Bright Smiles", "value": 60}]
    assert "birdy_data" not in blocks[0]


async def test_line_chart_by_day_and_split_per_client(mock_db):
    days = {"2026-09-27": 10.0, "2026-09-28": -5.0, "2026-09-29": 30.0}   # a refund day: negative is fine
    await _seed(mock_db, [_group("g1", "Glow Clinic", 1, 1, daily=days), _group("g2", "Bright Smiles", 1, 1, daily={"2026-09-29": 7.0})])
    blocks, _ = await rendered(mock_db,
        {"type": "data_visualization", "title": "Daily spend", "birdy_data": {"chart": "line", "by": "day", "metrics": ["spend"], "days": 3}},
        {"type": "data_visualization", "title": "Per client", "birdy_data": {"chart": "area", "by": "day", "metrics": ["spend"], "days": 3, "split": "client"}})
    total, split = blocks[0]["chart"], blocks[1]["chart"]
    assert total["axis_config"]["categories"] == ["Sun 27", "Mon 28", "Tue 29"]
    assert [d["value"] for d in total["series"][0]["data"]] == [10.0, -5.0, 37.0]
    assert [s["name"] for s in split["series"]] == ["Glow Clinic", "Bright Smiles"]


async def test_pie_drops_non_positive_segments(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 120, 12), _group("g2", "Bright Smiles", 60, 3)])
    blocks, _ = await rendered(mock_db,
        {"type": "data_visualization", "title": "Share of spend", "birdy_data": {"chart": "pie", "metrics": ["spend"]}})
    assert blocks[0]["chart"] == {"type": "pie", "segments": [{"label": "Glow Clinic", "value": 120}, {"label": "Bright Smiles", "value": 60}]}


async def test_tables_and_carousel(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 120, 12), _group("g2", "Bright Smiles", 60, 3)])
    blocks, text = await rendered(mock_db,
        {"type": "data_table", "caption": "Clients", "birdy_data": {"metrics": ["spend", "cpl"], "sort": "cpl", "total_row": True}},
        {"type": "carousel", "birdy_data": {"metrics": ["spend", "leads"], "limit": 1}})
    rows = blocks[0]["rows"]
    assert [c["text"] for c in rows[0]] == ["Client", "Ad spend", "Cost per lead"]
    assert [c["text"] for c in rows[1]] == ["Glow Clinic", "£120", "£10.00"]          # cheapest lead first
    assert rows[1][1] == {"type": "raw_number", "value": 120, "text": "£120"}
    assert [c["text"] for c in rows[-1]] == ["Total", "£180", "£12.00"]
    cards = blocks[1]["elements"]
    assert len(cards) == 1 and cards[0]["title"]["text"] == "*Glow Clinic*"
    assert cards[0]["body"]["text"] == "Ad spend £120 · Leads 12"
    assert "Glow Clinic | £120 | £10.00" in text


async def test_a_data_block_with_nothing_to_show_says_so(mock_db):
    await _seed(mock_db, [_group("g1", "Quiet", 0, 0)])
    blocks, _ = await rendered(mock_db,
        {"type": "data_visualization", "title": "Spend", "birdy_data": {"chart": "bar", "metrics": ["spend"]}})
    assert blocks[0]["type"] == "context" and "no data for this period yet" in blocks[0]["elements"][0]["text"]


# ── sending ───────────────────────────────────────────────────────────────────


@pytest.fixture
def slack(monkeypatch):
    posts = []

    async def fake_post(self, **kw):
        posts.append(kw)
        return {"ok": True}

    from slack_sdk.web.async_client import AsyncWebClient
    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    return posts


async def test_send_posts_real_block_kit_with_take(mock_db, slack, monkeypatch):
    seen = {}

    async def fake_take(db, user_id, template, report_text):
        seen["text"] = report_text
        return "Glow carries the week."

    monkeypatch.setattr(rb, "write_take", fake_take)
    brief = V({"type": "header", "text": "Brief"},
              {"type": "data_visualization", "title": "Spend", "birdy_data": {"chart": "bar", "metrics": ["spend"]}},
              {"type": "section", "text": "*Birdy's take:* {{birdy_take}}"})
    await _seed(mock_db, [_group("g1", "Glow Clinic", 120, 12)], brief=brief)
    await sb.send_due_briefs(mock_db, NOW)
    post = slack[0]
    assert [b["type"] for b in post["blocks"]] == ["header", "data_visualization", "section"]
    assert post["blocks"][1]["chart"]["series"][0]["data"][0]["value"] == 120
    assert post["blocks"][2]["text"]["text"] == "*Birdy's take:* Glow carries the week."
    assert "Spend\nAd spend: Glow Clinic 120" in seen["text"]


# ── endpoints and tools ───────────────────────────────────────────────────────


async def test_propose_report_takes_blocks(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)])
    bad = await tools.propose_report(mock_db, USER, template={"frequency": "daily", "time": "9:00 AM",
                                                              "blocks": [{"type": "alert", "text": "x"}]})
    assert bad["ok"] is False and "modals" in bad["errors"][0]
    good = await tools.propose_report(mock_db, USER, template={"frequency": "weekly", "day": "Friday", "time": "5:00 PM",
        "blocks": [{"type": "data_visualization", "title": "Trend", "birdy_data": {"chart": "line", "by": "day", "metrics": ["spend"]}}]})
    assert good["ok"] is True
    stored = await mock_db["report_drafts"].find_one({"user_id": USER})
    assert stored["template"]["version"] == 4


async def test_guides_reach_the_copilot(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)])
    out = await tools.list_report_metrics(mock_db, USER)
    assert "data_visualization" in out["blocks"] and "{{ranking" in out["placeholders"]
    assert {m["id"] for m in out["daily_metrics"]} >= {"spend", "crm_leads", "calls"}


async def test_preview_uses_sample_figures_before_anything_has_synced(mock_db):
    from routers.report_designer import preview_payload
    await _seed(mock_db, [{"id": "g1", "user_id": USER, "name": "Glow Clinic", "client_status": "Active"}])
    out = await preview_payload(mock_db, USER, V(
        {"type": "section", "text": "Spend {{spend}} {{spend show=change}}"},
        {"type": "data_visualization", "title": "Daily", "birdy_data": {"chart": "line", "by": "day", "metrics": ["spend"]}},
        {"type": "context", "elements": ["{{birdy_take}}"]}))
    assert out["sample"] is True
    assert "£142" in out["text"] and "shows from the 2nd report" in out["text"]
    assert out["blocks"][1]["chart"]["type"] == "line"
    assert "Birdy writes two or three sentences" in out["text"]


async def test_schedule_change_keeps_the_blocks(mock_db):
    from routers.onboarding import BriefConfigRequest, set_slack_brief
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)], brief=V({"type": "header", "text": "Mine"}))
    out = await set_slack_brief(BriefConfigRequest(frequency="weekly", time="8:00 AM", day="Friday"), current_user=USER)
    assert out["brief"]["day"] == "Friday" and out["brief"]["blocks"] == [{"type": "header", "text": {"type": "plain_text", "text": "Mine"}}]


async def test_slack_status_carries_the_report_for_settings(mock_db):
    from services.slack_bot_service import get_slack_bot_status
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)],
                brief={"frequency": "weekly", "day": "Friday", "time": "8:00 AM", "items": {"spend": True}},
                team_id="T1", bot_user_id="U1")
    status = await get_slack_bot_status(mock_db, USER)
    assert status["brief"]["version"] == 4 and status["brief"]["day"] == "Friday"


async def test_too_many_charts_are_fitted_not_refused(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)])
    chart = lambda t, by="client": {"type": "data_visualization", "title": t,
                                    "birdy_data": {"chart": "bar" if by == "client" else "line", "by": by, "metrics": ["spend"]}}
    out = await tools.propose_report(mock_db, USER, template={"frequency": "daily", "time": "9:00 AM", "blocks": [
        chart("One"), chart("Two", "day"), chart("Three"), chart("Four", "day")]})
    assert out["ok"] is True
    kinds = [b["type"] for b in out["draft"]["blocks"]]
    assert kinds == ["data_visualization", "data_visualization", "data_table"]
    assert any("“Three” became a sortable table" in n for n in out["adjusted"])
    assert any("“Four” was left out" in n for n in out["adjusted"])


async def test_a_failed_proposal_says_nothing_was_saved(mock_db):
    await _seed(mock_db, [_group("g1", "Glow Clinic", 100, 10)])
    out = await tools.propose_report(mock_db, USER, template={"frequency": "daily", "time": "9:00 AM",
        "blocks": [{"type": "data_visualization", "title": "x", "birdy_data": {"chart": "line", "by": "day", "metrics": ["leads"]}}]})
    assert out["ok"] is False and out["note"].startswith("NOT saved")


def test_small_slips_are_fitted():
    t, notes = rb.fit_to_slack({"blocks": [
        {"type": "data_table", "birdy_data": {"metrics": ["spend"]}},
        {"type": "carousel", "birdy_data": {"metrics": ["spend", "leads", "cpl", "ctr"]}}]})
    assert t["blocks"][0]["caption"] == "Clients"
    assert t["blocks"][1]["birdy_data"]["metrics"] == ["spend", "leads", "cpl"]
    assert notes == ["Client cards fit 3 numbers each, so they show the first three you picked."]
