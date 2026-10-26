"""
ai/agents/guardrails.py
-----------------------
Checks an agent suggestion must pass before Birdy writes to Meta. Run by
ai/suggestions/actions.apply_suggestion for suggestions that carry an
agent_id, whether a person approved them or the agent is auto-running.

Checked in order, first failure wins:
  1. no_permission  the agent may not touch this object type
  2. protected_tag  a target's name (or, for an ad set, any of its ads' names)
                    contains a protected tag
  3. last_live_ad   (pauses) the ad set would be left with no ACTIVE ad
  4. pause_cap      (pauses) the client has hit today's pause limit, counting
                    pauses by any actor since local midnight in the agent's tz

Also returned as a blocked_reason code, by apply_suggestion itself before
these checks run:
  flag_only         the item came from a flag-mode rule; it never writes
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai.agents.defaults import DEFAULT_GUARDRAILS, DEFAULT_PERMISSIONS
from ai.agents.meta_state import AD_STATE
from ai.suggestions.contracts import ACTION_PAUSE_ADS
from ai.suggestions.store import ACTIVITY, KIND_ACTION_APPLIED

logger = logging.getLogger(__name__)

PERMISSION_KEY = {"ad": "ads", "adset": "adsets", "campaign": "campaigns"}
DEFAULT_TZ = "Europe/London"
FLAG_ONLY = "flag_only"
FLAG_ONLY_MESSAGE = "This rule only flags — change it to Ask or Auto to act"


@dataclass
class GuardrailResult:
    ok: bool
    reason: str | None = None   # machine code: no_permission | protected_tag | last_live_ad | pause_cap (| flag_only)
    message: str | None = None  # human sentence for the card / Slack

    @classmethod
    def passed(cls) -> "GuardrailResult":
        return cls(ok=True)

    @classmethod
    def blocked(cls, reason: str, message: str) -> "GuardrailResult":
        return cls(ok=False, reason=reason, message=message)


def _naive_utc(dt: datetime) -> datetime:
    """Mongo stores naive UTC datetimes; accept either form from callers."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("guardrails: unknown tz %r, using %s", tz_name, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def agent_tz(agent: dict | None) -> str:
    return (((agent or {}).get("scope") or {}).get("schedule") or {}).get("tz") or DEFAULT_TZ


def local_today(now_utc: datetime, tz_name: str | None) -> date:
    """Today's date in `tz_name`."""
    return _naive_utc(now_utc).replace(tzinfo=timezone.utc).astimezone(_zone(tz_name)).date()


def local_day_start_utc(now_utc: datetime, tz_name: str | None) -> datetime:
    """Midnight today in `tz_name`, as naive UTC. DST-safe via zoneinfo."""
    tz = _zone(tz_name)
    local_now = _naive_utc(now_utc).replace(tzinfo=timezone.utc).astimezone(tz)
    midnight = datetime.combine(local_now.date(), time(0, 0), tzinfo=tz)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


async def _ad_states(db, client_group_id, ad_ids: list[str]) -> dict[str, dict]:
    if not ad_ids:
        return {}
    cur = db[AD_STATE].find({"client_group_id": client_group_id, "ad_id": {"$in": ad_ids}})
    return {d["ad_id"]: d async for d in cur}


async def check_guardrails(db, agent: dict, doc: dict, *, now_utc: datetime) -> GuardrailResult:
    action = doc.get("action") or {}
    atype = action.get("type")
    targets = action.get("targets") or []
    gid = doc.get("client_group_id")
    permissions = {**DEFAULT_PERMISSIONS, **(agent.get("permissions") or {})}
    guardrails = {**DEFAULT_GUARDRAILS, **(agent.get("guardrails") or {})}

    # 1. Permission for every target's object type.
    for t in targets:
        otype = t.get("object_type", "ad")
        key = PERMISSION_KEY.get(otype)
        if not key or not permissions.get(key):
            return GuardrailResult.blocked(
                "no_permission",
                f"This agent isn't allowed to change {key or otype or 'this object'}.",
            )

    ad_ids = [str(t.get("object_id")) for t in targets if t.get("object_type", "ad") == "ad"]
    states = await _ad_states(db, gid, ad_ids)

    # 2. Protected tags (case-insensitive substring of the name). Covers every
    # ad the action would affect: an ad set target is checked by its own name
    # and by the names of all its ads in meta_ad_state.
    tags = [str(tag) for tag in (guardrails.get("protected_tags") or []) if str(tag).strip()]
    if tags:
        for t in targets:
            names = [t.get("name") or ""]
            st = states.get(str(t.get("object_id")))
            if st:
                names.append(st.get("name") or "")
            if t.get("object_type") == "adset":
                names.extend(str(n) for n in (t.get("member_names") or []))
                names.extend([d.get("name") or "" async for d in db[AD_STATE].find(
                    {"client_group_id": gid, "adset_id": str(t.get("object_id"))})])
            for name in names:
                low = name.lower()
                for tag in tags:
                    if tag.lower() in low:
                        return GuardrailResult.blocked(
                            "protected_tag",
                            f"\"{name}\" is tagged {tag}, which is protected.",
                        )

    if atype != ACTION_PAUSE_ADS:
        return GuardrailResult.passed()

    # 3. Never leave an ad set with no ACTIVE ad.
    if guardrails.get("protect_last_live_ad"):
        pausing_by_adset: dict[str, set[str]] = {}
        for ad_id in ad_ids:
            st = states.get(ad_id)
            if st and st.get("adset_id"):
                pausing_by_adset.setdefault(st["adset_id"], set()).add(ad_id)
            # An ad missing from meta_ad_state can't be checked; the runner
            # refreshes state before evaluating, so this is rare.
        for adset_id, pausing in pausing_by_adset.items():
            live = {d["ad_id"] async for d in db[AD_STATE].find(
                {"client_group_id": gid, "adset_id": adset_id, "effective_status": "ACTIVE"})}
            if live and not (live - pausing):
                return GuardrailResult.blocked(
                    "last_live_ad",
                    "Pausing this would leave its ad set with no live ads.",
                )

    # 4. Per-client daily pause cap, any actor, since local midnight.
    caps = agent.get("client_caps") or {}
    cap = caps.get(gid, guardrails.get("max_pauses_per_client_day"))
    if cap is not None:
        now = _naive_utc(now_utc)
        start = local_day_start_utc(now, agent_tz(agent))
        used = await db[ACTIVITY].count_documents({
            "client_group_id": gid,
            "kind": KIND_ACTION_APPLIED,
            "applied_action_type": ACTION_PAUSE_ADS,
            "created_at": {"$gte": start, "$lte": now},
        })
        if used >= int(cap):
            return GuardrailResult.blocked(
                "pause_cap",
                f"Already {used} pause{'s' if used != 1 else ''} today for this client "
                f"(limit {int(cap)}).",
            )

    return GuardrailResult.passed()
