"""
ai/team/setup.py
----------------
Agent onboarding: each team agent is set up, in order, through a chat interview with that agent
(contract: GET/PUT /api/team/setup, routers/team_setup.py).

  birdy        done when the normal onboarding is (users.onboarding.completed), else open
  media_buyer  clients, cpl, playbook, permissions, schedule, slack   (a practice run is optional, on request)
  sales_coach  clients, outcomes, speed, schedule, slack
  sub_agents   one step: finish or skip

An agent is "open" only when the one before it is "done", and "done" once its last step is saved
(setup.completed_at). Every account starts locked (no grandfathering): the lock is derived from the
missing completed_at, so nothing is migrated.

State lives on the agent's `team_agents` row (ai/team/memory.py), `{_id: "<key>:<uid>", user_id, key,
created_at, setup: {steps: {<step>: true}, answers: {<step>: body}, completed_at}}`; Sales Coach's
answers are also its `settings`. Each step's answer is written where that setting really lives:
Media Buyer's into the stock (Media Buying) agent (scope, settings, permissions) and the client
groups' CPL targets; Sales Coach's into its own `settings`.

A refusal raises SetupError(status, detail). 423 is the lock: {"code": "agent_locked", agent, waits_on}.
"""

import copy
import logging
import re
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from ai.team import definitions as defs
from ai.team.memory import TEAM_AGENTS

logger = logging.getLogger(__name__)

BIRDY, MEDIA_BUYER, SALES_COACH, SUB_AGENTS = defs.BIRDY, defs.MEDIA_BUYER, defs.SALES_COACH, "sub_agents"
ORDER = (BIRDY, MEDIA_BUYER, SALES_COACH, SUB_AGENTS)
WAITS_ON = {MEDIA_BUYER: BIRDY, SALES_COACH: MEDIA_BUYER, SUB_AGENTS: SALES_COACH}
LOCKED, OPEN, DONE = "locked", "open", "done"

# Not a step: a form the agent shows only when the owner asks for it.
OPTIONAL_BLOCKS = {MEDIA_BUYER: ("practice",), SALES_COACH: ()}

STEPS = {
    MEDIA_BUYER: ("clients", "cpl", "playbook", "permissions", "schedule", "slack"),
    SALES_COACH: ("clients", "outcomes", "speed", "schedule", "slack"),
}
LABELS = {
    "clients": "Clients", "cpl": "Cost per lead", "playbook": "Playbook", "permissions": "What it can change",
    "schedule": "Report time", "slack": "Slack", "practice": "Practice run", "outcomes": "Booked and closed",
    "speed": "Speed to lead",
}
DAYS = ("daily", "weekdays")
OUTCOME_KINDS = ("tag", "stage")
SPEED_MAX_MINUTES = 24 * 60
LABEL_MAX = 120
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# The plain-words refusal for tool, MCP and scheduled paths (the HTTP one is the 423 body).
SUB_LOCKED_TEXT = "Sub-agents aren't available yet: finish setting up Media Buyer and Sales Coach in Birdy first."


class SetupError(Exception):
    """The route answers {"detail": detail, **extra} with `status`."""

    def __init__(self, status: int, detail, extra: dict | None = None):
        super().__init__(str(detail))
        self.status, self.detail, self.extra = status, detail, dict(extra or {})

    @classmethod
    def from_body(cls, status: int, body: dict) -> "SetupError":
        """From a {"detail", ...} refusal body of another path (settings, preview)."""
        return cls(status, body.get("detail"), {k: v for k, v in body.items() if k != "detail"})


def row_id(key: str, user_id: str) -> str:
    return f"{key}:{user_id}"


def _now() -> datetime:
    return datetime.utcnow()


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# ---------------------------------------------------------------------------
# Reading state
# ---------------------------------------------------------------------------

async def _rows(db, user_id: str) -> dict:
    ids = [row_id(k, user_id) for k in (MEDIA_BUYER, SALES_COACH, SUB_AGENTS)]
    rows = await db[TEAM_AGENTS].find({"_id": {"$in": ids}, "user_id": user_id}).to_list(length=None)
    return {r.get("key"): r for r in rows}


async def _birdy_done(db, user_id: str) -> bool:
    user = await db["users"].find_one({"user_id": user_id}, {"onboarding.completed": 1}) or {}
    return bool((user.get("onboarding") or {}).get("completed"))


def _completed(row: dict | None):
    return ((row or {}).get("setup") or {}).get("completed_at")


async def statuses(db, user_id: str) -> dict:
    """{agent: {"status", "waits_on"}} for the four entries, in order."""
    rows = await _rows(db, user_id)
    out = {BIRDY: {"status": DONE if await _birdy_done(db, user_id) else OPEN, "waits_on": None}}
    for key in (MEDIA_BUYER, SALES_COACH, SUB_AGENTS):
        before = WAITS_ON[key]
        if _completed(rows.get(key)):
            out[key] = {"status": DONE, "waits_on": None}
        elif out[before]["status"] == DONE:
            out[key] = {"status": OPEN, "waits_on": None}
        else:
            out[key] = {"status": LOCKED, "waits_on": before}
    return out


async def status_of(db, user_id: str, key: str) -> dict:
    return (await statuses(db, user_id))[key]


async def locked_agents(db, user_id: str) -> frozenset:
    """The main agents not usable outside their setup chat, plus "sub_agents" while sub-agents are locked.
    Birdy is never locked. Never raises: a failed read locks nothing (the frontend does the same)."""
    try:
        st = await statuses(db, user_id)
    except Exception:
        logger.warning("team setup status read failed", exc_info=True)
        return frozenset()
    out = {k for k in defs.MAIN_AGENTS if st[k]["status"] != DONE}
    if st[SUB_AGENTS]["status"] == LOCKED:
        out.add(SUB_AGENTS)
    return frozenset(out)


def locked_body(key: str, waits_on: str | None) -> dict:
    return {"code": "agent_locked", "agent": key, "waits_on": waits_on}


def locked_text(key: str, waits_on: str | None = None) -> str:
    """Why `key` can't take questions, in plain words. `waits_on` is the agent whose setup comes first,
    when that one isn't done either."""
    if key == SUB_AGENTS or str(key).startswith("sub:"):
        return SUB_LOCKED_TEXT
    name = defs.NAMES.get(key, key)
    first = (f"{defs.NAMES.get(waits_on, waits_on)}'s setup, then {name}'s setup chat"
             if waits_on else f"{name}'s setup chat")
    return f"{name} isn't set up yet, so it can't take questions. Finish {first} in Birdy first."


async def refusal(db, user_id: str, key: str) -> str | None:
    """The plain-words refusal when `key` (a main agent or "sub_agents") is locked for normal use, else None."""
    if key == BIRDY:
        return None
    try:
        st = await statuses(db, user_id)
    except Exception:
        logger.warning("team setup status read failed", exc_info=True)
        return None
    if key == SUB_AGENTS:
        return SUB_LOCKED_TEXT if st[SUB_AGENTS]["status"] == LOCKED else None
    if key in defs.MAIN_AGENTS and st[key]["status"] != DONE:
        return locked_text(key, st[key]["waits_on"])
    return None


def _steps_done(row: dict | None, key: str) -> dict:
    done = ((row or {}).get("setup") or {}).get("steps") or {}
    return {s: bool(done.get(s)) for s in STEPS[key]}


def current_step(steps: dict, key: str) -> str | None:
    return next((s for s in STEPS[key] if not steps.get(s)), None)


# ---------------------------------------------------------------------------
# Where each setting lives
# ---------------------------------------------------------------------------

async def stock_agent(db, user_id: str) -> dict | None:
    from ai.agents.store import AGENTS, TYPE_STOCK
    return await db[AGENTS].find_one({"user_id": user_id, "type": TYPE_STOCK})


async def ensure_stock_agent(db, user_id: str) -> dict:
    """The user's Media Buying (stock) agent, created as a draft the way POST /api/agents does when there is none."""
    from ai.agents import store as agent_store
    agent = await stock_agent(db, user_id)
    if agent:
        return agent
    user = await db["users"].find_one({"user_id": user_id}, {"timezone": 1}) or {}
    tz = _valid_tz(user.get("timezone")) or "Europe/London"
    try:
        return await agent_store.create_agent(db, user_id, type=agent_store.TYPE_STOCK, tz=tz)
    except agent_store.AgentExists:   # made by another request between the read and the insert
        return await stock_agent(db, user_id)


def _valid_tz(tz) -> str | None:
    try:
        ZoneInfo(str(tz))
        return str(tz)
    except Exception:
        return None


def _stock_settings(agent: dict | None) -> dict:
    from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
    return merge_settings(DEFAULT_SETTINGS, (agent or {}).get("settings") or {})


async def _groups(db, user_id: str, ids=None) -> list[dict]:
    q = {"user_id": user_id}
    if ids is not None:
        q["id"] = {"$in": list(ids)}
    return await db["client_groups"].find(
        q, {"_id": 0, "id": 1, "name": 1, "ad_account_currency": 1, "targets": 1, "ghl_location_id": 1,
            "meta_ad_account_id": 1}).to_list(length=None)


def _clean_ids(raw) -> list[str] | str:
    if raw == "all":
        return "all"
    if not isinstance(raw, list) or not raw or not all(isinstance(g, str) and g.strip() for g in raw):
        raise SetupError(400, "client_group_ids must be \"all\" or a non-empty list of client ids")
    return list(dict.fromkeys(g.strip() for g in raw))


async def _resolve_ids(db, user_id: str, raw) -> list[str]:
    """The concrete client ids for a clients answer ("all" = every client the owner has now)."""
    ids = _clean_ids(raw)
    groups = await _groups(db, user_id)
    owned = [g["id"] for g in groups]
    if ids == "all":
        if not owned:
            raise SetupError(400, "You have no clients yet")
        return owned
    unknown = [g for g in ids if g not in set(owned)]
    if unknown:
        raise SetupError(400, f"Unknown client: {unknown[0]}")
    return ids


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def _schedule(body: dict) -> dict:
    time, tz, days = body.get("time"), body.get("tz"), body.get("days", "daily")
    if not isinstance(time, str) or not _HHMM.match(time):
        raise SetupError(400, "time must be HH:MM (24 hour)")
    if not _valid_tz(tz):
        raise SetupError(400, "tz must be a timezone such as Europe/London")
    if days not in DAYS:
        raise SetupError(400, "days must be daily or weekdays")
    return {"time": time, "tz": str(tz), "days": days}


def _bool_body(body: dict, key: str = "confirmed") -> None:
    if body.get(key) is not True:
        raise SetupError(400, f"{key} must be true")


# ── Media Buyer ──────────────────────────────────────────────────────────────

def _main_currency(groups: list[dict]) -> str | None:
    counts = Counter(g.get("ad_account_currency") for g in groups if g.get("ad_account_currency"))
    return counts.most_common(1)[0][0] if counts else None


def _red_for(green: float, settings: dict) -> float:
    """The red limit for a CPL target: the stock agent's own loser multiple (scoring.lose_cpl_multiple)."""
    mult = (settings.get("scoring") or {}).get("lose_cpl_multiple") or 1.3
    return round(green * mult, 2)


async def _settings_update(db, user_id: str, agent: dict, **kw) -> dict:
    from ai.agents.stock import agent_settings
    try:
        return await agent_settings.update_settings(db, user_id, agent, **kw)
    except agent_settings.SettingsError as e:
        raise SetupError.from_body(e.status, e.body)


async def _mb_save(db, user_id: str, step: str, body: dict) -> None:
    from ai.agents import store as agent_store
    agent = await ensure_stock_agent(db, user_id)
    if step == "clients":
        ids = await _resolve_ids(db, user_id, body.get("client_group_ids"))
        await _settings_update(db, user_id, agent, client_group_ids=ids)
    elif step == "cpl":
        default, clients = body.get("default"), body.get("clients") or {}
        if default is not None and not _number(default):
            raise SetupError(400, "default must be a number, 0 or more, or null")
        if not isinstance(clients, dict) or not all(isinstance(g, str) and _number(v) for g, v in clients.items()):
            raise SetupError(400, "clients must map client ids to numbers, 0 or more")
        settings = _stock_settings(agent)
        watched = (agent.get("scope") or {}).get("client_group_ids") or []
        currency = _main_currency(await _groups(db, user_id, watched))
        zones = ({"currency": currency, "green_under": default, "red_over": _red_for(default, settings)}
                 if default is not None else {"currency": None, "green_under": None, "red_over": None})
        per_client = {gid: {"zones": {"green_under": v, "red_over": _red_for(v, settings)}} for gid, v in clients.items()}
        for gid, c in (settings.get("clients") or {}).items():   # a client left out goes back to the default
            if gid not in clients and (c or {}).get("zones"):
                per_client[gid] = {"zones": None}
        await _settings_update(db, user_id, agent, settings={"zones": zones, "clients": per_client})
        from services import client_targets
        for gid, v in clients.items():   # the client's own CPL target is the same number
            await client_targets.apply_targets(db, user_id, gid, {"cpl": v})
    elif step == "playbook":
        _bool_body(body)
    elif step == "permissions":
        perms = {}
        for k in ("ads", "adsets", "campaigns"):
            if not isinstance(body.get(k), bool):
                raise SetupError(400, f"{k} must be true or false")
            perms[k] = body[k]
        await agent_store.update_agent(db, user_id, agent["_id"], {"permissions": {**perms, "budgets": False}})
    elif step == "schedule":
        scope = agent.get("scope") or {}
        await agent_store.update_agent(db, user_id, agent["_id"], {"scope": {**scope, "schedule": _schedule(body)}})
    elif step == "slack":
        channel_id = body.get("channel_id")
        if channel_id is not None and not (isinstance(channel_id, str) and channel_id.strip()):
            raise SetupError(400, "channel_id must be a Slack channel id or null")
        name = await _channel_name(db, user_id, channel_id) if channel_id else None
        await _settings_update(db, user_id, agent, settings={"slack": {"channel_id": channel_id, "channel_name": name}})


async def _channel_name(db, user_id: str, channel_id: str) -> str | None:
    """The channel's name when Slack can say it; the id is saved either way, as PATCH /settings does."""
    try:
        from services import slack_bot_service
        found = await slack_bot_service.find_channel(db, user_id, channel_id)
        return (found or {}).get("name")
    except Exception:
        return None


async def activate_media_buyer(db, user_id: str) -> dict:
    """Switch the stock agent on through the normal activation path. Never forces it:
    {"status": "active"} or {"status": "not_ready", "missing": [...]}."""
    from ai.agents import custom_ops
    agent = await stock_agent(db, user_id)
    if agent is None:
        return {"status": "not_ready", "missing": ["The Media Buying agent doesn't exist yet"]}
    try:
        agent = await custom_ops.activate(db, user_id, agent)
    except custom_ops.OpError as e:
        missing = e.body.get("missing") or [e.body.get("detail") or "It isn't ready to switch on yet"]
        return {"status": "not_ready", "missing": list(missing)}
    return {"status": "active" if agent.get("status") == "active" else "not_ready", "missing": []}


async def media_buyer_activation(db, user_id: str) -> dict:
    """Whether the stock agent is on and, when it isn't, what its own readiness check says is missing."""
    from ai.agents import custom_ops
    agent = await stock_agent(db, user_id)
    if agent is None:
        return {"status": "not_ready", "missing": ["The Media Buying agent doesn't exist yet"]}
    if agent.get("status") == "active":
        return {"status": "active", "missing": []}
    missing, _ = await custom_ops.stock_missing(db, user_id, agent)
    return {"status": "not_ready", "missing": missing}


# ── Practice run (the stock agent's preview) ────────────────────────────────

async def practice_status(db, user_id: str) -> dict:
    """{"status": none|running|done|failed, "summary", "job_id"} from the stock agent's newest preview run."""
    from ai.agents.stock import runner as stock_runner
    agent = await stock_agent(db, user_id)
    if agent is None:
        return {"status": "none", "summary": None, "job_id": None}
    rows = await db[stock_runner.RUNS].find({"agent_id": agent["_id"], "kind": stock_runner.PREVIEW}) \
        .sort("as_of", -1).limit(1).to_list(length=1)
    if not rows:
        return {"status": "none", "summary": None, "job_id": None}
    run = rows[0]
    if run.get("phase") != stock_runner.DONE:
        return {"status": "running", "summary": None, "job_id": run["_id"]}
    states = Counter(str((c or {}).get("state") or "unknown") for c in (run.get("clients") or {}).values())
    cards = await db[stock_runner.CARDS].find({"run_id": run["_id"]}, {"tag": 1, "lines": 1}).to_list(length=None)
    summary = {"clients": sum(states.values()), "client_states": dict(states), "cards": len(cards),
               "changes": sum(len(c.get("lines") or []) for c in cards),
               "ended_early": bool(run.get("ended_early")), "finished_at": _iso(run.get("ended_at") or run.get("as_of"))}
    failed = bool(run.get("ended_early")) or (states and set(states) == {"failed"})
    return {"status": "failed" if failed else "done", "summary": summary, "job_id": run["_id"]}


# ── Sales Coach ──────────────────────────────────────────────────────────────

def _outcome(raw, which: str) -> dict:
    if not isinstance(raw, dict) or raw.get("kind") not in OUTCOME_KINDS:
        raise SetupError(400, f"{which}.kind must be tag or stage")
    value, label = raw.get("value"), raw.get("label")
    if not isinstance(value, str) or not value.strip():
        raise SetupError(400, f"{which}.value must be the tag or the stage id")
    if not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX:
        raise SetupError(400, f"{which}.label must be 1 to {LABEL_MAX} characters")
    return {"kind": raw["kind"], "value": value.strip(), "label": label.strip()}


async def _sc_save(db, user_id: str, step: str, body: dict) -> dict:
    """The Sales Coach settings patch for one step (dotted paths under `settings`)."""
    if step == "clients":
        ids = _clean_ids(body.get("client_group_ids"))
        if ids != "all":
            await _resolve_ids(db, user_id, ids)
        return {"settings.client_group_ids": ids}
    if step == "outcomes":
        return {"settings.outcomes": {"booked": _outcome(body.get("booked"), "booked"),
                                      "closed": _outcome(body.get("closed"), "closed")}}
    if step == "speed":
        m = body.get("minutes")
        if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= SPEED_MAX_MINUTES:
            raise SetupError(400, f"minutes must be a whole number from 1 to {SPEED_MAX_MINUTES}")
        return {"settings.speed_minutes": m}
    if step == "schedule":
        return {"settings.schedule": _schedule(body)}
    if step == "slack":
        channel_id = body.get("channel_id")
        if channel_id is not None and not (isinstance(channel_id, str) and channel_id.strip()):
            raise SetupError(400, "channel_id must be a Slack channel id or null")
        name = await _channel_name(db, user_id, channel_id) if channel_id else None
        return {"settings.slack": {"channel_id": channel_id, "channel_name": name}}
    raise SetupError(404, "Unknown step")


async def sales_coach_settings(db, user_id: str) -> dict:
    row = await db[TEAM_AGENTS].find_one({"_id": row_id(SALES_COACH, user_id), "user_id": user_id}, {"settings": 1})
    return (row or {}).get("settings") or {}


async def coached_ids(db, user_id: str, settings: dict | None = None) -> list[str]:
    settings = settings if settings is not None else await sales_coach_settings(db, user_id)
    raw = settings.get("client_group_ids")
    if raw == "all" or not raw:
        return [g["id"] for g in await _groups(db, user_id)]
    return list(raw)


# ---------------------------------------------------------------------------
# Saving a step
# ---------------------------------------------------------------------------

async def save_step(db, user_id: str, key: str, step: str, body: dict) -> dict:
    """Validate and save one step's answer where it lives, tick the step, and finish the agent on its last
    step. The first time through, steps go in order (409 otherwise); a done step can be saved again."""
    if key not in STEPS:
        raise SetupError(404, "Unknown agent")
    if step not in STEPS[key]:
        raise SetupError(404, "Unknown step")
    if not isinstance(body, dict):
        raise SetupError(400, "The body must be an object")
    st = await statuses(db, user_id)
    if st[key]["status"] == LOCKED:
        raise SetupError(423, locked_body(key, st[key]["waits_on"]))
    row = (await _rows(db, user_id)).get(key)
    steps = _steps_done(row, key)
    current = current_step(steps, key)
    if not steps[step] and st[key]["status"] != DONE and step != current:
        raise SetupError(409, {"code": "step_out_of_order", "agent": key, "step": step, "expected": current})

    sets: dict = {}
    if key == MEDIA_BUYER:
        await _mb_save(db, user_id, step, body)
    else:
        sets.update(await _sc_save(db, user_id, step, body))
    now = _now()
    sets.update({f"setup.steps.{step}": True, f"setup.answers.{step}": copy.deepcopy(body),
                 "setup.updated_at": now})
    finishing = current == step and all(v for s, v in steps.items() if s != step) and not _completed(row)
    if finishing:
        sets["setup.completed_at"] = now
    await db[TEAM_AGENTS].update_one(
        {"_id": row_id(key, user_id)},
        {"$set": sets, "$setOnInsert": {"user_id": user_id, "key": key, "created_at": now}}, upsert=True)
    if finishing and key == MEDIA_BUYER:
        result = await activate_media_buyer(db, user_id)
        await db[TEAM_AGENTS].update_one({"_id": row_id(key, user_id)},
                                         {"$set": {"setup.activation": {**result, "at": now}}})
    return await state(db, user_id)


PLAYBOOK_KEYS = ("modes", "creative", "scoring")


async def edit_playbook(db, user_id: str, changes: dict) -> None:
    """Change Media Buyer's playbook rules during its interview: `changes` is a partial patch of the
    stock agent's `modes`, `creative` and `scoring` settings, checked by the settings' own validation.
    It ticks nothing: the owner still confirms the playbook. Allowed once the playbook step is reached."""
    if not isinstance(changes, dict) or not changes or set(changes) - set(PLAYBOOK_KEYS):
        raise SetupError(400, f"changes must be an object with any of {list(PLAYBOOK_KEYS)}")
    st = await statuses(db, user_id)
    if st[MEDIA_BUYER]["status"] == LOCKED:
        raise SetupError(423, locked_body(MEDIA_BUYER, st[MEDIA_BUYER]["waits_on"]))
    row = (await _rows(db, user_id)).get(MEDIA_BUYER)
    steps = _steps_done(row, MEDIA_BUYER)
    current = current_step(steps, MEDIA_BUYER)
    if not steps["playbook"] and st[MEDIA_BUYER]["status"] != DONE and current != "playbook":
        raise SetupError(409, {"code": "step_out_of_order", "agent": MEDIA_BUYER, "step": "playbook",
                               "expected": current})
    agent = await ensure_stock_agent(db, user_id)
    before = _stock_settings(agent)
    agent = await _settings_update(db, user_id, agent, settings=changes)
    after = _stock_settings(agent)
    if after["scoring"].get("lose_cpl_multiple") != before["scoring"].get("lose_cpl_multiple"):
        # The red CPL line is the target times the loser multiple: keep it in step.
        zones = after.get("zones") or {}
        patch: dict = {}
        if zones.get("green_under") is not None:
            patch["zones"] = {**zones, "red_over": _red_for(zones["green_under"], after)}
        own = {gid: {"zones": {"green_under": c["zones"]["green_under"],
                               "red_over": _red_for(c["zones"]["green_under"], after)}}
               for gid, c in (after.get("clients") or {}).items()
               if (c or {}).get("zones") and c["zones"].get("green_under") is not None}
        if own:
            patch["clients"] = own
        if patch:
            await _settings_update(db, user_id, agent, settings=patch)


async def finish_sub_agents(db, user_id: str, skipped: bool) -> dict:
    if not isinstance(skipped, bool):
        raise SetupError(400, "skipped must be true or false")
    st = await statuses(db, user_id)
    if st[SUB_AGENTS]["status"] == LOCKED:
        raise SetupError(423, locked_body(SUB_AGENTS, st[SUB_AGENTS]["waits_on"]))
    now = _now()
    await db[TEAM_AGENTS].update_one(
        {"_id": row_id(SUB_AGENTS, user_id)},
        {"$set": {"setup.skipped": skipped, "setup.completed_at": now},
         "$setOnInsert": {"user_id": user_id, "key": SUB_AGENTS, "created_at": now}}, upsert=True)
    return await state(db, user_id)


# ---------------------------------------------------------------------------
# What it knows: values (the PUT bodies, read back from where they live) and known lines
# ---------------------------------------------------------------------------

def _names(groups: list[dict], ids) -> str:
    by_id = {g["id"]: g.get("name") or g["id"] for g in groups}
    names = [by_id.get(g, g) for g in ids]
    shown = ", ".join(names[:3])
    more = f" and {len(names) - 3} more" if len(names) > 3 else ""
    return f"{len(names)} client{'s' if len(names) != 1 else ''}: {shown}{more}"


def _money(v, currency) -> str:
    from ai.suggestions.agents.useless_ad_purger import _symbol
    return f"{_symbol(currency)}{v:g}"


_MODE_WORDS = {"flag": "Only suggests", "ask": "Asks first before", "auto": "Acts on its own when"}
_MODE_KINDS = {"remove": "pausing", "add": "adding", "revive": "reviving"}


def _playbook_text(modes: dict) -> str:
    """"Asks first before pausing, adding or reviving ads", one clause per mode in use."""
    by_mode: dict = {}
    for kind, word in _MODE_KINDS.items():
        by_mode.setdefault(modes.get(kind) or "ask", []).append(word)
    clauses = []
    for mode, words in by_mode.items():
        listed = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]
        lead = _MODE_WORDS.get(mode, _MODE_WORDS["ask"])
        clauses.append(f"{lead if not clauses else lead[0].lower() + lead[1:]} {listed} ads")
    return "; ".join(clauses)


async def _mb_values(db, user_id: str, row: dict | None, groups: list[dict]) -> tuple[dict, dict]:
    """(values, known text) per step from the stock agent itself."""
    agent = await stock_agent(db, user_id)
    answers = ((row or {}).get("setup") or {}).get("answers") or {}
    values, text = {}, {}
    if agent is None:
        return values, text
    settings = _stock_settings(agent)
    scope = agent.get("scope") or {}
    ids = list(scope.get("client_group_ids") or [])
    if ids:
        all_ids = {g["id"] for g in groups}
        is_all = (answers.get("clients") or {}).get("client_group_ids") == "all" and set(ids) == all_ids
        values["clients"] = {"client_group_ids": "all" if is_all else ids}
        text["clients"] = "All clients" if is_all else _names(groups, ids)
    zones = settings.get("zones") or {}
    own = {gid: (c.get("zones") or {}).get("green_under") for gid, c in (settings.get("clients") or {}).items()
           if (c or {}).get("zones") and (c["zones"]).get("green_under") is not None}
    if zones.get("green_under") is not None or own:
        values["cpl"] = {"default": zones.get("green_under"), "clients": own}
        bits = []
        if zones.get("green_under") is not None:
            bits.append(f"{_money(zones['green_under'], zones.get('currency'))} for every client")
        by_id = {g["id"]: g for g in groups}
        bits += [f"{(by_id.get(g) or {}).get('name') or g} {_money(v, (by_id.get(g) or {}).get('ad_account_currency'))}"
                 for g, v in own.items()]
        text["cpl"] = "; ".join(bits)
    modes = settings.get("modes") or {}
    values["playbook"] = {"confirmed": True}
    text["playbook"] = _playbook_text(modes)
    perms = agent.get("permissions") or {}
    values["permissions"] = {k: bool(perms.get(k)) for k in ("ads", "adsets", "campaigns")}
    can = [w for k, w in (("ads", "ads"), ("adsets", "ad sets"), ("campaigns", "campaigns")) if perms.get(k)]
    text["permissions"] = (f"Can change {' and '.join(can)}" if can else "Changes nothing itself") + "; never budgets"
    sched = scope.get("schedule") or {}
    if sched.get("time"):
        values["schedule"] = {"time": sched["time"], "tz": sched.get("tz"), "days": sched.get("days") or "daily"}
        text["schedule"] = f"{sched['time']} {sched.get('tz') or ''}, {'weekdays' if sched.get('days') == 'weekdays' else 'every day'}".replace("  ", " ")
    slack = settings.get("slack") or {}
    values["slack"] = {"channel_id": slack.get("channel_id")}
    text["slack"] = (f"#{slack['channel_name']}" if slack.get("channel_name") else slack.get("channel_id")) \
        if slack.get("channel_id") else "Not in Slack"
    return values, text


def _sc_values(settings: dict, groups: list[dict]) -> tuple[dict, dict]:
    values, text = {}, {}
    ids = settings.get("client_group_ids")
    if ids:
        values["clients"] = {"client_group_ids": ids}
        text["clients"] = "All clients" if ids == "all" else _names(groups, ids)
    outcomes = settings.get("outcomes")
    if outcomes:
        values["outcomes"] = outcomes
        text["outcomes"] = f"Booked: {outcomes['booked']['label']}; closed: {outcomes['closed']['label']}"
    if settings.get("speed_minutes"):
        m = settings["speed_minutes"]
        values["speed"] = {"minutes": m}
        text["speed"] = f"Call new leads within {m} minute{'s' if m != 1 else ''}"
    sched = settings.get("schedule")
    if sched:
        values["schedule"] = sched
        text["schedule"] = f"{sched['time']} {sched['tz']}, {'weekdays' if sched.get('days') == 'weekdays' else 'every day'}"
    if "slack" in settings:
        slack = settings.get("slack") or {}
        values["slack"] = {"channel_id": slack.get("channel_id")}
        text["slack"] = (f"#{slack['channel_name']}" if slack.get("channel_name") else slack.get("channel_id")) \
            if slack.get("channel_id") else "Not in Slack"
    return values, text


async def agent_state(db, user_id: str, key: str, st: dict | None = None, rows: dict | None = None,
                      groups: list[dict] | None = None) -> dict:
    """One main agent's entry in GET /api/team/setup."""
    st = st or await statuses(db, user_id)
    rows = rows if rows is not None else await _rows(db, user_id)
    groups = groups if groups is not None else await _groups(db, user_id)
    row = rows.get(key)
    steps = _steps_done(row, key)
    if key == MEDIA_BUYER:
        values, text = await _mb_values(db, user_id, row, groups)
    else:
        values, text = _sc_values((row or {}).get("settings") or {}, groups)
    values = {s: v for s, v in values.items() if steps[s]}
    out = {
        "status": st[key]["status"], "waits_on": st[key]["waits_on"], "order": list(STEPS[key]), "steps": steps,
        "current": current_step(steps, key),
        "known": [{"step": s, "label": LABELS[s], "value": text.get(s) if steps[s] else None} for s in STEPS[key]],
        "values": values, "completed_at": _iso(_completed(row)),
    }
    if key == MEDIA_BUYER and _completed(row):
        # Set up but recorded as not switched on: try again, once it can. Only while the record says
        # not_ready, so an owner who later switches the agent off isn't overruled.
        if ((row.get("setup") or {}).get("activation") or {}).get("status") == "not_ready":
            result = await activate_media_buyer(db, user_id)
            if result["status"] == "active":
                await db[TEAM_AGENTS].update_one({"_id": row_id(key, user_id)},
                                                 {"$set": {"setup.activation": {**result, "at": _now()}}})
        out["activation"] = await media_buyer_activation(db, user_id)   # beyond the contract
    out["chat_session_id"] = await _setup_session(db, user_id, key)    # beyond the contract
    return out


async def _setup_session(db, user_id: str, key: str) -> str | None:
    """The latest setup chat with this agent, so the app can reopen it (setup chats stay out of the history list)."""
    try:
        rows = await db["ai_conversation_log"].find(
            {"user_id": user_id, "source": "birdy", "page": "team_setup", "agent": key}, {"session_id": 1, "_id": 0}
        ).sort("created_at", -1).limit(1).to_list(length=1)
        return rows[0].get("session_id") if rows else None
    except Exception:
        return None


async def state(db, user_id: str) -> dict:
    """The body of GET /api/team/setup."""
    st = await statuses(db, user_id)
    rows = await _rows(db, user_id)
    groups = await _groups(db, user_id)
    subs = ((rows.get(SUB_AGENTS) or {}).get("setup") or {})
    return {"order": list(ORDER), "agents": {
        BIRDY: {"status": st[BIRDY]["status"]},
        MEDIA_BUYER: await agent_state(db, user_id, MEDIA_BUYER, st, rows, groups),
        SALES_COACH: await agent_state(db, user_id, SALES_COACH, st, rows, groups),
        SUB_AGENTS: {"status": st[SUB_AGENTS]["status"], "waits_on": st[SUB_AGENTS]["waits_on"],
                     "skipped": bool(subs.get("skipped")), "completed_at": _iso(subs.get("completed_at"))},
    }}
