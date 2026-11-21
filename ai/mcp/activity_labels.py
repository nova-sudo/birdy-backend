"""One label per MCP tool, for the activity log's plain-English summaries.

`label` is a past-tense phrase a business owner would say ("Paused ads"). `link` is an optional relative
Birdy URL template filled from the call's args; it is set only where an obvious Birdy page exists, and only
to routes that exist in the frontend (src/app). A template whose placeholder has no value yields no link.
A test asserts every registered tool has an entry, so a new tool cannot ship without one.
"""

_AGENT_PAGE = "/hub/agents/media-buying"
_ALERTS = "/alerts"
_SUGGESTIONS = "/dashboard"
_SLACK_REPORTS = "/hub"
_TEST_EDIT = "/campaigns/creatives/{test_id}/edit"


def _l(label: str, link: str | None = None) -> dict:
    return {"label": label, "link": link}


LABELS: dict[str, dict] = {
    # Alerts
    "get_alerts": _l("Checked alerts", _ALERTS),
    "create_alert": _l("Created an alert", _ALERTS),
    "update_alert": _l("Updated an alert", _ALERTS),
    "get_fired_alerts": _l("Looked at alerts that fired", _ALERTS),
    "explain_alert": _l("Looked into why an alert fired", _ALERTS),
    "check_my_accounts": _l("Checked accounts", "/clients"),
    "get_started": _l("Checked what Birdy can do", _SUGGESTIONS),
    # Asking a team agent (ai/mcp/team_mcp.py): the reply lives in that agent's chat
    "ask_media_buyer": _l("Asked Media Buyer", "/media-buyer/chat"),
    "ask_sales_coach": _l("Asked Sales Coach", "/sales-coach/chat"),
    # Meta and lead data
    "get_campaign_insights": _l("Looked at campaign results"),
    "get_adset_insights": _l("Looked at ad set results"),
    "get_ad_insights": _l("Looked at ad results"),
    "get_facebook_leads": _l("Looked at Facebook leads"),
    "get_meta_insights_live": _l("Pulled live Meta results"),
    "get_meta_leads_live": _l("Pulled live Meta leads"),
    "get_meta_insights_monthly": _l("Looked at monthly Meta results"),
    "find_ads": _l("Searched for ads"),
    "list_ad_accounts": _l("Listed ad accounts"),
    "list_adsets": _l("Listed ad sets"),
    # GoHighLevel and leads
    "get_ghl_contacts": _l("Looked at contacts"),
    "get_ghl_opportunity_stats": _l("Looked at pipeline stats"),
    "get_ghl_opp_stats_windowed": _l("Looked at pipeline stats for a period"),
    "get_ghl_opp_stats_monthly": _l("Looked at monthly pipeline stats"),
    "get_ghl_tag_breakdown": _l("Looked at tag breakdowns"),
    "get_tag_rollup_by_campaign": _l("Looked at tags by campaign"),
    "get_unified_leads": _l("Looked at leads"),
    "get_unified_lead_stats": _l("Looked at lead stats"),
    "get_lead": _l("Opened a lead"),
    "get_lead_messages": _l("Read a lead's messages"),
    "get_call_transcript": _l("Read a call transcript"),
    "send_lead_message": _l("Texted a lead"),
    # Clients
    "get_client_groups": _l("Listed clients"),
    "get_client": _l("Looked at a client"),
    "get_client_notes": _l("Read client notes"),
    "add_client_note": _l("Added a client note"),
    "delete_client_note": _l("Deleted a client note"),
    "set_client_target": _l("Set a client target"),
    "refresh_client_data": _l("Refreshed client data"),
    "refresh_status": _l("Checked data freshness"),
    # Summaries and comparisons
    "get_account_summary": _l("Looked at the account summary"),
    "compare_periods": _l("Compared two periods"),
    "get_budget_pacing": _l("Checked budget pacing", "/clients"),
    "get_daily_metrics": _l("Looked at daily numbers"),
    "get_metrics_by_day_windows": _l("Looked at numbers by time window"),
    "get_call_center_stats": _l("Looked at call centre stats"),
    "get_call_recordings_summary": _l("Looked at call recordings"),
    "analyze_call_recordings": _l("Analysed call recordings"),
    # Custom metrics
    "list_custom_metrics": _l("Listed custom metrics"),
    "compute_custom_metric": _l("Calculated a custom metric"),
    "list_available_metric_fields": _l("Listed metric fields"),
    "create_custom_metric": _l("Created a custom metric"),
    "update_custom_metric": _l("Updated a custom metric"),
    "delete_custom_metric": _l("Deleted a custom metric"),
    # Attribution
    "get_closes_by_ad": _l("Looked at closes by ad"),
    "attribution_status": _l("Checked attribution status"),
    "leads_by_ad": _l("Looked at leads by ad"),
    "funnel": _l("Looked at the funnel"),
    "attribution_overview": _l("Looked at the attribution overview"),
    # Agents (stock media buyer)
    "list_agents": _l("Listed agents"),
    "set_default_agent": _l("Set the default agent"),
    "get_agent_overview": _l("Looked at the agent overview", _AGENT_PAGE),
    "get_agent_runs": _l("Looked at agent runs", _AGENT_PAGE),
    "get_agent_run": _l("Opened an agent run", _AGENT_PAGE),
    "get_agent_month": _l("Looked at an agent month", _AGENT_PAGE),
    "get_agent_cards": _l("Looked at agent cards", _AGENT_PAGE),
    "get_agent_card": _l("Opened an agent card", _AGENT_PAGE),
    "get_agent_lessons": _l("Read agent lessons", _AGENT_PAGE),
    "get_agent_outcomes": _l("Looked at agent outcomes", _AGENT_PAGE),
    "get_agent_slack_template": _l("Looked at the agent's Slack template"),
    "apply_card": _l("Applied an agent card", _AGENT_PAGE),
    "apply_all_cards": _l("Applied all agent cards", _AGENT_PAGE),
    "undo_card_line": _l("Undid a card change", _AGENT_PAGE),
    "post_weekly_report": _l("Posted the weekly report", _AGENT_PAGE),
    "dismiss_card": _l("Dismissed an agent card", _AGENT_PAGE),
    "edit_card": _l("Edited an agent card", _AGENT_PAGE),
    "add_agent_lesson": _l("Taught the agent a lesson", _AGENT_PAGE),
    "update_agent_settings": _l("Changed agent settings", _AGENT_PAGE),
    "set_agent_slack_template": _l("Changed the agent's Slack template"),
    "reset_agent_slack_template": _l("Reset the agent's Slack template"),
    # Custom agents
    "get_agent_playbook": _l("Read an agent playbook"),
    "get_agent_activity": _l("Looked at agent activity"),
    "get_agent_history": _l("Looked at agent history"),
    "get_agent_claims": _l("Looked at what an agent manages"),
    "get_agent_impact": _l("Looked at agent impact"),
    "get_agent_confidence": _l("Looked at agent confidence"),
    "get_agent_proposals": _l("Looked at agent proposals"),
    "get_agent_memory": _l("Read agent memory"),
    "get_agent_offers": _l("Looked at agent offers"),
    "get_dry_run": _l("Looked at a dry run"),
    "accept_proposal": _l("Accepted a proposal"),
    "reject_proposal": _l("Rejected a proposal"),
    "set_rule_param": _l("Changed an agent rule"),
    "set_client_target_for_agent": _l("Set a client target for an agent"),
    "set_client_override": _l("Set a client override"),
    "add_agent_memory": _l("Added to agent memory"),
    "add_agent_exception": _l("Added an agent exception"),
    "set_agent_schedule": _l("Changed an agent's schedule"),
    "set_agent_slack_channel": _l("Changed an agent's Slack channel"),
    "delete_agent_memory": _l("Removed agent memory"),
    "graduate_rule": _l("Graduated an agent rule"),
    "start_dry_run": _l("Started a dry run"),
    "deactivate_agent": _l("Turned an agent off"),
    "activate_agent": _l("Turned an agent on"),
    "undo_auto_action": _l("Undid an automatic change", _SUGGESTIONS),
    # Suggestions
    "get_suggestions": _l("Looked at suggestions", _SUGGESTIONS),
    "apply_suggestion": _l("Applied a suggestion", _SUGGESTIONS),
    "deny_suggestion": _l("Dismissed a suggestion", _SUGGESTIONS),
    "undo_suggestion": _l("Undid a suggestion", _SUGGESTIONS),
    # Creatives
    "list_creative_tests": _l("Listed creative tests", "/campaigns"),
    "get_creative_test": _l("Opened a creative test"),
    "draft_creative_test": _l("Drafted a creative test", _TEST_EDIT),
    "post_creative_test": _l("Posted a creative test", _TEST_EDIT),
    "scale_creative_test": _l("Scaled a creative test", _TEST_EDIT),
    # Slack reports
    "list_slack_reports": _l("Listed Slack reports", _SLACK_REPORTS),
    "create_slack_report": _l("Created a Slack report", _SLACK_REPORTS),
    "update_slack_report": _l("Updated a Slack report", _SLACK_REPORTS),
    "delete_slack_report": _l("Deleted a Slack report", _SLACK_REPORTS),
    "send_test_slack_report": _l("Sent a test Slack report", _SLACK_REPORTS),
    # Report designer
    "list_report_clients": _l("Listed report clients"),
    "list_report_metrics": _l("Listed report metrics"),
    "get_report_draft": _l("Opened a report draft"),
    "preview_report": _l("Previewed a report"),
    "propose_report": _l("Proposed a report"),
}
