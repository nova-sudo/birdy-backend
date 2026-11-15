"""
ai/mcp/prompts_mcp.py
---------------------
Ready-made starters for Birdy's MCP server. A prompt is a text the user picks in Claude or ChatGPT; it names the
Birdy tools to use, so the assistant reaches for them in the right order. Prompts are not tools, so the access
middleware does not gate them: any token that can connect can list and use these.
"""

from ai.mcp.server import mcp


@mcp.prompt
def weekly_client_review() -> str:
    """Weekly review of every client: what needs attention and why, then an action list."""
    return (
        "Use Birdy's check_my_accounts for the last 7 days. For each client that needs attention, explain what "
        "changed and why using get_client, compare_periods and the agent's cards, then give me a short action list."
    )


@mcp.prompt
def why_did_cpl_jump(client: str) -> str:
    """Find out why one client's cost per lead changed."""
    return (
        f"Using Birdy, find out why CPL changed for {client}. Read get_client notes first, compare this week to "
        "last with compare_periods, break it down by campaign and ad, and check get_agent_cards for related "
        "findings. End with the likely cause and what to do."
    )


@mcp.prompt
def prep_client_call(client: str) -> str:
    """A one-page brief for a call with one client."""
    return (
        f"Prep me for a call with {client} using Birdy: last 30 days results vs target, best and worst ads, lead "
        "quality from get_unified_lead_stats, open agent cards, and recent notes. Format as a one-page brief."
    )


@mcp.prompt
def agent_week() -> str:
    """What the media buying agent did this week and what is waiting for a decision."""
    return (
        "Using Birdy, summarise what my media buying agent did this week: runs, applied and dismissed cards, "
        "outcomes, and anything waiting for my decision."
    )


@mcp.prompt
def daily_check_in() -> str:
    """Only what needs attention today, most urgent first."""
    return (
        "Use Birdy's check_my_accounts and tell me only what needs my attention today, most urgent first. "
        "If nothing does, say so in one line."
    )
