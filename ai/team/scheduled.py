"""
ai/team/scheduled.py
--------------------
A sub-agent's own run, on its schedule (jobs/agent_jobs.run_agents_tick -> schedule.run_due_sub_agents)
or when the owner presses Run (POST /api/agents/sub/{id}/run). Both go through `run_one`.

The run is the same nested, bounded run an ask uses (ai/team/delegation._run_child): the sub-agent's
own instructions and lessons, its tools cut to its main agent's read tools, the agency Context and
its client scope, read and propose only. No chat session is saved and nothing is archived.

  Clients   "all": one agency-wide run. A list: one run per client, each pinned to that client
            (at most MAX_CLIENTS_PER_RUN, in the order listed), so no run can read another client.
  Time      the whole run has min(RUN_BUDGET_SECONDS, time left before `deadline`); every model call
            and tool checks it (route.Turn.must_answer) and answers with what it has.
  Credits   the credits block the card writer uses (credits.is_blocked) is checked first: a blocked
            owner's run is skipped, nothing is spent. Otherwise the run is charged once, as its own
            ledger row: feature "sub_agent_run", agent "sub:<id>", with the per-run breakdown.
  Output    the answer and status are the run row (sub_agent_runs) and `last_run` on the record
            ({status, at, summary, credits}). Proposals are stored on the run row as pending, for the
            owner to approve from the app (routers/chat proposals). Nothing is applied and nothing is
            posted to Slack.

run_one's result: {status: "done" | "error" | "skipped", summary, answer, credits, proposals, clients,
reason?}. schedule.finish_run stores it.
"""

import asyncio
import logging
import time
from datetime import date

from ai.team import route as rt

logger = logging.getLogger(__name__)

RUN_BUDGET_SECONDS = 150        # one sub-agent run, all its clients
MIN_RUN_SECONDS = 60            # less than this before the deadline: the run waits for the next tick
MAX_CLIENTS_PER_RUN = 5
FEATURE = "sub_agent_run"
DONE, ERROR, SKIPPED = "done", "error", "skipped"


def _question(sub: dict, local_date: str, client_name: str | None) -> str:
    where = f"for {client_name}" if client_name else "across the agency"
    return (f"This is your scheduled run for {local_date}, {where}. Do the job your instructions describe, with "
            "your tools. Report what you found, finding first. If a change is worth making, propose it.")


async def _client_names(db, user_id: str, ids: list[str]) -> dict:
    rows = await db["client_groups"].find({"user_id": user_id, "id": {"$in": ids}},
                                          {"_id": 0, "id": 1, "name": 1}).to_list(length=None)
    return {r["id"]: r.get("name") for r in rows}


def _skipped(reason: str, summary: str) -> dict:
    return {"status": SKIPPED, "reason": reason, "summary": summary, "answer": None, "credits": 0.0,
            "proposals": [], "clients": []}


async def run_one(db, mongo_client, sub_agent: dict, *, local_date: str, deadline: float | None,
                  provider=None, tool_registry=None) -> dict:
    """One run of a specialist sub-agent (get_sub_agent's shape). Never raises for a model or tool
    failure: the result says how it went."""
    import credits
    from ai.team import delegation
    from ai.team.sub_agents import coerce

    user_id = sub_agent["user_id"]
    parent = sub_agent["parent"]
    if await credits.is_blocked(db, user_id):
        return _skipped("out_of_credits", "Skipped: out of Birdy Credits")
    if provider is None:
        from ai.provider_factory import NoAiCredentialError, get_provider_for_user
        try:
            provider = await get_provider_for_user(user_id, db)
        except NoAiCredentialError:
            return _skipped("no_ai_credentials", "Skipped: no AI key")
    if tool_registry is None:
        import routers.chat as chat_router   # registers every chat tool
        tool_registry = chat_router.registry

    budget = RUN_BUDGET_SECONDS
    if deadline is not None:
        budget = min(budget, deadline - time.monotonic())
    if budget < rt.FINAL_ANSWER_SECONDS:
        return _skipped("no_time", "Skipped: no time left in this run")

    clients = sub_agent.get("clients")
    gids = [None] if clients == "all" or not clients else list(clients)[:MAX_CLIENTS_PER_RUN]
    names = await _client_names(db, user_id, [g for g in gids if g])
    gids = [g for g in gids if g is None or g in names]      # a client deleted since is skipped
    if not gids:
        return _skipped("no_clients", "Skipped: none of its clients exist any more")

    sub = coerce({**sub_agent, "client_group_ids": [g for g in gids if g]}, parent)
    turn = rt.Turn(root_agent=sub.key, budget=budget)
    await turn.load_rates(db)
    model = getattr(provider, "model", None)
    ctx = {"provider": provider, "tool_registry": tool_registry, "db": db, "user_id": user_id,
           "mongo_client": mongo_client, "source": "birdy", "session_id": None}

    parts, summaries, failed = [], [], 0
    for gid in gids:
        name = names.get(gid) if gid else None
        if turn.time_left() <= rt.FINAL_ANSWER_SECONDS:
            failed += 1
            parts.append((name, None, "Not checked: out of time"))
            continue
        question = _question(sub_agent, local_date, name)
        step = turn.add_step(sub.key, sub.name, question=question, asked_by=parent)
        usage = rt.Usage(agent=sub.key, depth=1, model=model)
        turn.usages.append(usage)
        frame = rt.Frame(turn=turn, agent=sub.key, depth=1, chain=(parent, sub.key), step=step, usage=usage,
                         client_group_id=gid, client_name=name, sub=sub, parent_main=parent)
        task = asyncio.create_task(delegation._run_child(frame, question, ctx))
        try:
            out = await asyncio.wait_for(task, timeout=max(1.0, turn.time_left()))
        except asyncio.TimeoutError:
            out = None
            step.update(status="failed", summary="Ran out of time")
        except Exception:
            logger.exception("sub-agent run %s failed", sub_agent.get("id"))
            out = None
            step.update(status="failed", summary="Could not finish")
        if out is None:
            failed += 1
            parts.append((name, None, step.get("summary") or "Could not finish"))
            continue
        answer, summary = rt.split_summary(out.get("reply") or "")
        summary = summary or rt.summarize(answer) or "Answered"
        step.update(status="done", answer=answer, summary=summary)
        parts.append((name, answer, summary))
        summaries.append(summary)

    charge = 0.0
    totals = turn.totals()
    if totals["model_calls"]:
        charge = await credits.record_usage(
            db, user_id, model=model, source="birdy", feature=FEATURE, session_id=None,
            prompt_tokens=totals["prompt_tokens"], completion_tokens=totals["completion_tokens"],
            model_calls=totals["model_calls"], agent=sub.key, depth=0, agents=turn.breakdown()) or 0.0

    answered = len(gids) - failed
    if len(gids) == 1:
        name, answer, summary = parts[0]
        text = answer
    else:
        text = "\n\n".join(f"**{n}**\n{a if a is not None else s}" for n, a, s in parts)
        summary = f"{answered} of {len(gids)} clients checked"
    proposals = [{**p, "origin": "sub_agent_run", "sub_agent_id": sub_agent["id"]} for p in turn.proposals]
    return {"status": DONE if answered else ERROR, "summary": rt.clip(summary or "", rt.SUMMARY_MAX),
            "answer": text, "credits": round(float(charge), 4), "proposals": proposals,
            "clients": [g for g in gids if g], **({} if answered else {"reason": "failed"})}


def today_for(sub_agent: dict) -> str:
    """The sub-agent's local date for an owner-pressed run (its schedule's timezone, else UTC)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    tz = (sub_agent.get("schedule") or {}).get("timezone") or "UTC"
    try:
        return datetime.now(timezone.utc).astimezone(ZoneInfo(tz)).date().isoformat()
    except Exception:
        return date.today().isoformat()

