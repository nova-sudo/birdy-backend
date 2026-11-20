"""
ai/agents/sub_agents/tools.py
-----------------------------
Which tools a main agent has, and so which tools its sub-agents may be given.

MERGE POINT: `parent_tool_names` is the one place this branch learns a main agent's tool list. The
agent-hierarchy branch owns the real lists (each main agent's saved record). Until the two branches
merge, the lists are derived from today's page allowlists in ai/orchestrator.py (`_PAGE_TOOLS`):
  media_buyer  <- the "campaigns" page (the ads page)
  sales_coach  <- the "call_center" page
minus the tools that only make sense with a browser on the other end (`render_ui` and the saved
table view tools, which need the page's column catalogues). On merge, replace the body of
`parent_tool_names` with a read of the parent's record and keep the signature.
"""

from functools import lru_cache

MEDIA_BUYER = "media_buyer"
SALES_COACH = "sales_coach"
PARENTS = (MEDIA_BUYER, SALES_COACH)
PARENT_LABELS = {MEDIA_BUYER: "Media Buyer", SALES_COACH: "Sales Coach"}

_PARENT_PAGE = {MEDIA_BUYER: "campaigns", SALES_COACH: "call_center"}  # temporary, see the module note

# Words a write tool's name starts with. Used for the wording of a refusal only: the subset check
# alone decides what a sub-agent may have.
_WRITE_PREFIXES = ("create_", "update_", "delete_", "apply_", "post_", "scale_", "pause_", "draft_",
                   "send_", "set_", "add_", "remove_", "dashboard_apply", "propose_")

# Plain names for the tool pills on the sub-agent card and list. Anything missing falls back to the
# tool name with its words split out ("get_foo_bar" -> "Foo bar").
TOOL_LABELS = {
    "get_client_groups": "Client list",
    "get_campaign_insights": "Campaign results",
    "get_adset_insights": "Ad set results",
    "get_ad_insights": "Ad results",
    "get_meta_insights_live": "Live Meta results",
    "compare_periods": "Compare periods",
    "get_metrics_by_day_windows": "Results by day ranges",
    "get_call_center_stats": "Call center stats",
    "get_call_trend": "Calls against the period before",
    "get_daily_metrics": "Daily numbers",
    "get_call_recordings_summary": "Call recordings summary",
    "analyze_call_recordings": "Call analysis",
    "find_ads": "Find ads",
    "list_ad_accounts": "Ad accounts",
    "list_adsets": "Ad sets",
    "list_creative_tests": "Creative tests",
    "get_creative_test": "Creative test",
    "draft_creative_test": "Draft a creative test",
    "post_creative_test": "Post a creative test",
    "scale_creative_test": "Scale a creative test",
    "pause_creative_ads": "Pause test ads",
    "update_creative_targets": "Change test targets",
}


# A main agent's own lessons are private to it (design section 8): its sub-agents get their own.
NOT_FOR_SUB_AGENTS = frozenset({"list_lessons"})


def _ui_only_tools() -> set[str]:
    from ai.tools.page_view_tools import PAGE_VIEW_TOOL_NAMES
    from ai.tools.render_ui_tools import RENDER_UI_TOOL_NAME
    return {RENDER_UI_TOOL_NAME, *PAGE_VIEW_TOOL_NAMES}


@lru_cache(maxsize=None)
def _page_tools() -> dict:
    # Imported here: the orchestrator pulls in the providers and the MCP client, which nothing
    # else in this package needs.
    from ai.orchestrator import _PAGE_TOOLS
    return {page: tuple(names) for page, names in _PAGE_TOOLS.items()}


def parent_tool_names(parent: str) -> set[str]:
    """MERGE POINT. Every tool the main agent `parent` has; a sub-agent's tools must be a subset.
    Raises ValueError for anything but media_buyer / sales_coach."""
    if parent not in _PARENT_PAGE:
        raise ValueError(f"unknown parent agent: {parent!r}")
    # A sub-agent only ever reads and proposes, so it may be given its main agent's read tools.
    from ai.team import definitions as team_defs
    return set(team_defs.tool_names(parent, nested=True)) - _ui_only_tools() - NOT_FOR_SUB_AGENTS


@lru_cache(maxsize=None)
def known_tool_names() -> frozenset:
    """Every tool name Birdy has anywhere (every page's list and the MCP-hosted tools). A name
    outside this set is unknown, not merely outside a parent's area."""
    from ai.mcp_client import MCP_TOOL_NAMES
    names = set(MCP_TOOL_NAMES)
    for page_names in _page_tools().values():
        names.update(page_names)
    # Tools that only the team's agents have (no page lists them), e.g. get_call_trend.
    from ai.team import definitions as team_defs
    for agent in team_defs.AGENT_KEYS:
        names.update(team_defs.tool_names(agent, nested=False))
    return frozenset(names)


def is_write_tool(name: str) -> bool:
    from ai.tools.creative_tools import CREATIVE_WRITE_TOOLS
    return name in CREATIVE_WRITE_TOOLS or name.startswith(_WRITE_PREFIXES)


def tool_label(name: str) -> str:
    if name in TOOL_LABELS:
        return TOOL_LABELS[name]
    words = [w for w in name.split("_") if w]
    if words and words[0] in ("get", "list"):
        words = words[1:] or words
    text = " ".join(words)
    return text[:1].upper() + text[1:]
