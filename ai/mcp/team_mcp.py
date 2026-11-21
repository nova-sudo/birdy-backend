"""
ai/mcp/team_mcp.py
------------------
Ask one of Birdy's team agents from an outside assistant: `ask_media_buyer` (ads) and `ask_sales_coach`
(calls). Each ask is one reply of that agent's own chat, a root run made through the same path as the
web chat (routers/chat.run_chat_reply: the owner checks, the credit gate, run_chat). So the agent works
with its role, the agency's context cards, its saved lessons and sub-agents, may ask Birdy, is billed
once like a web chat reply, and the conversation is archived under that agent: it shows in the agent's
chat list in Birdy and the other agents can recall it.

What comes back is plain text: the agent's answer, a short trail of the route (who worked on it, the
tools they used, seconds, credits), any proposals the reply made, and the conversation id to continue.
Proposals are only listed: approving or declining one is the owner's, in Birdy. Nothing here touches them.

Billing: the chat engine debits the reply once (ledger source "birdy", feature "ask_birdy"); the tool
records the outcome CHAT_BILLED so the access middleware adds no per-call MCP charge and its "Birdy:"
line shows what the reply cost, with the link to the conversation.

Time: Vercel ends a request at 300 s. The reply's own budget (ai/team/route.TURN_BUDGET_SECONDS, 250 s)
already makes the agent answer in time; ASK_LIMIT_SECONDS is the backstop. Past it the tool stops waiting
and says so; the reply keeps running in its own task, so if it finishes it is still saved, billed once and
shown in Birdy.
"""

import asyncio
import logging
from urllib.parse import urlencode

from fastapi import HTTPException
from fastmcp.exceptions import ToolError

from ai.mcp import server as _server
from ai.mcp.access import WRITE
from ai.mcp.client_mcp import resolve_client
from ai.mcp.outcome import CHAT_BILLED, set_outcome
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.team import definitions as team_defs
from ai.team import route as team_route
from core.config import FRONTEND_URL
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services.mcp_billing import OUT_OF_CREDITS_MESSAGE

logger = logging.getLogger(__name__)

# Seconds the tool waits for the reply. Under Vercel's 300 s request limit, with room for the access
# middleware before and after the call; above the reply's own 250 s budget, so it is only a backstop.
ASK_LIMIT_SECONDS = 270

# What the web chat sends for each agent (birdy-beta src/components/agency/sections.js): its page and chat.
PAGES = {team_defs.MEDIA_BUYER: "campaigns", team_defs.SALES_COACH: "call_center"}
CHAT_PATHS = {team_defs.MEDIA_BUYER: "/media-buyer/chat", team_defs.SALES_COACH: "/sales-coach/chat"}
TOOL_FOR = {team_defs.MEDIA_BUYER: "ask_media_buyer", team_defs.SALES_COACH: "ask_sales_coach"}

QUESTION_QUOTE = 80
_RUNNING: set = set()   # strong references to replies still running after the tool stopped waiting


def chat_link(agent: str, session_id: str | None, client_group_id: str | None = None) -> str:
    """The relative Birdy address of the agent's chat, opened on this conversation (as the app builds it)."""
    query = {}
    if client_group_id:
        query["client"] = client_group_id
    if session_id:
        query["c"] = session_id
    return CHAT_PATHS[agent] + (f"?{urlencode(query)}" if query else "")


def _num(value) -> str:
    return f"{round(float(value or 0), 3):g}"


def _tools_text(tools: list) -> str:
    if not tools:
        return "no tools"
    names = []
    for t in tools:
        label = t.get("label") or t.get("name") or "a tool"
        names.append(f"{label} ({t['detail']})" if t.get("detail") else label)
    return ", ".join(names)


def format_trail(route: dict | None) -> str:
    """The route as a few plain lines: one per agent that worked on the reply, then the total."""
    if not route or not route.get("steps"):
        return ""
    names = {s.get("agent"): s.get("name") for s in route["steps"]}
    lines = ["Route:"]
    for i, s in enumerate(route["steps"], 1):
        who = s.get("name") or s.get("agent")
        if s.get("asked_by"):
            who += f", asked by {names.get(s['asked_by']) or team_defs.NAMES.get(s['asked_by'], s['asked_by'])}"
            if s.get("question"):
                who += f' ("{team_route.clip(s["question"], QUESTION_QUOTE)}")'
        else:
            who += ", asked by you"
        line = f"{i}. {who} · used {_tools_text(s.get('tools') or [])} · {_num(s.get('seconds'))} s · {_num(s.get('credits'))} credits"
        if s.get("status") not in (None, "done"):
            line += f" · {s['status']}"
        lines.append(line)
    total = f"Total: {_num(route.get('seconds'))} s · {_num(route.get('credits'))} credits"
    if route.get("limit_hit"):
        total += f" · stopped early ({route['limit_hit']} limit)"
    lines.append(total)
    return "\n".join(lines)


def format_proposals(proposals: list | None, url: str) -> str:
    """Each proposal with what it would change, as waiting for the owner. Listed only, never decided here."""
    if not proposals:
        return ""
    lines = [f"Proposals waiting for the owner's approval in Birdy. Nothing has changed: only the owner can "
             f"approve or decline them, at {url}"]
    for i, p in enumerate(proposals, 1):
        owner = team_defs.NAMES.get(p.get("agent"), p.get("agent") or "the owning agent")
        change = (p.get("detail") or "").strip() or f"kind {str(p.get('kind') or 'other').replace('_', ' ')}, no details given"
        lines.append(f"{i}. {p.get('title') or 'Untitled proposal'}: {change} ({owner} would make it once approved)")
    return "\n".join(lines)


def format_result(agent: str, result: dict, url: str) -> str:
    """The text an ask returns: the answer, the route trail, the proposals, how to continue."""
    parts = [(result.get("reply") or "").strip() or f"{team_defs.NAMES[agent]} gave no answer."]
    for block in (format_trail(result.get("route")), format_proposals(result.get("proposals"), url)):
        if block:
            parts.append(block)
    if result.get("session_id"):
        parts.append(f"Conversation id: {result['session_id']} (pass it as conversation_id to {TOOL_FOR[agent]} "
                     "to continue this conversation)")
    return "\n\n".join(parts)


async def _conversation_scope(db, user_id: str, agent: str, conversation_id: str) -> str | None:
    """The one client an earlier conversation with this agent was about (None for none or several).
    Refuses an id that is not this user's conversation with this agent."""
    rows = await db["ai_conversation_log"].find(
        {"session_id": conversation_id, "user_id": user_id, "source": "birdy"},
        {"agent": 1, "client_group_id": 1, "_id": 0},
    ).to_list(length=None)
    if not rows:
        raise ToolError(f"No conversation {conversation_id} was found. Leave conversation_id out to start a new one.")
    had = next((r.get("agent") for r in rows if r.get("agent")), None)
    if had != agent:
        other = TOOL_FOR.get(had)
        hint = f" It is with {team_defs.NAMES[had]}: use {other}." if other else ""
        raise ToolError(f"Conversation {conversation_id} is not a chat with {team_defs.NAMES[agent]}.{hint}")
    groups = {r.get("client_group_id") for r in rows if r.get("client_group_id")}
    return next(iter(groups)) if len(groups) == 1 else None


def _keep(task: asyncio.Task) -> None:
    _RUNNING.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("MCP ask reply failed after the tool stopped waiting: %r", task.exception())


async def ask_agent(agent: str, question: str, client: str | None = None,
                    conversation_id: str | None = None) -> str:
    """One reply of `agent`'s own chat for the token's user, as text (see the module docstring)."""
    name = team_defs.NAMES[agent]
    question = (question or "").strip()
    if not question:
        raise ToolError(f"Give the question to ask {name}.")
    token = _server.get_access_token()
    if token is not None and token.claims.get("type") == "access":
        # Birdy's own chats ask through their own ask tools (ai/team/delegation.py), never this one.
        raise ToolError(f"{TOOL_FOR[agent]} is for assistants outside Birdy. In Birdy, talk to {name} in its chat.")
    user_id = _current_user_id()
    mongo_client = get_shared_mongo_client()
    db = get_db(mongo_client)
    conversation_id = (conversation_id or "").strip() or None

    # Continuing: the conversation must be this user's chat with this agent; it keeps its one client.
    earlier_gid = await _conversation_scope(db, user_id, agent, conversation_id) if conversation_id else None
    group = None
    if client and str(client).strip():
        group = await resolve_client(db, user_id, client)
    elif earlier_gid:
        group = await db["client_groups"].find_one({"user_id": user_id, "id": earlier_gid})

    from core.models import ChatRequest
    from ai.provider_factory import NoAiCredentialError
    from routers import chat as chat_router   # lazy: routers.chat imports the orchestrator, which imports ai.mcp

    request = ChatRequest(message=question, agent=agent, page=PAGES[agent], session_id=conversation_id,
                          client_group_id=(group or {}).get("id"), client_name=(group or {}).get("name"))
    # Its own task, so a reply that outlives the wait below still finishes: saved, archived and billed once.
    task = asyncio.ensure_future(chat_router.run_chat_reply(db, mongo_client, user_id, request))
    _RUNNING.add(task)
    task.add_done_callback(_keep)
    try:
        result = await asyncio.wait_for(asyncio.shield(task), ASK_LIMIT_SECONDS)
    except asyncio.TimeoutError:
        raise ToolError(
            f"{name} did not finish within {ASK_LIMIT_SECONDS} seconds, so there is no answer here. If it "
            f"finishes, the answer will be in {name}'s chat in Birdy: "
            f"{FRONTEND_URL}{CHAT_PATHS[agent]}. Try a narrower question (one client, one period).")
    except HTTPException as e:
        if e.status_code == 402:
            raise ToolError(OUT_OF_CREDITS_MESSAGE)
        raise ToolError(f"{name} could not take this question ({e.detail}). Nothing was changed.")
    except NoAiCredentialError:
        raise ToolError("Birdy has no AI key set up for this account, so the agents cannot answer. Add one in "
                        f"Birdy's settings: {FRONTEND_URL}/settings. Nothing was charged.")
    except Exception:
        logger.error("MCP ask %s failed", agent, exc_info=True)
        raise ToolError(f"{name} could not answer this time. Try again in a moment.")

    link = chat_link(agent, result.get("session_id"), request.client_group_id)
    credits = (result.get("route") or {}).get("credits") or 0.0
    set_outcome(CHAT_BILLED, credits=float(credits), link=link)
    return format_result(agent, result, f"{FRONTEND_URL}{link}")


_WHEN = (
    "Use an ask for judgement: a diagnosis, \"why\", \"what should we do\", a plan, or anything that should draw "
    "on the agency's saved lessons, context cards and the agent's sub-agents. For plain numbers use the direct "
    "data tools instead: they are quick and much cheaper. Each ask is a full chat reply in Birdy: it can take a "
    "few minutes, it costs credits like a chat reply there, and the conversation shows in the agent's chat in "
    "Birdy.\n\n"
    "The result is the agent's answer, then a short route trail (who worked on it, tools used, seconds, "
    "credits), then any proposals it made. Proposals wait for the owner's approval in Birdy: tell the user, "
    "with the link, and never say they are done; this tool cannot approve or decline them. If the agent shows "
    "a preview or asks to confirm a change, show it to the user and confirm (by asking again with the "
    "conversation id) only after the user explicitly says yes. A client name that matches several clients "
    "returns the candidates and nothing is asked.\n\n"
    "Args:\n"
    "    question: The question, in the user's own words, with the period if they gave one.\n"
    "    client: Optional. The client's id or name (a unique part of it is enough); leave it out for the whole "
    "agency. When continuing a conversation it defaults to that conversation's client.\n"
    "    conversation_id: Optional. The conversation id from an earlier result of this tool, to continue it."
)

ASK_MEDIA_BUYER = (
    "Ask Media Buyer, Birdy's ads agent, and get its considered answer: Meta campaigns, ad sets, ads, spend, "
    "cost per lead, creatives and creative tests. It pulls the figures itself and can ask Birdy about leads "
    "and calls.\n\n" + _WHEN
)
ASK_SALES_COACH = (
    "Ask Sales Coach, Birdy's call-centre agent, and get its considered answer: call centre stats, connect and "
    "booking rates, talk time, call recordings and how the callers are doing. It pulls the figures itself and "
    "can ask Birdy about ads and the pipeline.\n\n" + _WHEN
)


@mcp.tool(description=ASK_MEDIA_BUYER, output_schema=None, **WRITE)
async def ask_media_buyer(question: str, client: str | None = None, conversation_id: str | None = None) -> str:
    return await ask_agent(team_defs.MEDIA_BUYER, question, client, conversation_id)


@mcp.tool(description=ASK_SALES_COACH, output_schema=None, **WRITE)
async def ask_sales_coach(question: str, client: str | None = None, conversation_id: str | None = None) -> str:
    return await ask_agent(team_defs.SALES_COACH, question, client, conversation_id)
