"""
ai/tools/agent_tools.py
-----------------------
Chat tools that let a user train one custom agent by talking to Birdy
(page "agent_training"), and set a new one up step by step (page "agent_setup",
the tools under "Setup tools"). Each tool resolves the agent from current_training_agent (set per request by routers/chat.py) and returns
{ok, changed: {...}} so the chat can echo the change, or {ok: false, error}
instead of raising. Rule validation is shared with the PATCH route
(ai/agents/validation.py).
"""

import copy
import re
from time import monotonic
from contextvars import ContextVar
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from ai.agents import confidence, replay, store, validation
from ai.agents.defaults import DEFAULT_PARAMS, RULE_KINDS
from ai.suggestions.agents.useless_ad_purger import _symbol
from ai.tools.registry import registry
from services import client_targets

TOPICS = ("targets", "pause", "revive", "flags")
NO_AGENT = "Set up this agent first"

# The agent the training chat is acting on. routers/chat.py sets it for the
# duration of one request on page "agent_training" or "agent_setup" and resets it afterwards;
# the default None means "no agent", never "the user's first agent".
current_training_agent: ContextVar[str | None] = ContextVar("current_training_agent", default=None)


def _fail(error: str, **extra) -> dict:
    return {"ok": False, "error": error, **extra}


async def _agent(db, user_id):
    agent_id = current_training_agent.get()
    if not agent_id:
        return None
    agent = await store.get_agent(db, user_id, agent_id)
    return agent if agent and agent.get("type") == store.TYPE_CUSTOM else None


async def _rule(db, agent_id, kind):
    return next((r for r in await store.list_rules(db, agent_id) if r["kind"] == kind), None)


async def _groups(db, user_id) -> list[dict]:
    return await db["client_groups"].find({"user_id": user_id}).to_list(length=None)


def _match_client(groups: list[dict], name) -> tuple[Optional[dict], Optional[dict]]:
    """(group, None) on a unique match, else (None, error dict). Exact name
    (case-insensitive) wins; otherwise a unique substring match is accepted."""
    names = sorted(g.get("name") or "" for g in groups)
    q = str(name or "").strip().lower()
    if not q:
        return None, _fail("client_name is required", candidates=names)
    exact = [g for g in groups if (g.get("name") or "").strip().lower() == q]
    if len(exact) == 1:
        return exact[0], None
    partial = exact or [g for g in groups if q in (g.get("name") or "").lower()]
    if len(partial) == 1:
        return partial[0], None
    if partial:
        return None, _fail(f"'{name}' matches more than one client",
                           candidates=sorted(g.get("name") or "" for g in partial))
    return None, _fail(f"No client matches '{name}'", candidates=names)


def _number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def get_playbook(db, user_id, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NO_AGENT)
    rules = await store.list_rules(db, agent["_id"])
    groups = await _groups(db, user_id)
    watched = set((agent.get("scope") or {}).get("client_group_ids") or [])
    if watched:
        groups = [g for g in groups if g.get("id") in watched]
    by_id = {g.get("id"): g.get("name") for g in await _groups(db, user_id)}
    return {
        "ok": True,
        "rules": [
            {
                "kind": r["kind"],
                "params": r.get("params") or {},
                "mode": r.get("mode"),
                "client_overrides": {
                    by_id.get(gid, gid): p for gid, p in (r.get("client_overrides") or {}).items()
                },
            }
            for r in rules
        ],
        "targets": {g.get("name"): (g.get("targets") or {}).get("cpl") for g in groups},
        "currencies": {g.get("name"): _symbol(g.get("ad_account_currency")) for g in groups},
        "topics": dict((agent.get("training") or {}).get("topics") or {}),
        "steps": _steps(agent),
    }


async def set_rule_param(db, user_id, rule_kind=None, param=None, value=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NO_AGENT)
    if rule_kind not in RULE_KINDS:
        return _fail(f"Unknown rule_kind. Use one of {list(RULE_KINDS)}")
    err = validation.params_error(rule_kind, {param: value})
    if err:
        return _fail(err)
    rule = await _rule(db, agent["_id"], rule_kind)
    if rule is None:
        return _fail(f"Rule {rule_kind} not found")
    params = rule.get("params") or {}
    before = params.get(param, DEFAULT_PARAMS[rule_kind].get(param))
    await store.update_rule(db, agent["_id"], rule["_id"], params={**params, param: value})
    result = {"ok": True, "changed": {"rule_kind": rule_kind, "param": param, "before": before, "after": value}}
    if not _steps(agent)["playbook"]:
        # Still being drafted in setup: show the updated playbook again so they can say yes to it.
        result.update(ui_block="playbook", playbook=_playbook(await store.list_rules(db, agent["_id"])))
    return result


async def set_client_target(db, user_id, client_name=None, cpl=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NO_AGENT)
    if not _number(cpl) or cpl < 0:
        return _fail("cpl must be a number, zero or more")
    group, err = _match_client(await _groups(db, user_id), client_name)
    if err:
        return err
    before = (group.get("targets") or {}).get("cpl")
    targets = await client_targets.apply_targets(db, user_id, group["id"], {"cpl": cpl})
    if targets is None:
        return _fail("Client group not found")
    return {"ok": True, "changed": {"client": group.get("name"), "param": "cpl", "before": before, "after": cpl}}


async def set_client_override(db, user_id, client_name=None, rule_kind=None, param=None, value=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NO_AGENT)
    if rule_kind not in RULE_KINDS:
        return _fail(f"Unknown rule_kind. Use one of {list(RULE_KINDS)}")
    group, err = _match_client(await _groups(db, user_id), client_name)
    if err:
        return err
    rule = await _rule(db, agent["_id"], rule_kind)
    if rule is None:
        return _fail(f"Rule {rule_kind} not found")
    overrides = copy.deepcopy(rule.get("client_overrides") or {})
    mine = overrides.get(group["id"]) or {}
    candidate = {**mine, param: value}
    err = await validation.overrides_error(db, user_id, rule_kind, {group["id"]: candidate})
    if err:
        return _fail(err)
    before = mine.get(param, (rule.get("params") or {}).get(param))
    overrides[group["id"]] = candidate
    await store.update_rule(db, agent["_id"], rule["_id"], client_overrides=overrides)
    return {"ok": True, "changed": {"rule_kind": rule_kind, "param": param, "client": group.get("name"),
                                    "before": before, "after": value}}


async def add_memory(db, user_id, text=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NO_AGENT)
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        return _fail("text is required")
    await store.add_memory(db, agent["_id"], text=text, kind="fact", source="chat")
    return {"ok": True, "changed": {"text": text}}


async def add_exception(db, user_id, text=None, client_name=None, ad_name_contains=None,
                        weekdays=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NO_AGENT)
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        return _fail("text is required")
    flt: dict = {}
    if client_name:
        group, err = _match_client(await _groups(db, user_id), client_name)
        if err:
            return err
        flt["client_group_id"] = group["id"]
    if ad_name_contains:
        if not isinstance(ad_name_contains, str) or not ad_name_contains.strip():
            return _fail("ad_name_contains must be text")
        flt["ad_name_contains"] = ad_name_contains.strip()
    if weekdays is not None:
        if (not isinstance(weekdays, list) or not weekdays
                or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in weekdays)):
            return _fail("weekdays must be a list of integers 0-6 (Monday = 0)")
        flt["weekdays"] = sorted(set(weekdays))
    if not flt:
        return _fail("An exception needs at least one of client_name, ad_name_contains or weekdays. "
                     "For a general note, use add_memory instead.")
    await store.add_memory(db, agent["_id"], text=text, kind="exception", source="chat", filter=flt)
    return {"ok": True, "changed": {"text": text, "filter": flt}}


async def answer_topic(db, user_id, topic=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NO_AGENT)
    if topic not in TOPICS:
        return _fail(f"Unknown topic. Use one of {list(TOPICS)}")
    # Targeted $set so concurrent topic answers don't overwrite each other.
    await db[store.AGENTS].update_one(
        {"_id": agent["_id"], "user_id": user_id}, {"$set": {f"training.topics.{topic}": True}}
    )
    return {"ok": True, "changed": {"topic": topic, "answered": True}}


# ---------------------------------------------------------------------------
# Setup tools (page "agent_setup"): one step at a time, each ticks training.steps
# ---------------------------------------------------------------------------

NOT_FOUND = "not_found"
SETUP_BLOCKS = ("clients", "permissions", "schedule", "slack")
PUBLIC_PRACTICE_KEYS = ("case_id", "rule_kind", "client_name", "title", "facts", "options")
# The preview runs inside the chat turn, which also waits on the model, so it gets less
# than POST /dry-run. One that runs out fails as timed_out and the preview block can retry.
PREVIEW_BUDGET_SECONDS = 120
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:\s*[:.]\s*(\d{2}))?\s*(?:([ap])\.?\s*m\.?)?\s*$", re.IGNORECASE)


def _steps(agent: dict) -> dict:
    done = (agent.get("training") or {}).get("steps") or {}
    return {k: bool(done.get(k)) for k in store.STEP_KEYS}


def _playbook(rules: list[dict]) -> list[dict]:
    order = {k: i for i, k in enumerate(RULE_KINDS)}
    return [{"kind": r["kind"], "enabled": bool(r.get("enabled", True)), "mode": r.get("mode"),
             "params": r.get("params") or {}}
            for r in sorted(rules, key=lambda r: order.get(r["kind"], len(order)))]


def parse_time(raw) -> Optional[str]:
    """ "7am", "07:00", "7:30 pm" -> "HH:MM" (24h), or None when it isn't a time of day."""
    m = _TIME_RE.match(str(raw or ""))
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if minute > 59:
        return None
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "p" else 0)
    elif hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


async def set_purpose(db, user_id, name=None, description=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    name = name.strip() if isinstance(name, str) else ""
    description = description.strip() if isinstance(description, str) else ""
    if not 1 <= len(name) <= store.NAME_MAX:
        return _fail("bad_name", detail=f"The name must be 1 to {store.NAME_MAX} characters")
    if len(description) > store.DESCRIPTION_MAX:
        return _fail("bad_description", detail=f"The description must be at most {store.DESCRIPTION_MAX} characters")
    await store.update_agent(db, user_id, agent["_id"], {"name": name, "description": description})
    await store.mark_step(db, agent["_id"], "purpose")
    return {"ok": True, "changed": "purpose", "name": name, "description": description}


async def draft_playbook(db, user_id, rules=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    if not _steps(agent)["purpose"]:
        # Without this the model sometimes drafted (or only described) a playbook and never named the
        # agent, leaving "What it does" open so Turn on could never unlock.
        return _fail("purpose_first", detail="Save the agent's name and description with set_purpose first, "
                                             "then call draft_playbook again.")
    if not isinstance(rules, list) or not rules:
        return _fail("rules must be a non-empty list of {kind, enabled, params}")
    seen: set = set()
    for item in rules:  # check every rule before writing any
        if not isinstance(item, dict) or item.get("kind") not in RULE_KINDS:
            return _fail(f"Unknown rule kind. This agent can only run {list(RULE_KINDS)}")
        if item["kind"] in seen:
            return _fail(f"{item['kind']} is listed twice")
        seen.add(item["kind"])
        if not isinstance(item.get("enabled", True), bool):
            return _fail("enabled must be true or false")
        err = validation.params_error(item["kind"], item.get("params") or {})
        if err:
            return _fail(err)
    for item in rules:
        rule = await _rule(db, agent["_id"], item["kind"])
        if rule is None:
            return _fail(f"Rule {item['kind']} not found")
        params = item.get("params") or None
        await store.update_rule(db, agent["_id"], rule["_id"],
                                params={**(rule.get("params") or {}), **params} if params else None,
                                enabled=item.get("enabled", True))
    return {"ok": True, "changed": "playbook", "ui_block": "playbook",
            "playbook": _playbook(await store.list_rules(db, agent["_id"]))}


async def confirm_playbook(db, user_id, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    await store.mark_step(db, agent["_id"], "playbook")
    topics = ["pause", "revive", "flags"]
    watched = (agent.get("scope") or {}).get("client_group_ids") or []
    if watched:
        groups = await db["client_groups"].find({"user_id": user_id, "id": {"$in": watched}}).to_list(length=None)
        if len(groups) == len(set(watched)) and all(
                _number((g.get("targets") or {}).get("cpl")) for g in groups):
            topics.insert(0, "targets")
    for topic in topics:
        await answer_topic(db, user_id, topic=topic)
    return {"ok": True, "changed": "playbook", "topics": topics}


async def show_block(db, user_id, block=None, **_):
    if await _agent(db, user_id) is None:
        return _fail(NOT_FOUND)
    if block not in SETUP_BLOCKS:
        return _fail(f"block must be one of {list(SETUP_BLOCKS)}")
    return {"ok": True, "ui_block": block}


async def set_schedule(db, user_id, time=None, tz=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    hhmm = parse_time(time)
    if hhmm is None:
        return _fail("bad_time")
    scope = agent.get("scope") or {}
    if not tz:
        tz = (scope.get("schedule") or {}).get("tz")
    if not tz:
        user = await db["users"].find_one({"user_id": user_id}, {"timezone": 1}) or {}
        tz = user.get("timezone") or "UTC"
    try:
        ZoneInfo(str(tz))
    except Exception:
        return _fail("bad_tz")
    schedule = {"time": hhmm, "tz": str(tz)}
    await store.update_agent(db, user_id, agent["_id"], {"scope": {**scope, "schedule": schedule}})
    await store.mark_step(db, agent["_id"], "schedule")
    return {"ok": True, "changed": "schedule", "schedule": schedule}


async def set_slack(db, user_id, channel_id=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    from services import slack_bot_service
    channel = await slack_bot_service.find_channel(db, user_id, channel_id)
    if channel is None:
        return _fail("unknown_channel")
    scope = agent.get("scope") or {}
    await store.update_agent(db, user_id, agent["_id"], {"scope": {**scope, "slack_channel_id": channel["id"]}})
    await store.mark_step(db, agent["_id"], "slack")
    return {"ok": True, "changed": "slack", "channel": {"id": channel["id"], "name": channel["name"]}}


async def ask_practice_case(db, user_id, mongo_client=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    conf = await confidence.compute_confidence(db, agent)
    score = {"score": conf["score"], "ready": conf["ready"], "missing": conf["missing"]}
    if conf["ready"]:
        return _fail("ready", confidence=score)  # practice is done: on to the preview
    case = await replay.next_practice_case(db, mongo_client, agent)
    if case is None:
        # Practice cases come from the watched clients' ads over the last PRACTICE_DAYS days. With
        # none to ask, the score can't reach READY_SCORE, so say why and how to unblock it.
        return _fail("no_cases", confidence=score, days=replay.PRACTICE_DAYS,
                     detail="The watched clients have no ads to practise on from the last "
                            f"{replay.PRACTICE_DAYS} days. Watch more clients, ones with ads running recently.")
    return {"ok": True, "ui_block": "practice", "case": {k: case.get(k) for k in PUBLIC_PRACTICE_KEYS},
            "confidence": score}


async def run_preview(db, user_id, mongo_client=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    job_id = await replay.start_dry_run_job(
        db, mongo_client, user_id, agent, end=replay.local_today(agent) - timedelta(days=1),
        deadline=monotonic() + PREVIEW_BUDGET_SECONDS)
    job = await db[store.JOBS].find_one({"_id": job_id}, {"status": 1}) or {}
    return {"ok": True, "ui_block": "preview", "job_id": job_id, "status": job.get("status")}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_KIND = {"type": "string", "enum": list(RULE_KINDS)}

_TOOL_DEFS = {
    "get_playbook": (
        get_playbook,
        "Read the agent's current playbook: each rule's params, mode and per-client overrides, each client's "
        "CPL target (null = none yet) and currency symbol, and which training topics are answered.",
        {"type": "object", "properties": {}, "required": []},
    ),
    "set_rule_param": (
        set_rule_param,
        "Change one numeric param of a rule for all clients. Only call once the user has confirmed the value.",
        {"type": "object", "properties": {
            "rule_kind": _KIND,
            "param": {"type": "string", "description": "A param name from get_playbook for that rule."},
            "value": {"type": "number", "description": "Zero or more."}},
         "required": ["rule_kind", "param", "value"]},
    ),
    "set_client_target": (
        set_client_target,
        "Set a client's cost-per-lead target, in that client's own currency. Match the client by name.",
        {"type": "object", "properties": {
            "client_name": {"type": "string"}, "cpl": {"type": "number", "description": "Zero or more."}},
         "required": ["client_name", "cpl"]},
    ),
    "set_client_override": (
        set_client_override,
        "Override one rule param for a single client only.",
        {"type": "object", "properties": {
            "client_name": {"type": "string"}, "rule_kind": _KIND,
            "param": {"type": "string"}, "value": {"type": "number"}},
         "required": ["client_name", "rule_kind", "param", "value"]},
    ),
    "add_memory": (
        add_memory,
        "Remember a general fact or preference the user told you about how to run their ads.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    ),
    "add_exception": (
        add_exception,
        "Remember an exception that applies only to some ads: a client, ads whose name contains some text, "
        "and/or certain weekdays. At least one filter is required; otherwise use add_memory.",
        {"type": "object", "properties": {
            "text": {"type": "string"},
            "client_name": {"type": "string"},
            "ad_name_contains": {"type": "string", "description": "Case-insensitive."},
            "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6},
                         "description": "Monday = 0 ... Sunday = 6."}},
         "required": ["text"]},
    ),
    "answer_topic": (
        answer_topic,
        "Mark a training topic as covered. Only after the user has confirmed the values for it.",
        {"type": "object", "properties": {"topic": {"type": "string", "enum": list(TOPICS)}},
         "required": ["topic"]},
    ),
}

TRAINING_TOOL_NAMES = tuple(_TOOL_DEFS)

_SETUP_TOOL_DEFS = {
    "set_purpose": (
        set_purpose,
        "Step 1, what it does: save the agent's short name and a one-sentence description of its job.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": f"1 to {store.NAME_MAX} characters."},
            "description": {"type": "string", "description": f"At most {store.DESCRIPTION_MAX} characters."}},
         "required": ["name", "description"]},
    ),
    "draft_playbook": (
        draft_playbook,
        "Step 2, its playbook: write the drafted rules and show them to the user to confirm. Only the four "
        "rule kinds exist. Leave params out to keep the defaults; params names come from get_playbook.",
        {"type": "object", "properties": {"rules": {"type": "array", "items": {
            "type": "object", "properties": {
                "kind": _KIND,
                "enabled": {"type": "boolean"},
                "params": {"type": "object", "additionalProperties": {"type": "number"}}},
            "required": ["kind", "enabled"]}}},
         "required": ["rules"]},
    ),
    "confirm_playbook": (
        confirm_playbook,
        "Step 2 done: call only after the user said the drafted playbook is right.",
        {"type": "object", "properties": {}, "required": []},
    ),
    "show_block": (
        show_block,
        "Show the user an inline picker for one step (clients, permissions, schedule or slack). Saves nothing: "
        "the user saves in the picker and the app tells you.",
        {"type": "object", "properties": {"block": {"type": "string", "enum": list(SETUP_BLOCKS)}},
         "required": ["block"]},
    ),
    "set_schedule": (
        set_schedule,
        "Step 5, report time: save the daily time the agent runs and reports, from what the user said "
        "(\"7am\", \"07:00\", \"7:30 pm\"). tz is an IANA zone; leave it out to keep the agent's.",
        {"type": "object", "properties": {
            "time": {"type": "string"}, "tz": {"type": "string", "description": "e.g. Europe/London"}},
         "required": ["time"]},
    ),
    "set_slack": (
        set_slack,
        "Step 6, Slack: save the channel this agent posts to, by channel id or name.",
        {"type": "object", "properties": {"channel_id": {"type": "string"}}, "required": ["channel_id"]},
    ),
    "ask_practice_case": (
        ask_practice_case,
        "After the six steps: show the user the next practice case to answer. Returns no_cases when none are left.",
        {"type": "object", "properties": {}, "required": []},
    ),
    "run_preview": (
        run_preview,
        "Last: run the preview of what the agent would have done over the last 30 days, and show it.",
        {"type": "object", "properties": {}, "required": []},
    ),
}

_TOOL_DEFS.update(_SETUP_TOOL_DEFS)

SETUP_TOOL_NAMES = tuple(_SETUP_TOOL_DEFS)
AGENT_TOOL_NAMES = TRAINING_TOOL_NAMES + SETUP_TOOL_NAMES
# The agent_setup page: the setup tools, then the training tools it also needs.
SETUP_PAGE_TOOLS = SETUP_TOOL_NAMES + ("get_playbook", "set_rule_param", "set_client_target",
                                       "set_client_override", "add_memory", "add_exception")


def register_agent_tools(tool_registry=None):
    target = tool_registry or registry
    for name, (executor, description, parameters) in _TOOL_DEFS.items():
        target.register(name=name, description=description, parameters=parameters, executor=executor)
