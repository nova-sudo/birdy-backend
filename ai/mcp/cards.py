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

from ai.mcp import card_views
from ai.mcp.server import mcp
from core.config import API_PUBLIC_URL

MIME = "text/html;profile=mcp-app"
SDK_ORIGIN = "https://cdn.jsdelivr.net"
SDK_URL = f"{SDK_ORIGIN}/npm/@modelcontextprotocol/ext-apps@2.0.4/dist/src/app-with-deps.js"


def uri(name: str) -> str:
    return f"ui://birdy/{name}.html"


def card(name: str) -> dict:
    """The tool `meta` that attaches card `name` (both the current key and the older flat one)."""
    return {"ui": {"resourceUri": uri(name)}, "ui/resourceUri": uri(name)}


_CSP = {"resourceDomains": [SDK_ORIGIN, API_PUBLIC_URL]}


def _register(name: str, title: str, body: str) -> None:
    html = card_views.page(title, body, sdk_url=SDK_URL, icon_url=f"{API_PUBLIC_URL}/birdy-mcp-icon.png")

    @mcp.resource(uri(name), name=f"birdy_card_{name}", title=title, mime_type=MIME,
                  meta={"ui": {"csp": _CSP, "prefersBorder": False}})
    def _view() -> str:
        return html


CARDS = {
    "accounts": ("Birdy · all clients", card_views.ACCOUNTS),
}

for _name, (_title, _body) in CARDS.items():
    _register(_name, _title, _body)
