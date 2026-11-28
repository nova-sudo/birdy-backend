"""
Sales Coach's outcomes as Slack report metrics (services/report_sales.py), Markdown tables in report text,
the template errors that teach the fix, and the agents' report rules (ai/team/definitions.py).
Runs on mongomock: no model, Slack or GoHighLevel calls.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

import services.report_blocks as rb
from ai.team import definitions as defs
from ai.team.memory import TEAM_AGENTS
from ai.tools import report_designer_tools as tools
from services import report_sales
from services import slack_report_ops as ops

USER = "agency@x.com"
NAMES = {"g1": "Aura", "g2": "Bloom"}
NOW = datetime.now(timezone.utc)
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


async def _seed(db, *, setup=True, target=5):
    await db["users"].insert_one({"user_id": USER, "default_currency": "GBP", "timezone": "UTC"})
    await db["client_groups"].insert_many([
        {"id": "g1", "user_id": USER, "name": "Aura", "client_status": "Active", "ghl_location_id": "loc-1",
         "call_log_provider": "hotprospector",
         "hotprospector_call_cache": {"yesterday": {"total_calls": 40}}},
        {"id": "g2", "user_id": USER, "name": "Bloom", "client_status": "Active", "ghl_location_id": "loc-2",
         "call_log_provider": "hotprospector"},
    ])
    if not setup:
        return
    await db[TEAM_AGENTS].insert_one({"_id": f"sales_coach:{USER}", "user_id": USER, "key": "sales_coach", "settings": {
        "client_group_ids": "all", "speed_minutes": target,
        "outcomes": {"booked": {"kind": "tag", "value": "Booked Call", "label": "Booked Call"},
                     "closed": {"kind": "stage", "value": "st-won", "label": "Sales / Won"}}}})
    await db["client_group_tag_cache"].insert_many([
        {"user_id": USER, "group_id": "g1", "preset": "yesterday", "tags": {"booked call": 3, "new lead": 9}},
        {"user_id": USER, "group_id": "g2", "preset": "yesterday", "tags": {"Booked Call": 1}},
    ])
    await db["ghl_contacts"].insert_many([
        {"user_id": USER, "client_group_id": "g1", "contact_data": {"dateAdded": YESTERDAY + "T10:00:00Z",
                                                                  "opportunities": [{"pipelineStageId": "st-won"}]}},
        {"user_id": USER, "client_group_id": "g1", "contact_data": {"dateAdded": "2020-01-01T10:00:00Z",
                                                                  "opportunities": [{"pipelineStageId": "st-won"}]}},
    ])
    await db["hotprospector_leads"].insert_many([
        {"user_id": USER, "ghl_location_id": "loc-1", "lead_data": {"call_logs": [
            {"call_time_iso": YESTERDAY + "T10:05:00", "speed_to_lead": 120},
            {"call_time_iso": YESTERDAY + "T11:00:00", "speed_to_lead": 600}]}},
        {"user_id": USER, "ghl_location_id": "loc-2", "lead_data": {"call_logs": [
            {"call_time_iso": YESTERDAY + "T09:00:00", "speed_to_lead": 60}]}},
    ])


def V(*blocks, metrics=None):
    return rb.validate({"frequency": "daily", "time": "6:00 AM", "blocks": list(blocks)}, NAMES, metrics=metrics)


async def rendered(db, *blocks, previous=None, preview=False):
    t = V(*blocks)
    ctx = await rb.build_context(db, USER, t, NOW, previous=previous, preview=preview)
    return rb.render(t, ctx), ctx


def errors_of(*blocks, metrics=None):
    with pytest.raises(rb.TemplateError) as e:
        V(*blocks, metrics=metrics)
    return " | ".join(e.value.errors)


# ── The metrics ───────────────────────────────────────────────────────────────


async def test_catalog_lists_them_only_when_the_setup_has_what_they_need(mock_db):
    await _seed(mock_db, setup=False)
    ids = {m["id"] for m in (await tools.list_report_metrics(mock_db, USER))["metrics"]}
    assert not ids & set(report_sales.METRICS) and "hp_total_calls" in ids
    assert not set(report_sales.METRICS) & set(await rb.metric_names(mock_db, USER))

    await mock_db[TEAM_AGENTS].insert_one({"_id": f"sales_coach:{USER}", "user_id": USER, "settings": {
        "client_group_ids": "all", "outcomes": {"booked": {"kind": "tag", "value": "Booked Call"}}}})
    ids = {m["id"] for m in (await tools.list_report_metrics(mock_db, USER))["metrics"]}
    assert {"booked", "speed_to_lead"} <= ids and not {"closed", "speed_within_target"} & ids   # no stage, no target

    await mock_db[TEAM_AGENTS].update_one({"_id": f"sales_coach:{USER}"}, {"$set": {
        "settings.outcomes.closed": {"kind": "stage", "value": "st-won"}, "settings.speed_minutes": 5}})
    assert set(report_sales.METRICS) <= set(await rb.metric_names(mock_db, USER))


async def test_placeholders_count_with_the_owners_definitions(mock_db):
    await _seed(mock_db)
    (blocks, text), _ = await rendered(mock_db, {"type": "section", "fields": [
        {"type": "mrkdwn", "text": "*Booked*\n{{booked}}"},
        {"type": "mrkdwn", "text": "*Closed*\n{{closed client=\"Aura\"}}"},
        {"type": "mrkdwn", "text": "*Speed to lead*\n{{speed_to_lead}}"},
        {"type": "mrkdwn", "text": "*Within target*\n{{speed_within_target client=\"Aura\"}}"}]})
    fields = [f["text"] for f in blocks[0]["fields"]]
    # booked: the tag (any case) on both clients; closed: Aura's contact added yesterday in the stage;
    # speed: (120 + 600 + 60) s over 3 calls = 4.3 min; Aura within 5 min: 1 of 2 calls
    assert fields == ["*Booked*\n4", "*Closed*\n1", "*Speed to lead*\n4.3 min", "*Within target*\n50.0%"]


async def test_a_table_by_client_and_its_total_row(mock_db):
    await _seed(mock_db)
    (blocks, _), _ = await rendered(mock_db, {"type": "data_table", "caption": "Sales team", "birdy_data": {
        "metrics": ["booked", "closed", "speed_to_lead"], "sort": "booked", "total_row": True}})
    rows = [[c["text"] for c in r] for r in blocks[0]["rows"]]
    assert rows == [["Client", "Booked", "Closed", "Speed to lead (min)"],
                    ["Aura", "3", "1", "6.0 min"],
                    ["Bloom", "1", "n/a", "1.0 min"],    # no contacts cached: no data, never 0
                    ["Total", "4", "1", "4.3 min"]]


async def test_change_compares_with_the_day_before_as_every_metric_does(mock_db):
    await _seed(mock_db)
    block = {"type": "section", "text": {"type": "mrkdwn", "text": "Booked {{booked}} {{booked show=change}}"}}
    (_, _), ctx = await rendered(mock_db, block)
    snap = rb.snapshot(ctx, "2026-11-27")
    assert snap["values"]["booked:portfolio@yesterday"] == 4.0
    previous = {**snap, "values": {"booked:portfolio@yesterday": 2.0}}
    (blocks, _), _ = await rendered(mock_db, block, previous=previous)
    assert blocks[0]["text"]["text"] == "Booked 4 ▲ 100% vs the day before"


async def test_a_compare_chart_puts_yesterday_next_to_the_day_before(mock_db):
    await _seed(mock_db)
    chart = {"type": "data_visualization", "title": "Booked: yesterday vs day before",
             "birdy_data": {"chart": "bar", "by": "client", "metrics": ["booked"], "compare": "previous"}}
    # The first send has nothing to compare with: today's bars, and a preview says why.
    (blocks, _), ctx = await rendered(mock_db, chart, preview=True)
    assert [s["name"] for s in blocks[0]["chart"]["series"]] == ["Booked"]
    assert "the day before shows next to yesterday" in blocks[1]["elements"][0]["text"]
    # The chart's own figures are kept for the next send.
    assert ctx.values["booked:g1@yesterday"] == 3.0

    previous = {"values": {"booked:g1@yesterday": 1.0, "booked:g2@yesterday": 2.0}}
    (blocks, _), _ = await rendered(mock_db, chart, previous=previous)
    series = {s["name"]: [d["value"] for d in s["data"]] for s in blocks[0]["chart"]["series"]}
    assert series == {"Yesterday": [3.0, 1.0], "The day before": [1.0, 2.0]}
    assert blocks[0]["chart"]["axis_config"]["categories"] == ["Aura", "Bloom"] and len(blocks) == 1


def test_compare_and_by_day_rules_teach_the_fix():
    assert "needs by='client'" in errors_of({"type": "data_visualization", "title": "x", "birdy_data": {
        "chart": "line", "by": "day", "metrics": ["calls"], "compare": "previous"}})
    err = errors_of({"type": "data_visualization", "title": "x", "birdy_data": {
        "chart": "line", "by": "day", "metrics": ["booked"]}})
    assert "\"compare\": \"previous\"" in err and "yesterday vs the day before" in err


# ── Markdown tables ───────────────────────────────────────────────────────────


def test_a_plain_markdown_table_becomes_a_native_table():
    t = V({"type": "section", "text": {"type": "mrkdwn", "text": (
        "*Sales team*\n| Client | Booked | Closed |\n| --- | ---: | ---: |\n"
        "| **Aura** | {{booked client=\"Aura\"}} | {{closed client=\"Aura\"}} |\nSee you tomorrow")}})
    kinds = [b["type"] for b in t["blocks"]]
    assert kinds == ["section", "table", "section"]
    assert t["blocks"][0]["text"]["text"] == "*Sales team*" and t["blocks"][2]["text"]["text"] == "See you tomorrow"
    assert [[c["text"] for c in r] for r in t["blocks"][1]["rows"]] == [
        ["Client", "Booked", "Closed"], ["Aura", "{{booked client=\"Aura\"}}", "{{closed client=\"Aura\"}}"]]


def test_a_markdown_table_with_typed_figures_is_refused_with_the_fix():
    err = errors_of({"type": "markdown", "text": (
        "| Client | Booked | Closed | Speed |\n| --- | ---: | ---: | ---: |\n| Aura | 3 | 0 | 2545.7 min |")})
    assert "figures typed into it" in err and "data_table" in err and "birdy_data" in err
    assert "Valid metric names for this account" in err
    err = errors_of({"type": "section", "fields": [{"type": "mrkdwn", "text": "| a | b |\n|---|---|\n| x | y |"}]})
    assert "doesn't render in Slack" in err


# ── Errors that list the account's metrics ────────────────────────────────────


def test_a_bad_placeholder_lists_the_valid_metric_names():
    err = errors_of({"type": "section", "text": "{{conversion rate}}"}, metrics=["spend", "leads", "booked"])
    assert err.endswith("Valid metric names for this account: spend, leads, booked. Use them as {{name}} "
                        "placeholders or in birdy_data \"metrics\"; by='day' charts take only "
                        + ", ".join(rb.DAILY_METRICS) + ".")
    assert "Valid metric names" not in errors_of({"type": "header"})   # not a metric problem


async def test_the_create_tool_error_carries_the_accounts_catalog(mock_db):
    await _seed(mock_db, setup=False)
    body = ops.ReportBody(name="Daily", trigger={"type": "schedule", "frequency": "daily", "time": "06:00",
                                                 "timezone": "Africa/Cairo"},
                          template={"frequency": "daily", "blocks": [{"type": "section", "text": "{{conversion_rate}}"}]})
    with pytest.raises(ops.ReportError) as e:
        await ops.create_report(mock_db, USER, body)
    hint = e.value.detail["errors"][-1]
    assert hint.startswith("Valid metric names for this account:") and "hp_total_calls" in hint
    assert "booked" not in hint.split(";")[0]


def test_tool_descriptions_teach_live_numbers_only():
    from ai.mcp import slack_reports_mcp as m
    for tool in (m.create_slack_report, m.update_slack_report):
        d = tool.description
        assert "never type a figure" in d and "data_table" in d and "data_visualization" in d
        assert '"compare": "previous"' in d and "speed_to_lead" in d and "call again" in d
    assert "Never type a figure" in rb.PLACEHOLDER_GUIDE and "{{booked}}" in rb.PLACEHOLDER_GUIDE
    assert '"compare": "previous"' in rb.BLOCKS_GUIDE and '"booked"' in rb.BLOCKS_GUIDE


# ── The agents ────────────────────────────────────────────────────────────────


async def test_every_agent_fixes_its_own_report_errors_and_birdy_routes_reports(mock_db):
    for agent in (defs.BIRDY, defs.MEDIA_BUYER, defs.SALES_COACH):
        prompt = await defs.build_instructions(agent, mock_db, USER, client_group_id=None, client_name=None,
                                               asked_by=None)
        assert "correct it and call again yourself" in prompt and "whatever you" in prompt
        assert "list_report_metrics" in defs.tool_names(agent, nested=False)
        assert "list_report_metrics" in defs.tool_names(agent, nested=True)
    birdy = await defs.build_instructions(defs.BIRDY, mock_db, USER, client_group_id=None, client_name=None,
                                          asked_by=None)
    assert "media buying reports" in birdy and "Never" in birdy and "create one yourself" in birdy
    asked = await defs.build_instructions(defs.MEDIA_BUYER, mock_db, USER, client_group_id=None, client_name=None,
                                          asked_by="Birdy")
    assert "kind `create_slack_report`" in asked
    # An asked agent's proposal can name its own create tool, so the approved report is that agent's.
    assert "create_slack_report" in defs.write_tools(defs.MEDIA_BUYER)
    assert "create_slack_report" in defs.write_tools(defs.SALES_COACH)
