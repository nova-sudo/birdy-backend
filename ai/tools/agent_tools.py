"""
ai/tools/agent_tools.py
-----------------------
Chat tools that let a user train the Media Buying Agent by talking to Birdy
(page "agent_training"). Each tool resolves the user's agent itself and returns
{ok, changed: {...}} so the chat can echo the change, or {ok: false, error}
instead of raising. Rule validation is shared with the PATCH route
(ai/agents/validation.py).
"""

import copy
from typing import Optional

from ai.agents import store, validation
from ai.agents.defaults import DEFAULT_PARAMS, RULE_KINDS
from ai.suggestions.agents.useless_ad_purger import _symbol
from ai.tools.registry import registry
from services import client_targets

TOPICS = ("targets", "pause", "revive", "flags")
NO_AGENT = "Set up the Media Buying Agent first"


def _fail(error: str, **extra) -> dict:
    return {"ok": False, "error": error, **extra}


async def _agent(db, user_id):
    agents = await store.list_agents(db, user_id)
    return agents[0] if agents else None


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
    return {"ok": True, "changed": {"rule_kind": rule_kind, "param": param, "before": before, "after": value}}


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

AGENT_TOOL_NAMES = tuple(_TOOL_DEFS)


def register_agent_tools(tool_registry=None):
    target = tool_registry or registry
    for name, (executor, description, parameters) in _TOOL_DEFS.items():
        target.register(name=name, description=description, parameters=parameters, executor=executor)
