"""
ai/mcp_client.py is the bridge ai/orchestrator.py uses to reach the MCP
server internally. These tests cover the pure-logic pieces: the per-page
gating that decides whether an MCP client is even constructed, the internal
token minting (and its timezone-safety — see the docstring in mcp_client.py),
and that MCP_TOOL_NAMES matches what's actually registered on the server.
"""

import time

import jwt as pyjwt
import pytest

from ai import mcp_client
from core.config import JWT_SECRET, JWT_ALGORITHM


def test_needs_mcp_true_when_page_unknown_or_none():
    assert mcp_client.needs_mcp(None) is True


def test_needs_mcp_true_when_allowlist_includes_an_mcp_tool():
    assert mcp_client.needs_mcp(["get_client_groups", "get_alerts"]) is True


def test_needs_mcp_false_when_allowlist_has_no_mcp_tools():
    # None of these exist in MCP_TOOL_NAMES at time of writing; this asserts
    # the *shape* of the check, not a specific tool list.
    assert mcp_client.needs_mcp(["some_tool_not_in_mcp_yet"]) is False


def test_mint_internal_token_round_trips_and_is_not_expired():
    token = mcp_client._mint_internal_token("user@example.com")
    claims = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

    assert claims["sub"] == "user@example.com"
    assert claims["type"] == "access"
    # Must be in the future relative to real wall-clock time (time.time()),
    # not a naive-datetime.utcnow().timestamp() computation — that pattern
    # silently mints already-expired tokens on any non-UTC host.
    assert claims["exp"] > time.time()
    assert claims["exp"] <= time.time() + mcp_client._INTERNAL_TOKEN_TTL_SECONDS + 5


@pytest.mark.asyncio
async def test_mcp_tool_names_matches_server_registration():
    from ai.mcp import mcp

    registered = set((await mcp.get_tools()).keys())
    # Tools built for external MCP clients only: the orchestrator keeps its internal paths for them, so
    # they are deliberately absent from MCP_TOOL_NAMES.
    external_only = {"list_agents", "set_default_agent",
                     "get_agent_overview", "get_agent_runs", "get_agent_run", "get_agent_month", "get_agent_cards", "get_agent_card", "get_agent_lessons", "get_agent_outcomes", "get_agent_slack_template",
                     "apply_card", "apply_all_cards", "undo_card_line", "post_weekly_report", "dismiss_card", "edit_card",
                     "add_agent_lesson", "update_agent_settings", "set_agent_slack_template", "reset_agent_slack_template",
                     "get_agent_playbook", "get_agent_activity", "get_agent_history", "get_agent_claims", "get_agent_impact", "get_agent_confidence", "get_agent_proposals", "get_agent_memory", "get_agent_offers", "get_dry_run", "accept_proposal", "reject_proposal", "set_rule_param", "set_client_target_for_agent", "set_client_override", "add_agent_memory", "add_agent_exception", "set_agent_schedule", "set_agent_slack_channel", "delete_agent_memory", "graduate_rule", "start_dry_run", "deactivate_agent", "activate_agent", "undo_auto_action", "get_suggestions", "apply_suggestion", "deny_suggestion", "undo_suggestion",
                     "get_daily_metrics", "get_metrics_by_day_windows", "get_call_center_stats", "get_call_recordings_summary", "analyze_call_recordings", "find_ads", "list_ad_accounts", "list_adsets", "list_creative_tests", "get_creative_test", "draft_creative_test", "post_creative_test", "scale_creative_test", "get_closes_by_ad",
                     "get_client", "get_client_notes", "add_client_note", "delete_client_note", "set_client_target",
                     "get_lead", "get_lead_messages", "get_call_transcript",
                     "list_slack_reports", "create_slack_report", "update_slack_report", "delete_slack_report", "send_test_slack_report",
                     "attribution_status", "leads_by_ad", "funnel", "attribution_overview",
                     "refresh_client_data", "refresh_status", "check_my_accounts"}
    assert mcp_client.MCP_TOOL_NAMES == registered - external_only, (
        f"MCP_TOOL_NAMES is out of sync with the server's registered tools.\n"
        f"In MCP_TOOL_NAMES but not registered: {mcp_client.MCP_TOOL_NAMES - registered}\n"
        f"Registered but missing from MCP_TOOL_NAMES: {registered - external_only - mcp_client.MCP_TOOL_NAMES}"
    )
