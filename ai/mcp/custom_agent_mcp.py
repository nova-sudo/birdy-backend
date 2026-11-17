"""
ai/mcp/custom_agent_mcp.py
---------------------------
Tools for the owner's custom agents (the ones built in Birdy's agent setup): what an agent has learned, found and
proposes (read), the owner's decisions on it (write), and switching it on or undoing what it ran by itself (live,
two-step). They wrap the same logic as the routes in routers/agents.py (ai/agents/custom_ops.py) and the chat
tools in ai/tools/agent_tools.py. A custom agent is chosen with `agent` (id or name); the Birdy Media Buying
Agent has its own tools (ai/mcp/stock_agent_mcp.py) and is refused here with a hint.
"""

import time
from contextlib import contextmanager
from datetime import timedelta
from typing import Literal

from fastmcp.exceptions import ToolError

from ai.agents import confidence as agent_confidence
from ai.agents import custom_ops, learning, outcomes, replay
from ai.agents import store as agent_store
from ai.agents.defaults import RULE_KINDS
from ai.mcp.access import LIVE, READ, WRITE
from ai.mcp.agent_mcp import resolve_agent
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import agent_tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client

SOURCE = "mcp"
ACTIVITY_MAX = 200

_FRIENDLY = {
    "bad_time": "That isn't a time of day. Use something like 07:00, 7am or 7:30 pm.",
    "bad_tz": "That isn't a valid timezone. Use an IANA name such as Europe/London.",
    "unknown_channel": "No Slack channel matches that. Use a channel id or name Birdy can see.",
    "not_found": "Custom agent not found",
}


async def _custom(agent: str | None):
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    return db, user_id, await resolve_agent(db, user_id, agent, kind="custom")


@contextmanager
def _acting_on(agent_id: str):
    """ai/tools/agent_tools act on the agent of the current chat; set that for one call."""
    token = agent_tools.current_training_agent.set(agent_id)
    try:
        yield
    finally:
        agent_tools.current_training_agent.reset(token)


def _failure(res: dict) -> ToolError:
    msg = res.get("detail") or _FRIENDLY.get(res.get("error")) or res.get("error") or "That didn't work"
    if res.get("candidates"):
        msg += f" Candidates: {', '.join(res['candidates'])}."
    return ToolError(msg)


def _unwrap(res: dict, summary: str) -> dict:
    """A chat-tool result: raise when it refused, else drop the chat-only UI hints and add a summary."""
    if not res.get("ok"):
        raise _failure(res)
    return {**{k: v for k, v in res.items() if k not in ("ui_block", "playbook")}, "summary": summary}


def _refused(e: custom_ops.OpError) -> dict:
    """A route's refusal as data: a 404 is a plain error, everything else (409, 410, ...) is returned with the
    route's message and its extras (missing steps, conflicts) so the caller can say what to do."""
    if e.status == 404:
        raise ToolError(e.body.get("detail") or "Not found")
    extra = {k: v for k, v in e.body.items() if k != "detail"}
    return {"ok": False, "error": e.body.get("detail"), "status": e.status, **extra}


def _agent_view(agent: dict) -> dict:
    return {"id": agent["_id"], "name": agent.get("name"), "status": agent.get("status"),
            "scope": agent.get("scope") or {}, "turned_off_at": agent.get("turned_off_at"),
            "confidence": agent.get("confidence") or {}}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@mcp.tool(**READ)
async def get_agent_playbook(agent: str | None = None) -> dict:
    """A custom agent's playbook: each rule's params, mode and per-client overrides, each client's CPL target and currency, and which setup steps are done.

    Use it before changing a rule or target so the change is based on what is set now.

    Args:
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.get_playbook(db, user_id)
    if not res.get("ok"):
        raise _failure(res)
    return {k: v for k, v in res.items() if k != "ok"}


@mcp.tool(**READ)
async def get_agent_activity(limit: int = 25, cursor: str | None = None, agent: str | None = None) -> dict:
    """What a custom agent's owner decided on its work items, newest first: approved, denied and auto-run actions, with whether each can still be undone.

    Returns `items` and a `next_cursor` (pass it back as cursor for older ones). `can_undo` items can be reversed with undo_auto_action.

    Args:
        limit: How many items (1-200, default 25).
        cursor: The previous page's next_cursor.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, _, found = await _custom(agent)
    after = None
    if cursor:
        try:
            after = outcomes.decode_cursor(cursor)
        except ValueError:
            raise ToolError("cursor is not valid. Use the next_cursor from the previous page.")
    return await outcomes.list_activity(db, found, limit=max(1, min(limit, ACTIVITY_MAX)), cursor=after)


@mcp.tool(**READ)
async def get_agent_history(agent: str | None = None) -> dict:
    """How far each watched client's Meta ad history has been pulled for a custom agent. A client is "ready" once the agent may read it.

    Args:
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    return await custom_ops.history_view(db, user_id, found)


@mcp.tool(**READ)
async def get_agent_claims(agent: str | None = None) -> dict:
    """Who acts on each client a custom agent watches: this agent, another agent, or nobody.

    Use it before activate_agent to see which clients another agent already holds.

    Args:
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    return await custom_ops.claims_view(db, user_id, found)


@mcp.tool(**READ)
async def get_agent_impact(month: str | None = None, agent: str | None = None) -> dict:
    """A custom agent's impact for one month: what it paused or changed, the spend it saved and how that moved.

    Args:
        month: The month as "YYYY-MM" (the agent's local calendar month). Defaults to this month.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    if month is not None and not outcomes.MONTH_RE.match(month):
        raise ToolError("month must look like 2026-10 (YYYY-MM)")
    db, _, found = await _custom(agent)
    return await outcomes.compute_impact(db, found, month=month or outcomes.current_month(found))


@mcp.tool(**READ)
async def get_agent_confidence(agent: str | None = None) -> dict:
    """How ready a custom agent is to be switched on: its score, whether it is `ready`, and what is `missing`.

    Args:
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, _, found = await _custom(agent)
    return await custom_ops.confidence(db, found)


@mcp.tool(**READ)
async def get_agent_proposals(status: Literal["open", "accepted", "rejected", "all"] = "open",
                              agent: str | None = None) -> list[dict]:
    """The rule changes (and "make it a rule?" suggestions) a custom agent proposes from what the owner denied, newest first.

    "open" is what waits on the owner; use accept_proposal or reject_proposal on a proposal's id.

    Args:
        status: open (default), accepted, rejected or all.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, _, found = await _custom(agent)
    return await custom_ops.list_proposals(db, found["_id"], status)


@mcp.tool(**READ)
async def get_agent_memory(agent: str | None = None) -> list[dict]:
    """Everything a custom agent remembers: general facts and exceptions (with their client, ad name and weekday filters), oldest first.

    Args:
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, _, found = await _custom(agent)
    return await custom_ops.list_memory(db, found["_id"])


@mcp.tool(**READ)
async def get_agent_offers(agent: str | None = None) -> list[dict]:
    """Offers waiting on the owner: rules the agent has earned the right to run on its own (answer with graduate_rule), and notices that it slowed itself down.

    Args:
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, _, found = await _custom(agent)
    return await learning.pending_offers(db, found)


@mcp.tool(**READ)
async def get_dry_run(job_id: str, agent: str | None = None) -> dict:
    """Poll a dry run started with start_dry_run: its status, and its result (what the agent would have done over the last 30 days) once done.

    Args:
        job_id: The job_id start_dry_run returned.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    try:
        return await custom_ops.get_job(db, user_id, found["_id"], job_id)
    except custom_ops.OpError as e:
        raise ToolError(e.body["detail"])


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

@mcp.tool(**WRITE)
async def accept_proposal(proposal_id: str, agent: str | None = None) -> dict:
    """Say yes to a proposal: a rule change is applied to the rule (a new rule version); a "make it a rule?" suggestion is saved as a lesson.

    A proposal already decided, or out of date because the rule changed since, returns `ok: false` with the reason and changes nothing.

    Args:
        proposal_id: The proposal id from get_agent_proposals.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    try:
        res = await custom_ops.accept_proposal(db, found, proposal_id, by=user_id)
    except custom_ops.OpError as e:
        return _refused(e)
    said = "Saved it as a lesson." if res.get("lesson") else "Applied the change to the rule."
    return {"ok": True, **res, "summary": said}


@mcp.tool(**WRITE)
async def reject_proposal(proposal_id: str, agent: str | None = None) -> dict:
    """Say no to a proposal. The agent won't suggest it again for 30 days, or until the owner has denied twice as many.

    A proposal already decided returns `ok: false` with the reason.

    Args:
        proposal_id: The proposal id from get_agent_proposals.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, _, found = await _custom(agent)
    try:
        res = await custom_ops.reject_proposal(db, found, proposal_id)
    except custom_ops.OpError as e:
        return _refused(e)
    return {"ok": True, **res, "summary": "Rejected the proposal."}


@mcp.tool(**WRITE)
async def set_rule_param(rule_kind: Literal[RULE_KINDS], param: str, value: float, agent: str | None = None) -> dict:
    """Change one number of a rule for every client (for example pause_over_target cpl_multiple to 2). get_agent_playbook lists the params.

    Only call it once the owner has said what value they want.

    Args:
        rule_kind: pause_over_target, revive_winner, flag_no_spend or flag_declining.
        param: A param name from get_agent_playbook for that rule.
        value: The new value, zero or more.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.set_rule_param(db, user_id, rule_kind=rule_kind, param=param, value=value)
    ch = res.get("changed") or {}
    return _unwrap(res, f"Changed {param} of {rule_kind} from {ch.get('before')} to {ch.get('after')}.")


@mcp.tool(**WRITE)
async def set_client_target_for_agent(client_name: str, cpl: float, agent: str | None = None) -> dict:
    """Set a client's cost-per-lead target, in that client's own currency. The target belongs to the client, so it applies to every agent.

    A name that matches several clients returns the candidates and changes nothing.

    Args:
        client_name: The client's name (a unique part of it is enough).
        cpl: The target, zero or more.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.set_client_target(db, user_id, client_name=client_name, cpl=cpl)
    ch = res.get("changed") or {}
    return _unwrap(res, f"Set {ch.get('client')}'s CPL target to {cpl}.")


@mcp.tool(**WRITE)
async def set_client_override(client_name: str, rule_kind: Literal[RULE_KINDS], param: str, value: float,
                              agent: str | None = None) -> dict:
    """Override one rule param for a single client only (for example a looser cpl_multiple for one client).

    A name that matches several clients returns the candidates and changes nothing.

    Args:
        client_name: The client's name (a unique part of it is enough).
        rule_kind: pause_over_target, revive_winner, flag_no_spend or flag_declining.
        param: A param name from get_agent_playbook for that rule.
        value: The new value for this client.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.set_client_override(db, user_id, client_name=client_name, rule_kind=rule_kind,
                                                    param=param, value=value)
    ch = res.get("changed") or {}
    return _unwrap(res, f"For {ch.get('client')}, {param} of {rule_kind} is now {value}.")


@mcp.tool(**WRITE)
async def add_agent_memory(text: str, agent: str | None = None) -> dict:
    """Teach a custom agent a general fact or preference about how to run the owner's ads, in the owner's words.

    For something that applies to only some ads (one client, an ad name, certain weekdays) use add_agent_exception.

    Args:
        text: What the agent should remember.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.add_memory(db, user_id, text=text, source=SOURCE)
    return _unwrap(res, "The agent will remember that.")


@mcp.tool(**WRITE)
async def add_agent_exception(text: str, client_name: str | None = None, ad_name_contains: str | None = None,
                              weekdays: list[int] | None = None, agent: str | None = None) -> dict:
    """Teach a custom agent an exception that applies only to some ads. At least one of client_name, ad_name_contains or weekdays is required.

    A client name that matches several clients returns the candidates and changes nothing.

    Args:
        text: The exception, in the owner's words (for example "never pause brand ads").
        client_name: Limit it to one client.
        ad_name_contains: Limit it to ads whose name contains this text (case-insensitive).
        weekdays: Limit it to these weekdays, Monday = 0 to Sunday = 6.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.add_exception(db, user_id, text=text, client_name=client_name,
                                              ad_name_contains=ad_name_contains, weekdays=weekdays, source=SOURCE)
    return _unwrap(res, "The agent will remember that exception.")


@mcp.tool(**WRITE)
async def set_agent_schedule(time: str, tz: str | None = None, agent: str | None = None) -> dict:
    """Set the daily time a custom agent runs and reports.

    Args:
        time: A time of day such as "07:00", "7am" or "7:30 pm".
        tz: Optional IANA timezone such as Europe/London. Leave out to keep the agent's (or the owner's).
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.set_schedule(db, user_id, time=time, tz=tz)
    sched = res.get("schedule") or {}
    return _unwrap(res, f"The agent will run daily at {sched.get('time')} ({sched.get('tz')}).")


@mcp.tool(**WRITE)
async def set_agent_slack_channel(channel_id: str, agent: str | None = None) -> dict:
    """Choose the Slack channel a custom agent posts to, by channel id or name. The channel must be one Birdy's Slack app can see.

    Args:
        channel_id: The Slack channel id or name.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    with _acting_on(found["_id"]):
        res = await agent_tools.set_slack(db, user_id, channel_id=channel_id)
    return _unwrap(res, f"The agent will post to #{(res.get('channel') or {}).get('name')}.")


@mcp.tool(**WRITE)
async def delete_agent_memory(memory_id: str, agent: str | None = None) -> dict:
    """Make a custom agent forget one thing it remembers (an id from get_agent_memory).

    Args:
        memory_id: The memory id.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, _, found = await _custom(agent)
    try:
        res = await custom_ops.delete_memory(db, found["_id"], memory_id)
    except custom_ops.OpError as e:
        return _refused(e)
    return {**res, "summary": "The agent forgot it."}


@mcp.tool(**LIVE)
async def graduate_rule(rule_id: str, choice: Literal["auto_notify", "auto_silent", "keep_asking"],
                        agent: str | None = None, confirm_code: str | None = None) -> dict:
    """Answer a graduation offer from get_agent_offers: let the rule run on its own (and tell the owner, or not), or keep asking first.

    keep_asking is saved on the first call. auto_notify and auto_silent let the agent change live Meta ads without asking,
    so they are two steps: the first call changes nothing and returns a preview and a confirm_code.
    Show the preview to the user and only call again with confirm_code after they say yes. Pass the same arguments.
    With no offer waiting for the rule it returns `ok: false`, changes nothing and issues no code.

    Args:
        rule_id: The rule id from get_agent_offers.
        choice: auto_notify (run on its own, tell me), auto_silent (run on its own) or keep_asking.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
        confirm_code: The code from the preview, once the user has agreed (only for auto_notify or auto_silent).
    """
    db, user_id, found = await _custom(agent)
    try:
        # Say it now if it can't work, so the user is never asked to confirm something that will be refused.
        current = await custom_ops.graduate_rule(db, found, rule_id, choice, check_only=True)
    except custom_ops.OpError as e:
        return _refused(e)

    async def execute():
        try:
            rule = await custom_ops.graduate_rule(db, found, rule_id, choice)
        except custom_ops.OpError as e:
            return _refused(e)
        said = {"keep_asking": "The agent will keep asking first.", "auto_notify": "The rule now runs on its own and tells the owner.",
                "auto_silent": "The rule now runs on its own."}[choice]
        return {"ok": True, "rule": rule, "summary": said}

    if choice == "keep_asking":
        return await execute()

    async def preview():
        return {"action": "Let this rule run on its own", "agent": found.get("name"), "rule": current,
                "result": "The agent will make this rule's changes in live Meta ads without asking first"
                          + (", and tell the owner after." if choice == "auto_notify" else ", silently.")}

    # Bound to the rule the preview showed (its kind and params), so an edit in between needs a fresh yes.
    state = {k: current.get(k) for k in ("kind", "params", "mode")}
    return await two_step(db, user_id, "graduate_rule", {"agent_id": found["_id"], "rule_id": rule_id, "choice": choice},
                          confirm_code, preview, execute, state=state)


@mcp.tool(**WRITE)
async def start_dry_run(agent: str | None = None) -> dict:
    """Start a dry run: replay the last 30 days to see what a custom agent would have done with its current rules. Nothing is changed in Meta.

    Returns a job_id; poll it with get_dry_run (it is usually already done on the first poll).

    Args:
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
    """
    db, user_id, found = await _custom(agent)
    job_id = await custom_ops.start_dry_run(
        db, get_shared_mongo_client(), user_id, found, end=replay.local_today(found) - timedelta(days=1),
        deadline=time.monotonic() + replay.DRY_RUN_BUDGET_SECONDS)
    return {"job_id": job_id}


@mcp.tool(**WRITE)
async def deactivate_agent(agent: str | None = None) -> dict:
    """Turn the Birdy Media Buying Agent off. It goes back to a draft and stops acting on its clients; its settings, cards and history stay, and it can be switched on again in Birdy.

    Leave agent out to turn off the owner's Birdy Media Buying Agent. Naming a custom agent returns `ok: false` with the reason (custom agents can't be turned off, only the media buying agent), and nothing changes.

    Args:
        agent: Optional agent id or name. Defaults to the Birdy Media Buying Agent.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    if agent:
        found = await resolve_agent(db, user_id, agent)
    else:
        found = await db[agent_store.AGENTS].find_one({"user_id": user_id, "type": agent_store.TYPE_STOCK})
        if found is None:
            raise ToolError("You don't have a media buying agent yet.")
    try:
        updated = await custom_ops.deactivate(db, user_id, found)
    except custom_ops.OpError as e:
        return _refused(e)
    said = f"Turned {found.get('name')} off." if found.get("status") == "active" else f"{found.get('name')} was already off."
    return {"ok": True, "agent": _agent_view(updated), "summary": said}


# ---------------------------------------------------------------------------
# Live tools (two steps)
# ---------------------------------------------------------------------------

async def _activate_preview(db, user_id: str, found: dict) -> dict:
    scope = found.get("scope") or {}
    ids = list(scope.get("client_group_ids") or [])
    names = {g["id"]: g.get("name") for g in await db["client_groups"].find(
        {"id": {"$in": ids}, "user_id": user_id}).to_list(length=None)}
    rules = [{"kind": r["kind"], "enabled": bool(r.get("enabled", True)),
              "mode": ("ask" if r["kind"] in agent_confidence.ASK_KINDS
                       else "flag" if r["kind"] in agent_confidence.FLAG_KINDS else r.get("mode"))}
             for r in await agent_store.list_rules(db, found["_id"])]
    return {"agent": found.get("name"), "status": found.get("status"), "schedule": scope.get("schedule"),
            "clients": [{"id": g, "name": names.get(g)} for g in ids if g in names],
            "may_change": {"permissions": found.get("permissions") or {}, "guardrails": found.get("guardrails") or {},
                           "rules": rules,
                           "note": "On ask, it proposes and waits for the owner. On flag, it only tells the owner."}}


@mcp.tool(**LIVE)
async def activate_agent(move_claims: bool = False, agent: str | None = None, confirm_code: str | None = None) -> dict:
    """Switch a custom agent on, so it runs on its schedule and acts on its clients' live Meta ads (what the owner allowed).

    Two steps. Called without confirm_code it changes nothing and returns a preview (its schedule, the clients it covers and what it may change) and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments. It is refused with `ok: false` and what is `missing` when the agent isn't ready (see get_agent_confidence), or when another agent already acts on one of its clients (see get_agent_claims; move_claims takes them over).

    Args:
        move_claims: Take over clients another agent acts on, instead of refusing.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
        confirm_code: The code from the preview, once the user has agreed.
    """
    db, user_id, found = await _custom(agent)
    if not confirm_code:
        # Say it now if it can't work, so the user is never asked to confirm something that will be refused.
        if found.get("status") == "active":
            return {"ok": True, "agent": _agent_view(found), "summary": "It is already on; nothing to confirm."}
        conf = await custom_ops.confidence(db, found)
        if not conf["ready"]:
            return {"ok": False, "error": "The agent isn't ready to switch on yet", "status": 409,
                    "ready": False, "missing": conf["missing"]}
        held = [] if move_claims else [c for c in (await custom_ops.claims_view(db, user_id, found))["clients"]
                                       if c["acting"] == "other_agent"]
        if held:
            return {"ok": False, "status": 409, "conflicts": held,
                    "error": "Another agent already acts on " + ", ".join(sorted({c["holder_name"] or "another agent" for c in held}))
                             + ". Call again with move_claims to take them over."}

    async def preview():
        out = await _activate_preview(db, user_id, found)
        return {**out, "ready": True, "missing": [], "takes_over_clients": [
            c["name"] for c in (await custom_ops.claims_view(db, user_id, found))["clients"]
            if move_claims and c["acting"] == "other_agent"]}

    async def execute():
        try:
            updated = await custom_ops.activate(db, user_id, found, move_claims=move_claims)
        except custom_ops.OpError as e:
            return _refused(e)
        said = "It was already on; nothing changed." if found.get("status") == "active" else \
            f"{found.get('name')} is now on and will run on its schedule."
        return {"ok": True, "agent": _agent_view(updated), "summary": said}

    return await two_step(db, user_id, "activate_agent", {"agent_id": found["_id"], "move_claims": move_claims},
                          confirm_code, preview, execute)


@mcp.tool(**LIVE)
async def undo_auto_action(suggestion_id: str, agent: str | None = None, confirm_code: str | None = None) -> dict:
    """Reverse something a custom agent did on its own in Meta, within 24 hours of it running (get_agent_activity marks which items `can_undo`; suggestion_id is the item's suggestion_id).

    Two steps. Called without confirm_code it changes nothing and returns a preview of the action and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments. An action that is too old, or that a person ran, returns `ok: false` with the reason.

    Args:
        suggestion_id: The suggestion_id of the auto-run item.
        agent: Optional custom agent id or name. Defaults to the owner's default agent.
        confirm_code: The code from the preview, once the user has agreed.
    """
    db, user_id, found = await _custom(agent)
    try:
        doc = await custom_ops.auto_action(db, user_id, found["_id"], suggestion_id)
    except custom_ops.OpError as e:
        return _refused(e)

    async def preview():
        return {"title": doc.get("title"), "client": doc.get("client_name"), "description": doc.get("description"),
                "applied_at": doc.get("applied_at"), "ran_on_its_own": True}

    async def execute():
        try:
            res = await custom_ops.undo_auto(db, get_shared_mongo_client(), user_id, found["_id"], suggestion_id,
                                             source=SOURCE)
        except custom_ops.OpError as e:
            return _refused(e)
        return {**res, "summary": "Undid the action in Meta."}

    return await two_step(db, user_id, "undo_auto_action", {"agent_id": found["_id"], "suggestion_id": suggestion_id},
                          confirm_code, preview, execute)
