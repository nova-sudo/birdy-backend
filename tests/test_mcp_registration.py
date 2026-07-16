"""
Guards against accidentally losing or duplicating a tool on the shared MCP
server (ai/mcp/server.py) during future refactors — every module's tools
must still be registered, with schemas an LLM can actually call.
"""

import pytest

EXPECTED_TOOLS_BY_MODULE = {
    "alert_mcp": {"get_alerts", "create_alert", "update_alert"},
    "meta_mcp": {"get_campaign_insights", "get_adset_insights", "get_ad_insights", "get_facebook_leads"},
    "ghl_mcp": {
        "get_ghl_contacts", "get_ghl_opportunity_stats", "get_ghl_opp_stats_windowed",
        "get_ghl_opp_stats_monthly", "get_ghl_tag_breakdown", "get_tag_rollup_by_campaign",
    },
    "group_mcp": {"get_client_groups"},
    "summary_mcp": {"get_account_summary"},
    "compare_mcp": {"compare_periods"},
    "meta_live_mcp": {"get_meta_insights_live", "get_meta_leads_live"},
    "custom_metrics_mcp": {
        "list_custom_metrics", "compute_custom_metric", "list_available_metric_fields",
        "create_custom_metric", "update_custom_metric", "delete_custom_metric",
    },
    "unified_leads_mcp": {"get_unified_leads", "get_unified_lead_stats"},
}

ALL_EXPECTED_TOOLS = set().union(*EXPECTED_TOOLS_BY_MODULE.values())


@pytest.mark.asyncio
async def test_all_expected_tools_are_registered_exactly_once():
    from ai.mcp import mcp

    tools = await mcp.get_tools()
    assert set(tools.keys()) == ALL_EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_every_tool_has_a_nonempty_description():
    # An empty description means an LLM has no idea when to call the tool —
    # catches an f-string-used-as-docstring mistake (see alert_mcp.py's
    # _CREATE_ALERT_DESCRIPTION pattern) silently producing an empty __doc__.
    from ai.mcp import mcp

    tools = await mcp.get_tools()
    empty = [name for name, tool in tools.items() if not (tool.description or "").strip()]
    assert empty == [], f"Tools with empty descriptions: {empty}"


@pytest.mark.asyncio
async def test_every_tool_has_a_valid_object_schema():
    from ai.mcp import mcp

    tools = await mcp.get_tools()
    for name, tool in tools.items():
        assert tool.parameters.get("type") == "object", f"{name} has a non-object input schema"
