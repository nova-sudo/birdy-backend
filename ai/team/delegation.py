"""
ai/team/delegation.py
---------------------
Agents asking agents, hub and spoke (design section 4):

- Birdy asks a main agent:        ask_media_buyer / ask_sales_coach
- A main agent passes a need up:  ask_birdy
- A main agent asks its own sub:  ask_sub_agent
- Main agents never ask each other; a sub-agent asks no one.

Each ask runs a nested, bounded `run_chat` for the child with the child's own
instructions, tools and memory (ai/team/definitions.py), in the client scope
the ask carries. The child can only read and propose (`propose_action`); its
answer comes back to the asking agent as the tool result.

Limits per user reply (route.py): 3 asks, chain depth 3, and an agent already
involved in the reply is never asked again. A hit sets the route's `limit_hit`
and the asking agent is told to answer with what it has.
"""

import asyncio
import json
import logging
import time
import uuid

from ai.team import definitions as defs
from ai.team import route as rt
from ai.team.sub_agents import list_sub_agents

logger = logging.getLogger(__name__)

ASK_MEDIA_BUYER = "ask_media_buyer"
ASK_SALES_COACH = "ask_sales_coach"
ASK_BIRDY = "ask_birdy"
ASK_SUB_AGENT = "ask_sub_agent"
PROPOSE_ACTION = "propose_action"

ASK_TOOLS = {ASK_MEDIA_BUYER: defs.MEDIA_BUYER, ASK_SALES_COACH: defs.SALES_COACH, ASK_BIRDY: defs.BIRDY}
PROPOSE_SUB_AGENT = "propose_sub_agent"
TOOL_NAMES = frozenset({*ASK_TOOLS, ASK_SUB_AGENT, PROPOSE_ACTION, PROPOSE_SUB_AGENT})

AGENCY = "agency"
ANSWER_MAX_CHARS = 4000      # how much of a child's answer goes back into the parent's context
QUESTION_MAX_CHARS = 1500


def _ask_schema(name: str, who: str, what: str, extra_props: dict | None = None, extra_req=()) -> dict:
    props = {
        "question": {"type": "string", "description": "One clear question, with the period and what you need back."},
        "client": {"type": "string", "description": (
            "The client this is about, as its group ID from get_client_groups, or \"agency\" for the "
            "whole agency. In a one-client conversation the server uses that client whatever you pass.")},
        **(extra_props or {}),
    }
    return {"type": "function", "function": {
        "name": name,
        "description": f"Ask {who} and get its answer back in this reply. {what} It can only read and propose; "
                       "it changes nothing. Asks per reply are limited, so ask once, with everything you need.",
        "parameters": {"type": "object", "properties": props, "required": ["question", "client", *extra_req]},
    }}


_SCHEMAS = {
    ASK_MEDIA_BUYER: _ask_schema(ASK_MEDIA_BUYER, "Media Buyer",
                                 "It owns the ads: campaigns, ad sets, ads, spend, CPL, creative tests."),
    ASK_SALES_COACH: _ask_schema(ASK_SALES_COACH, "Sales Coach",
                                 "It owns the call centre: call stats, call recordings, what happens on calls."),
    ASK_BIRDY: _ask_schema(ASK_BIRDY, "Birdy, the orchestrator",
                           "Use it for anything outside your area; Birdy asks the right agent."),
}


def _sub_schema(subs: list) -> dict:
    listing = "; ".join(f"{s.id}: {s.name}" for s in subs)
    return _ask_schema(ASK_SUB_AGENT, "one of your sub-agents", f"Your sub-agents: {listing}.",
                       {"sub_agent_id": {"type": "string", "enum": [s.id for s in subs],
                                         "description": "Which sub-agent to ask."}},
                       ("sub_agent_id",))


def _propose_schema(owner: str) -> dict:
    kinds = [*defs.write_tools(owner), "other"]
    return {"type": "function", "function": {
        "name": PROPOSE_ACTION,
        "description": ("Propose one change for the owner to approve later. Nothing changes now. On approval "
                        f"{defs.NAMES[owner]} makes it through its own checks."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short title, e.g. 'Pause ad 'Spring promo' in Aura'."},
            "detail": {"type": "string", "description": "Why, with the figures behind it."},
            "kind": {"type": "string", "enum": kinds,
                     "description": "The tool that would make the change, or 'other'."},
            "payload": {"type": "object", "description": "The arguments that tool would need, as far as you know them."},
        }, "required": ["title", "detail", "kind"]},
    }}


def owner_of(frame: rt.Frame) -> str | None:
    """The main agent a frame's proposals belong to (Birdy owns no writes)."""
    if frame.agent in defs.MAIN_AGENTS:
        return frame.agent
    return frame.parent_main


async def subs_for(db, frame: rt.Frame, user_id: str) -> list:
    """A main agent's callable sub-agents (none for Birdy or a sub-agent, or when the chain is full)."""
    if frame.agent not in defs.MAIN_AGENTS or frame.depth + 1 > rt.MAX_DEPTH:
        return []
    return await list_sub_agents(db, frame.agent, user_id)


def tool_schemas(frame: rt.Frame, subs: list) -> list[dict]:
    """The delegation tools this run is offered. Never an ask to an agent already in the chain."""
    out = []
    if frame.agent == defs.BIRDY:
        for tool, target in ((ASK_MEDIA_BUYER, defs.MEDIA_BUYER), (ASK_SALES_COACH, defs.SALES_COACH)):
            if target not in frame.chain:
                out.append(_SCHEMAS[tool])
    elif frame.agent in defs.MAIN_AGENTS:
        if defs.BIRDY not in frame.chain:
            out.append(_SCHEMAS[ASK_BIRDY])
        if subs:
            out.append(_sub_schema(subs))
        if not frame.nested:
            # Only in the main agent's own chat: the owner is there to approve the card.
            from ai.agents.sub_agents import propose_tool
            out.append(propose_tool.tool_schema(frame.agent))
    if frame.nested:
        owner = owner_of(frame)
        if owner:
            out.append(_propose_schema(owner))
    return out


def _result(ok: bool, **kw) -> str:
    return json.dumps({"ok": ok, **kw}, default=str)


async def _resolve_client(db, user_id: str, frame: rt.Frame, client) -> tuple[bool, str | None, str | None, str]:
    """(ok, client_group_id or None for agency, client name, note). The child inherits a
    client-scoped parent's client whatever the model passed."""
    if frame.client_group_id:
        note = ""
        if client and client not in (frame.client_group_id, AGENCY):
            note = "This conversation is about one client; the ask was kept to that client."
        return True, frame.client_group_id, frame.client_name, note
    if not client or str(client).strip().lower() == AGENCY:
        return True, None, None, ""
    gid = str(client).strip()
    group = await db["client_groups"].find_one({"user_id": user_id, "id": gid}, {"name": 1})
    if not group:
        return False, None, None, (f"'{gid}' is not one of the owner's client group IDs. Call get_client_groups "
                                   "and pass the client's id, or \"agency\".")
    return True, gid, group.get("name"), ""


def _target(name: str, args: dict, subs: list):
    """(agent key, display name, SubAgent or None) for an ask tool call, or None."""
    if name in ASK_TOOLS:
        key = ASK_TOOLS[name]
        return key, defs.NAMES[key], None
    if name == ASK_SUB_AGENT:
        sub = next((s for s in subs if s.id == str(args.get("sub_agent_id") or "")), None)
        if sub:
            return sub.key, sub.name, sub
    return None


def _allowed_edge(frame: rt.Frame, target: str) -> bool:
    """Hub and spoke: Birdy -> main, main -> Birdy, main -> its own sub. Nothing else."""
    if frame.agent == defs.BIRDY:
        return target in defs.MAIN_AGENTS
    if frame.agent in defs.MAIN_AGENTS:
        return target == defs.BIRDY or target.startswith("sub:")
    return False


async def handle(name: str, args: dict, frame: rt.Frame, ctx: dict) -> str:
    """Run one delegation tool call and return its result for the asking agent."""
    if name == PROPOSE_ACTION:
        return _propose(args, frame)
    if name == PROPOSE_SUB_AGENT:
        return await _propose_sub_agent(args, frame, ctx)

    turn = frame.turn
    subs = ctx.get("subs") or []
    target = _target(name, args, subs)
    if target is None:
        return _result(False, error="Unknown agent. Use one of the ask tools you were given.")
    key, display, sub = target
    if not _allowed_edge(frame, key):
        return _result(False, error=f"You can't ask {display} directly. Agents only ask Birdy or their own sub-agents.")

    question = str(args.get("question") or "").strip()
    if not question:
        return _result(False, error="Pass the question you want answered.")
    question = question[:QUESTION_MAX_CHARS]

    db, user_id = ctx["db"], ctx["user_id"]
    ok, gid, gname, scope_note = await _resolve_client(db, user_id, frame, args.get("client"))
    if not ok:
        return _result(False, error=scope_note)
    if sub and sub.client_group_ids and gid not in sub.client_group_ids:
        return _result(False, error=f"{sub.name} only covers some clients; this question is outside them.")

    # ── limits ──
    earlier = turn.step_for(key)
    if key in frame.chain or earlier is not None:
        turn.involved = True
        turn.hit(rt.LIMIT_REPEAT)
        turn.changed()
        return _result(False, limit_hit=rt.LIMIT_REPEAT, agent=display,
                       earlier_answer=(earlier or {}).get("answer"),
                       note=(f"{display} was already asked in this reply and is not asked again. Use its earlier "
                             "answer if there is one, and answer with what you know."))
    if frame.depth + 1 > rt.MAX_DEPTH:
        turn.involved = True
        turn.hit(rt.LIMIT_DEPTH)
        _skipped(turn, key, display, question, frame.agent, "Not asked: chain too long")
        return _result(False, limit_hit=rt.LIMIT_DEPTH,
                       note="The chain of agents is as long as it can get. Answer with what you know and say what is missing.")
    if turn.asks >= rt.MAX_ASKS_PER_REPLY:
        turn.involved = True
        turn.hit(rt.LIMIT_ASKS)
        _skipped(turn, key, display, question, frame.agent, f"Not asked: limit of {rt.MAX_ASKS_PER_REPLY} asks")
        return _result(False, limit_hit=rt.LIMIT_ASKS,
                       note=(f"This reply already used its {rt.MAX_ASKS_PER_REPLY} agent asks. Answer with what you "
                             "know and tell the owner what you could not check."))

    timeout = turn.ask_timeout()
    turn.involved = True
    turn.asks += 1
    step = turn.add_step(key, display, question=question, asked_by=frame.agent)
    if timeout <= 0:
        step.update(status="failed", summary="Not asked: no time left in this reply")
        turn.changed()
        return _result(False, error="timeout",
                       note="There is no time left in this reply to ask. Answer with what you know.")

    child_usage = rt.Usage(agent=key, depth=frame.depth + 1, model=frame.usage.model)
    turn.usages.append(child_usage)
    child = rt.Frame(turn=turn, agent=key, depth=frame.depth + 1, chain=(*frame.chain, key), step=step,
                     usage=child_usage, client_group_id=gid, client_name=gname, sub=sub,
                     parent_main=sub.parent if sub else None)
    turn.changed()

    started = time.monotonic()
    proposals_before = len(turn.proposals)
    task = asyncio.create_task(_run_child(child, question, ctx))
    out = None
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            out = task.result()
        else:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except BaseException:
                pass
            step.update(status="failed", summary="Ran out of time")
    except Exception:
        logger.exception(f"ask {key} failed")
        step.update(status="failed", summary="Could not finish")
    step["seconds"] = round(time.monotonic() - started, 1)
    step["credits"] = turn.credits_for(child_usage)

    new_props = [{"id": p["id"], "title": p["title"]} for p in turn.proposals[proposals_before:]]
    if out is None:
        turn.changed()
        note = (f"{display} did not finish in time. Answer with what you already know and say this part "
                "could not be checked." if step["summary"] == "Ran out of time" else
                f"{display} could not answer. Answer with what you know.")
        return _result(False, error="timeout" if step["summary"] == "Ran out of time" else "failed",
                       agent=display, note=note, proposals=new_props)

    answer, summary = rt.split_summary(out.get("reply") or "")
    step.update(status="done", answer=answer, summary=summary or rt.summarize(answer) or "Answered")
    turn.changed()
    return _result(True, agent=display, answer=rt.clip(answer, ANSWER_MAX_CHARS), proposals=new_props,
                   **({"note": scope_note} if scope_note else {}))


def _skipped(turn: rt.Turn, key: str, display: str, question: str, asked_by: str, summary: str) -> None:
    if turn.step_for(key) is None:
        step = turn.add_step(key, display, question=question, asked_by=asked_by, status="skipped")
        step["summary"] = summary
    turn.changed()


async def _propose_sub_agent(args: dict, frame: rt.Frame, ctx: dict) -> str:
    """A main agent proposes a sub-agent in its own chat. Stored as an inert proposal; the owner
    approves or rejects it from the card (POST /api/agents/sub/{id}/approve | /reject)."""
    if frame.nested or frame.agent not in defs.MAIN_AGENTS:
        return _result(False, error="Only a main agent can propose a sub-agent, in its own chat.")
    from ai.agents.sub_agents import propose_tool
    args = {k: v for k, v in (args or {}).items() if k != "parent"}
    res = await propose_tool.execute(ctx["db"], ctx["user_id"], frame.agent, **args)
    if not res.get("ok"):
        return json.dumps(res)
    sub = res["sub_agent"]
    frame.turn.proposals.append({
        "id": "sub_" + str(sub["id"]),
        "agent": frame.agent,
        "title": f"New sub-agent: {sub['name']}",
        "detail": sub.get("purpose") or "",
        "kind": "sub_agent",
        "payload": {"sub_agent": sub, "approval": res["approval"]},
        "status": "pending",
        "proposed_by": frame.agent,
        "client_group_id": frame.client_group_id,
    })
    frame.turn.changed()
    return json.dumps({"ok": True, "id": sub["id"], "note": res["note"]})


def _propose(args: dict, frame: rt.Frame) -> str:
    owner = owner_of(frame)
    if not frame.nested or owner is None:
        return _result(False, error="propose_action is only for agents asked by another agent.")
    kind = str(args.get("kind") or "other")
    if kind not in defs.write_tools(owner):
        kind = "other"
    title = rt.clip(str(args.get("title") or "").strip(), 120)
    if not title:
        return _result(False, error="Give the proposal a short title.")
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    proposal = {
        "id": "p_" + uuid.uuid4().hex[:12],
        "agent": owner,
        "title": title,
        "detail": rt.clip(str(args.get("detail") or "").strip(), 1000),
        "kind": kind,
        "payload": payload,
        "status": "pending",
        # additions beyond the contract
        "proposed_by": frame.agent,
        "client_group_id": frame.client_group_id,
    }
    frame.turn.proposals.append(proposal)
    frame.turn.changed()
    return _result(True, id=proposal["id"], note=("Saved as a proposal. Nothing has changed; the owner approves it "
                                                  "later. Mention it in your answer as a proposal."))


async def _run_child(child: rt.Frame, question: str, ctx: dict) -> dict:
    """The child's nested run, in its own task, with the request-scoped flags reset for it."""
    from ai.orchestrator import run_chat
    from ai.tools.agent_tools import current_training_agent
    from ai.tools.stock_card_tools import current_card_read_only, current_stock_card
    from ai.tools.stock_lesson_tools import current_lessons_agent

    lessons_agent = None
    if child.agent == defs.MEDIA_BUYER or child.parent_main == defs.MEDIA_BUYER:
        lessons_agent = await defs.stock_agent_id(ctx["db"], ctx["user_id"])
    # Not inherited from the request: no card, read-only, no training/setup agent.
    current_stock_card.set(None)
    current_card_read_only.set(True)
    current_training_agent.set(None)
    current_lessons_agent.set(lessons_agent)
    return await run_chat(
        provider=ctx["provider"], tool_registry=ctx["tool_registry"], db=ctx["db"], user_id=ctx["user_id"],
        message=question, session_id=ctx.get("session_id"), page=None, mongo_client=ctx.get("mongo_client"),
        client_group_id=child.client_group_id, client_name=child.client_name, source=ctx.get("source"),
        agent=child.agent, _frame=child,
    )
