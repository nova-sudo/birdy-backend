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
    "meta_live_mcp": {"get_meta_insights_live", "get_meta_leads_live", "get_meta_insights_monthly"},
    "custom_metrics_mcp": {
        "list_custom_metrics", "compute_custom_metric", "list_available_metric_fields",
        "create_custom_metric", "update_custom_metric", "delete_custom_metric",
    },
    "unified_leads_mcp": {"get_unified_leads", "get_unified_lead_stats"},
    "agent_mcp": {"list_agents", "set_default_agent"},
    "stock_agent_mcp": {"get_agent_overview", "get_agent_runs", "get_agent_run", "get_agent_month", "get_agent_cards", "get_agent_card", "get_agent_lessons", "get_agent_outcomes", "get_agent_slack_template",
                        "apply_card", "apply_all_cards", "undo_card_line", "post_weekly_report", "dismiss_card", "edit_card",
                        "add_agent_lesson", "update_agent_settings", "set_agent_slack_template", "reset_agent_slack_template"},
    "custom_agent_mcp": {"get_agent_playbook", "get_agent_activity", "get_agent_history", "get_agent_claims", "get_agent_impact", "get_agent_confidence", "get_agent_proposals", "get_agent_memory", "get_agent_offers", "get_dry_run", "accept_proposal", "reject_proposal", "set_rule_param", "set_client_target_for_agent", "set_client_override", "add_agent_memory", "add_agent_exception", "set_agent_schedule", "set_agent_slack_channel", "delete_agent_memory", "graduate_rule", "start_dry_run", "deactivate_agent", "activate_agent", "undo_auto_action"},
    "suggestions_mcp": {"get_suggestions", "apply_suggestion", "deny_suggestion", "undo_suggestion"},
    "multi_window_mcp": {"get_daily_metrics", "get_metrics_by_day_windows"},
    "hp_mcp": {"get_call_center_stats"},
    "call_analysis_mcp": {"get_call_recordings_summary", "analyze_call_recordings"},
    "creatives_mcp": {"find_ads", "list_ad_accounts", "list_adsets", "list_creative_tests", "get_creative_test", "draft_creative_test", "post_creative_test", "scale_creative_test"},
    "attribution_mcp": {"get_closes_by_ad", "attribution_status", "leads_by_ad", "funnel", "attribution_overview"},
    "client_mcp": {"get_client", "get_client_notes", "add_client_note", "delete_client_note", "set_client_target"},
    "leads_mcp": {"get_lead", "get_lead_messages", "get_call_transcript", "send_lead_message"},
    "slack_reports_mcp": {"list_slack_reports", "create_slack_report", "update_slack_report", "delete_slack_report", "send_test_slack_report"},
    "report_designer_mcp": {"list_report_clients", "list_report_metrics", "get_report_draft", "preview_report", "propose_report"},
    "refresh_mcp": {"refresh_client_data", "refresh_status"},
    "watch_mcp": {"check_my_accounts", "get_fired_alerts", "explain_alert"},
    "prompts_mcp": set(),  # prompts only, no tools
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
