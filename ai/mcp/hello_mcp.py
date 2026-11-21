"""
ai/mcp/hello_mcp.py
-------------------
Birdy's first hello in an AI app (mocks/mcp-flavour #30): which clients Birdy can see, what this connection may
do, and a few questions worth trying. The server instructions ask the assistant to call it on the first Birdy
request in a chat or when the user asks what Birdy can do.
"""

from fastmcp.server.dependencies import get_access_token

from ai.mcp.access import READ, WRITE_SCOPE, token_scopes
from ai.mcp.server import mcp, current_user_id
from core.config import FRONTEND_URL
from core.database import get_db
from core.mongo_client import get_shared_mongo_client

NAMES_SHOWN = 10

CAN_DO = [
    "Show how any client is doing: spend, leads, cost per lead, booked calls",
    "Explain why a number moved, down to the campaign and ad",
    "Watch alerts and explain why one fired",
    "Draft and send client reports to Slack",
    "Run the Media Buying Agent and review its suggestions",
    "Ask Media Buyer (ads) or Sales Coach (calls) what to do, using your agency's lessons and context. "
    "Each ask costs credits like a chat reply in Birdy",
]
CAN_CHANGE = "Make ad changes and message leads. You confirm each live change after a preview."
VIEW_ONLY = "This connection is view-only: Birdy can look but not change anything."

TRY_ASKING = [
    "Which client needs me most this week?",
    "Why did my cost per lead go up?",
    "Prep me for my call with my biggest client.",
]


@mcp.tool(**READ)
async def get_started() -> dict:
    """What Birdy can do in this chat and which clients it can see.

    Call this on the first Birdy request in a chat, or when the user asks what Birdy can do. Greet the user in
    two lines at most, list what Birdy can do, and suggest one of `try_asking`."""
    user_id = current_user_id()
    db = get_db(get_shared_mongo_client())
    groups = await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}}, {"name": 1, "_id": 0},
    ).to_list(None)
    names = sorted(g["name"] for g in groups if g.get("name"))
    token = get_access_token()
    can_change = WRITE_SCOPE in token_scopes(token.claims if token else {})
    return {
        "clients": len(names),
        "client_names": names[:NAMES_SHOWN],
        "access": "view and make changes" if can_change else "view only",
        "can_do": CAN_DO + [CAN_CHANGE if can_change else VIEW_ONLY],
        "try_asking": TRY_ASKING,
        "birdy_url": FRONTEND_URL,
    }
