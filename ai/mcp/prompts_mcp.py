"""
ai/mcp/prompts_mcp.py
---------------------
Ready-made starters for Birdy's MCP server. A prompt is a text the user picks in Claude or ChatGPT; it names the
Birdy tools to use, so the assistant reaches for them in the right order. Prompts are not tools, so the access
middleware does not gate them: any token that can connect can list and use these.

Titles are what the AI app shows in its prompt menu (mocks/mcp-flavour #27). The answer format comes from the
server instructions (ai/mcp/voice.py); a prompt only names the format when it differs (the morning brief).
"""

from ai.mcp.server import mcp


@mcp.prompt(title="Monday review")
def weekly_client_review() -> str:
    """How every client did last week: what needs attention and why, then an action list."""
    return (
        "Use Birdy's check_my_accounts for the last 7 days. For each client that needs attention, explain what "
        "changed and why using get_client, compare_periods and the agent's cards. Answer as Birdy's morning "
        "brief: a one-line headline, The win, The worry, The number, then numbered next steps."
    )


@mcp.prompt(title="What changed?")
def why_did_cpl_jump(client: str) -> str:
    """Explain a jump in one client's cost per lead or spend."""
    return (
        f"Using Birdy, find out why CPL changed for {client}. Read get_client notes first, compare this week to "
        "last with compare_periods, break it down by campaign and ad, and check get_agent_cards for related "
        "findings. End with the likely cause and what to do."
    )


@mcp.prompt(title="Call prep")
def prep_client_call(client: str) -> str:
    """One page to read before a call with a client."""
    return (
        f"Prep me for a call with {client} using Birdy: last 30 days results vs target, best and worst ads, lead "
        "quality from get_unified_lead_stats, open agent cards, and recent notes. Format as a one-page brief "
        "with three talking points at the end."
    )


@mcp.prompt(title="Agent recap")
def agent_week() -> str:
    """What the media buying agent did this week, and why."""
    return (
        "Using Birdy, summarise what my media buying agent did this week: runs, applied and dismissed cards, "
        "outcomes, and anything waiting for my decision."
    )


@mcp.prompt(title="Daily check-in")
def daily_check_in() -> str:
    """Only what needs attention today, most urgent first."""
    return (
        "Use Birdy's check_my_accounts and tell me only what needs my attention today, most urgent first, as "
        "Birdy's morning brief. If nothing does, say so in one line."
    )


@mcp.prompt(title="Fix my worst client")
def fix_worst_client() -> str:
    """A plan for the account that needs the most help right now."""
    return (
        "Use Birdy's check_my_accounts for the last 14 days and pick the one client that most needs help. Read "
        "its notes with get_client, find what is driving the problem with compare_periods and its campaigns and "
        "ads, then give me a plan of at most three steps. Offer to prepare each step (live changes as a preview)."
    )
