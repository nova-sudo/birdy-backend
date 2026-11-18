"""
ai/mcp/cards.py
---------------
Birdy cards inside AI apps (MCP Apps, mocks/mcp-flavour #11-#24). A tool that has a card points at it with
`_meta.ui.resourceUri`; the host (Claude) fetches that `ui://` resource once, renders the HTML in a sandboxed
frame, and hands it the tool result. The card reads `structuredContent`, so the tool's normal JSON return is
the card's data; hosts without MCP Apps simply show the text as before.

The view talks to the host through the official SDK (@modelcontextprotocol/ext-apps), loaded from jsDelivr at
a pinned version; the CSP below allows that origin and this API (for the mascot icon). Cards are kept as Python
strings (ai/mcp/card_views.py) so the serverless bundle always has them.
"""

import hashlib
import json

from mcp.types import ReadResourceRequest

from ai.mcp import card_views
from ai.mcp.server import mcp
from core.config import API_PUBLIC_URL

MIME = "text/html;profile=mcp-app"
SDK_ORIGIN = "https://cdn.jsdelivr.net"
SDK_URL = f"{SDK_ORIGIN}/npm/@modelcontextprotocol/ext-apps@2.0.4/dist/src/app-with-deps.js"


def version() -> str:
    """A short fingerprint of everything a host caches for a card: the HTML of every card and the CSP."""
    blob = json.dumps([card_views._SHELL, CARDS, SDK_URL, API_PUBLIC_URL, _UI_META], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def uri(name: str) -> str:
    """The card's address. It carries version(): hosts cache a card (and its CSP) by address, and a cached bad
    CSP kept every card on "Rendering widget" after the server was fixed (2026-11-18)."""
    return f"ui://birdy/{name}.{version()}.html"


def card(name: str) -> dict:
    """The tool `meta` that attaches card `name` (both the current key and the older flat one)."""
    return {"ui": {"resourceUri": uri(name)}, "ui/resourceUri": uri(name)}


# Only the SDK's CDN and this API. Adding Meta's image hosts (with a *.fbcdn.net wildcard) stopped Claude
# showing any card ("Rendering widget" forever, 2026-11-18), so creative thumbnails aren't shown in cards.
_CSP = {"resourceDomains": [SDK_ORIGIN, API_PUBLIC_URL]}
_UI_META = {"ui": {"csp": _CSP, "prefersBorder": False}}


def _register(name: str, title: str, body: str) -> None:
    html = card_views.page(title, body, sdk_url=SDK_URL, icon_url=f"{API_PUBLIC_URL}/birdy-mcp-icon.png")

    @mcp.resource(uri(name), name=f"birdy_card_{name}", title=title, mime_type=MIME, meta=_UI_META)
    def _view() -> str:
        return html


def attach_content_meta(server) -> None:
    """Send each card's `_meta.ui` with its content in resources/read, where hosts read the CSP from.

    The list entry alone isn't enough: without the CSP on the content the sandbox blocks the SDK script, the
    card never connects and Claude shows "Rendering widget" forever. This FastMCP/mcp version can't return
    content `_meta` from a resource function, so the read handler's result is filled in here."""
    handlers = server._mcp_server.request_handlers
    read = handlers[ReadResourceRequest]

    async def read_with_card_meta(req):
        result = await read(req)
        for item in getattr(getattr(result, "root", result), "contents", None) or []:
            if str(item.uri).startswith("ui://birdy/"):
                item.meta = {**(item.meta or {}), **_UI_META}
        return result

    handlers[ReadResourceRequest] = read_with_card_meta


CARDS = {
    "accounts": ("Birdy · all clients", card_views.ACCOUNTS),      # #12 check_my_accounts
    "scorecard": ("Birdy · client scorecard", card_views.SCORECARD),  # #11 compare_periods
    "alert": ("Birdy · alert", card_views.ALERT),                  # #13 explain_alert, get_fired_alerts
    "confirm": ("Birdy · live change", card_views.CONFIRM),        # #15 every LIVE tool
    "summary": ("Birdy · numbers", card_views.SUMMARY),            # #23 get_account_summary
    "ads": ("Birdy · ad leaderboard", card_views.LEADERBOARD),     # #16 get_ad_insights
    "pacing": ("Birdy · budget pacing", card_views.PACING),        # #19 get_budget_pacing
    "creative_test": ("Birdy · creative test", card_views.CREATIVE_TEST),  # #21 get_creative_test
    "funnel": ("Birdy · funnel", card_views.FUNNEL),               # #22 funnel
    "leads": ("Birdy · leads", card_views.LEADS),                  # #17 get_unified_leads
    "report": ("Birdy · report preview", card_views.REPORT),       # #18 preview_report
    "agent_week": ("Birdy · agent's week", card_views.AGENT_WEEK), # #20 get_agent_cards
}

for _name, (_title, _body) in CARDS.items():
    _register(_name, _title, _body)


def attach_live_cards(server) -> None:
    """Every LIVE tool gets the confirm card: its preview (two_step) shows with Confirm and Cancel buttons."""
    for tool in server._tool_manager._tools.values():
        if "live" in (tool.tags or set()) and not (tool.meta or {}).get("ui"):
            tool.meta = {**(tool.meta or {}), **card("confirm")}
