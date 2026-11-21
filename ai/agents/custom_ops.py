"""
ai/agents/custom_ops.py
-----------------------
What the owner does with a custom agent: decide proposals, switch it on or off, answer offers, undo an auto-run
action, run a dry run, read its confidence, claims and ad history. The HTTP routes (routers/agents.py) and the MCP
tools (ai/mcp/custom_agent_mcp.py) both go through these functions, so there is one implementation. A refusal raises
OpError carrying the status and the JSON body the route answers with.
"""

from datetime import datetime

from ai.agents import claims
from ai.agents import confidence as agent_confidence
from ai.agents import history as ad_history
from ai.agents import learning, outcomes, replay
from ai.agents import store as agent_store
from ai.agents.stock import lessons as stock_lessons
from ai.agents.stock import reports
from ai.agents.stock import settings as stock_settings
from ai.suggestions import actions as sug_actions
from ai.suggestions import store as sug_store

UNDO_AUTO_WINDOW = outcomes.UNDO_AUTO_WINDOW  # the activity feed's can_undo uses the same window
JOB_TIMEOUT = replay.JOB_TIMEOUT
FLAG_ONLY_KEY = claims.FLAG_ONLY_KEY


class OpError(Exception):
    def __init__(self, status: int, body: dict | str):
        body = {"detail": body} if isinstance(body, str) else body
        super().__init__(body.get("detail"))
        self.status, self.body = status, body


def rule_to_api(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    return out


# ---------------------------------------------------------------------------
# Proposals, offers, memory
# ---------------------------------------------------------------------------

async def list_proposals(db, agent_id: str, status: str) -> list[dict]:
    q: dict = {"agent_id": agent_id}
    if status != "all":
        q["status"] = status
    docs = await db[agent_store.PROPOSALS].find(q).sort("created_at", -1).to_list(length=None)
    return [reports.proposal_to_api(d) for d in docs]


async def accept_proposal(db, agent: dict, proposal_id: str, *, by: str) -> dict:
    """Yes on a proposal. A "Make it a rule?" one becomes a lesson and changes no rule."""
    stored = await db[agent_store.PROPOSALS].find_one({"_id": proposal_id, "agent_id": agent["_id"]}, {"kind": 1})
    if stored and stored.get("kind") == stock_lessons.PROMOTE:
        try:
            proposal, lesson = await stock_lessons.accept_promotion(db, agent, proposal_id, by=by)
        except learning.ProposalNotFound:
            raise OpError(404, "Proposal not found")
        except learning.ProposalClosed:
            raise OpError(409, "This proposal was already decided")
        return {"proposal": reports.proposal_to_api(proposal), "rule": None, "lesson": reports.memory_to_api(lesson)}
    try:
        proposal, rule = await learning.accept_proposal(db, agent, proposal_id)
    except learning.ProposalNotFound:
        raise OpError(404, "Proposal not found")
    except learning.ProposalClosed:
        raise OpError(409, "This proposal was already decided")
    except learning.RuleChanged:
        raise OpError(409, "The rule changed since this proposal was made; it is out of date")
    except ValueError as e:
        raise OpError(400, str(e))
    return {"proposal": reports.proposal_to_api(proposal), "rule": rule_to_api(rule)}


async def reject_proposal(db, agent: dict, proposal_id: str) -> dict:
    try:
        proposal = await learning.reject_proposal(db, agent, proposal_id)
    except learning.ProposalNotFound:
        raise OpError(404, "Proposal not found")
    except learning.ProposalClosed:
        raise OpError(409, "This proposal was already decided")
    return {"proposal": reports.proposal_to_api(proposal)}


async def graduate_rule(db, agent: dict, rule_id: str, choice: str, *, check_only: bool = False) -> dict:
    """Answer a graduation offer. check_only validates it the same way and changes nothing."""
    try:
        if check_only:
            rule = await learning.check_graduation(db, agent, rule_id, choice)
        else:
            rule = await learning.graduate(db, agent, rule_id, choice)
    except KeyError:
        raise OpError(404, "Rule not found")
    except learning.NoOffer:
        raise OpError(409, "There is no offer to answer for this rule")
    return rule_to_api(rule)


async def list_memory(db, agent_id: str) -> list[dict]:
    return [reports.memory_to_api(m) for m in await agent_store.list_memory(db, agent_id)]


async def delete_memory(db, agent_id: str, memory_id: str) -> dict:
    if not await stock_lessons.remove_lesson(db, agent_id, memory_id):
        raise OpError(404, "Memory not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Reading: claims, ad history, confidence
# ---------------------------------------------------------------------------

async def claims_view(db, user_id: str, agent: dict) -> dict:
    """Who acts on each client this agent watches. A client that no longer exists isn't listed."""
    agent_id = agent["_id"]
    gids = list(dict.fromkeys((agent.get("scope") or {}).get("client_group_ids") or []))
    names = {g["id"]: g.get("name") or "" async for g in db["client_groups"].find(
        {"id": {"$in": gids}, "user_id": user_id})}
    held = {r["client_group_id"]: r["agent_id"] async for r in db[claims.CLAIMS].find(
        {"user_id": user_id, "client_group_id": {"$in": gids}})}
    holders = {a["_id"]: a.get("name") or "" async for a in db[agent_store.AGENTS].find(
        {"_id": {"$in": [h for h in held.values() if h != agent_id]}, "user_id": user_id})}

    def row(gid: str) -> dict:
        holder = held.get(gid)
        acting = "no_agent" if holder is None else "this_agent" if holder == agent_id else "other_agent"
        return {"client_group_id": gid, "name": names[gid], "acting": acting,
                "holder_name": holders.get(holder) if acting == "other_agent" else None}

    return {"clients": [row(g) for g in gids if g in names]}


async def history_view(db, user_id: str, agent: dict) -> dict:
    """How far each watched client's Meta ad history has been pulled."""
    gids = (agent.get("scope") or {}).get("client_group_ids") or []
    groups = {g["id"]: g async for g in db["client_groups"].find({"id": {"$in": list(gids)}, "user_id": user_id})}
    states = await ad_history.statuses(db, [g for g in gids if groups.get(g, {}).get("meta_ad_account_id")])
    clients = [{
        "client_group_id": gid,
        "name": groups[gid].get("name") or "",
        "status": "ready" if ad_history.is_ready(state) else state["status"],
        "error": state["error"],
    } for gid, state in states.items()]
    return {"total": len(clients), "ready": sum(c["status"] == "ready" for c in clients), "clients": clients}


async def confidence(db, agent: dict) -> dict:
    """Compute the score and persist it on the agent (at_activation untouched)."""
    conf = await agent_confidence.compute_confidence(db, agent)
    await db[agent_store.AGENTS].update_one({"_id": agent["_id"]}, {"$set": {
        **{f"confidence.{k}": v for k, v in conf.items()},
        "confidence.updated_at": datetime.utcnow(),
    }})
    return conf


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

async def start_dry_run(db, mongo_client, user_id: str, agent: dict, *, end, deadline: float) -> str:
    return await replay.start_dry_run_job(db, mongo_client, user_id, agent, end=end, deadline=deadline)


async def get_job(db, user_id: str, agent_id: str, job_id: str) -> dict:
    jobs = db[agent_store.JOBS]
    job = await jobs.find_one({"_id": job_id, "agent_id": agent_id, "user_id": user_id})
    if job is None:
        raise OpError(404, "Job not found")
    created = job.get("created_at")
    if (job.get("status") in ("queued", "running") and isinstance(created, datetime)
            and datetime.utcnow() - created > JOB_TIMEOUT):
        # The platform killed the request that was running it: report and persist it.
        await jobs.update_one({"_id": job_id, "status": job["status"]},
                              {"$set": {"status": "failed", "error": "timed_out", "finished_at": datetime.utcnow()}})
        job = {**job, "status": "failed", "error": "timed_out"}
    out = {"status": job.get("status")}
    if job.get("status") == "done":
        out["result"] = job.get("result")
    elif job.get("status") == "failed":
        out["error"] = job.get("error")
    return out


# ---------------------------------------------------------------------------
# Switching on and off
# ---------------------------------------------------------------------------

async def stock_missing(db, user_id: str, agent: dict) -> tuple[list[str], list[dict]]:
    """What stops the stock agent switching on: a client, and zones that resolve for each. Also
    returns the settings errors behind any zone messages. A finished preview used to be required
    too; the owner dropped that (2026-11-22) so the daily run starts straight after setup. Its
    rules default to "ask first", so a first run proposes changes and applies none on its own."""
    ids = list((agent.get("scope") or {}).get("client_group_ids") or [])
    groups = await db["client_groups"].find({"id": {"$in": ids}, "user_id": user_id}).to_list(length=None)
    errors = stock_settings.settings_errors(agent.get("settings") or {}, groups)
    missing = [] if ids else ["Choose at least one client to watch"]
    missing += [e["message"] for e in errors]
    return missing, errors


async def activate(db, user_id: str, agent: dict, *, move_claims: bool = False, only_flag=None,
                   compute=None) -> dict:
    """Switch the agent on. Returns the agent doc (unchanged when it was already on). `compute` is the
    confidence function (the route passes its own so tests can stand in for it)."""
    agent_id = agent["_id"]
    if agent.get("status") == "active":
        return agent  # idempotent: nothing changes
    if agent.get("type") == agent_store.TYPE_STOCK:
        missing, errors = await stock_missing(db, user_id, agent)
        if missing:
            raise OpError(409, {"detail": "The agent isn't ready to switch on yet", "missing": missing, "errors": errors})
        conf = {"score": None}
    else:
        conf = await (compute or confidence)(db, agent)
        if not conf["ready"]:
            raise OpError(409, {"detail": "The agent isn't ready to switch on yet", "missing": conf["missing"]})
    scope = agent.get("scope") or {}
    ids = list(scope.get("client_group_ids") or [])
    flag_only = [g for g in (only_flag if only_flag is not None else scope.get(FLAG_ONLY_KEY) or []) if g in ids]
    conflict = await claims.claim_scope(db, user_id, agent_id, ids, move=move_claims, only_flag=flag_only)
    if conflict:
        raise OpError(409, conflict)
    await db[agent_store.AGENTS].update_one(
        {"_id": agent_id, "user_id": user_id}, {"$set": {f"scope.{FLAG_ONLY_KEY}": flag_only}})
    for rule in await agent_store.list_rules(db, agent_id):
        mode = ("ask" if rule["kind"] in agent_confidence.ASK_KINDS
                else "flag" if rule["kind"] in agent_confidence.FLAG_KINDS else None)
        if mode is not None:
            await agent_store.update_rule(db, agent_id, rule["_id"], mode=mode)  # syncs open items
    await db[agent_store.AGENTS].update_one(
        {"_id": agent_id, "user_id": user_id, "status": {"$ne": "active"}}, {
        "$set": {"status": "active", "confidence.at_activation": conf["score"], "activated_at": datetime.utcnow()},
        "$unset": {"turned_off_at": ""}})
    return await agent_store.get_agent(db, user_id, agent_id)


async def deactivate(db, user_id: str, agent: dict) -> dict:
    """Turn the Birdy Media Buying Agent off: back to a draft marked `turned_off_at`, its clients released.
    Scope, settings, runs, cards and lines stay. Returns the agent doc."""
    if agent.get("type") != agent_store.TYPE_STOCK:
        raise OpError(409, "Only the Birdy Media Buying Agent can be turned off")
    if agent.get("status") != "active":
        return agent  # idempotent: nothing changes
    return await agent_store.update_agent(
        db, user_id, agent["_id"], {"status": "draft", "turned_off_at": datetime.utcnow()})


# ---------------------------------------------------------------------------
# Undo an auto-run action
# ---------------------------------------------------------------------------

async def undo_auto(db, mongo_client, user_id: str, agent_id: str, suggestion_id: str, *, source: str) -> dict:
    """Undo an action Birdy auto-ran, within 24 hours of it running."""
    await auto_action(db, user_id, agent_id, suggestion_id)
    res = await sug_actions.undo_suggestion(
        db, mongo_client, user_id, suggestion_id, actor=sug_store.ACTOR_USER, source=source)
    if res["outcome"] == "not_found":
        raise OpError(404, "Suggestion not found")
    if res["outcome"] == "failed":
        raise OpError(502, res.get("detail", "Undo failed"))
    return {"ok": True, "outcome": res["outcome"], "succeeded": res.get("succeeded", [])}


async def auto_action(db, user_id: str, agent_id: str, suggestion_id: str) -> dict:
    """The suggestion, only if Birdy auto-ran it for this user's agent and it is still inside the undo window."""
    doc = await db[sug_store.SUGGESTIONS].find_one({"_id": suggestion_id, "user_id": user_id, "agent_id": agent_id})
    if doc is None:
        raise OpError(404, "Suggestion not found")
    if (doc.get("status") != sug_store.STATUS_APPLIED
            or (doc.get("decided_by") or {}).get("actor") != sug_store.ACTOR_BIRDY):
        raise OpError(409, "Only an action Birdy ran on its own can be undone here")
    applied_at = doc.get("applied_at")
    if not isinstance(applied_at, datetime) or datetime.utcnow() - applied_at > UNDO_AUTO_WINDOW:
        raise OpError(410, "Auto-run actions can only be undone for 24 hours")
    return doc
