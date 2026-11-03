"""
ai/agents/stock/settings.py
---------------------------
The stock Media Buying Agent's settings: defaults, zone and mode resolution, validation.
Spec sections 3 and 5. Money limits are never pre-set: zones default to nulls, and a client's
zones come from its own override or from the agency default when the currencies match.
"""

import copy
import os
from dataclasses import dataclass

MODE_KINDS = ("remove", "add", "revive")
MODES = ("flag", "ask", "auto")
SETTINGS_KEYS = ("batches", "weekly", "zones", "treatments", "clients", "creative", "scoring",
                 "alerts", "modes", "slack")

DEFAULT_SETTINGS: dict = {
    "batches": [
        {"id": "a", "name": "Batch A", "weekdays": [0, 3]},
        {"id": "b", "name": "Batch B", "weekdays": [1, 4]},
    ],
    "weekly": {"enabled": False, "weekday": 0, "auto_post": False},
    "zones": {"currency": None, "green_under": None, "red_over": None},
    "treatments": [],
    "clients": {},
    "creative": {"max_ads_live": 4, "winner_days": 14, "winner_min_leads": 8,
                 "revive_after_days": 60, "major_downtrend_pct": 30, "auto_max_adds": 2},
    "scoring": {"min_days": 7, "win_min_leads": 5, "lose_cpl_multiple": 1.3,
                "lose_spend_multiple": 6, "lose_max_leads": 1, "rule_after": 3},
    "alerts": {"prepaid_days": 4, "spend_drop_ratio": 0.5},
    "modes": {"remove": "ask", "add": "ask", "revive": "ask"},
    "slack": {"channel_id": None, "channel_name": None, "also_tag_user_id": None, "templates": {}},
}


@dataclass(frozen=True)
class Zones:
    green_under: float
    red_over: float


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def add_writes_enabled() -> bool:
    return os.environ.get("AGENT_ADD_WRITES_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _zones_from(z) -> Zones | None:
    if isinstance(z, dict) and _num(z.get("green_under")) and _num(z.get("red_over")):
        return Zones(z["green_under"], z["red_over"])
    return None


def resolve_zones(settings: dict, gid: str, currency: str | None) -> Zones | None:
    """The client's own zones win. The agency default applies only when the client's account
    currency matches the default's currency. Otherwise None: the setup must ask for an override."""
    own = _zones_from((((settings.get("clients") or {}).get(gid)) or {}).get("zones"))
    if own is not None:
        return own
    default = settings.get("zones") or {}
    if default.get("currency") and currency and default["currency"] == currency:
        return _zones_from(default)
    return None


def resolve_mode(settings: dict, gid: str, kind: str) -> str:
    client = (((settings.get("clients") or {}).get(gid)) or {}).get("modes") or {}
    mode = client.get(kind) or (settings.get("modes") or {}).get(kind) or "ask"
    if kind == "add" and mode == "auto" and not add_writes_enabled():
        return "flag"  # Add writes are gated; until enabled Add can only flag
    return mode


def resolve_lost_spend(settings: dict, zones: Zones) -> float:
    mult = (settings.get("scoring") or {}).get("lose_spend_multiple", 6)
    return mult * zones.green_under


def settings_errors(settings: dict, groups: list[dict]) -> list[dict]:
    """Problems that block switching on. `groups` are the watched client_groups documents."""
    errors: list[dict] = []
    default = settings.get("zones") or {}
    if _num(default.get("green_under")) and _num(default.get("red_over")) \
            and default["red_over"] < default["green_under"]:
        errors.append({"field": "zones", "message": "Red must be at least the green limit"})
    for g in groups:
        gid, name = g.get("id"), g.get("name") or g.get("id")
        own = ((settings.get("clients") or {}).get(gid) or {}).get("zones")
        z = _zones_from(own)
        if z is not None and z.red_over < z.green_under:
            errors.append({"field": "zones", "client_group_id": gid,
                           "message": f"{name}: red must be at least the green limit"})
        elif resolve_zones(settings, gid, g.get("ad_account_currency")) is None:
            cur = g.get("ad_account_currency") or "an unknown currency"
            errors.append({"field": "zones", "client_group_id": gid,
                           "message": f"{name} needs its own zones: its account is in {cur}, "
                                      "which the default zones don't cover"})
    return errors


# ---------------------------------------------------------------------------
# Shape validation of a settings patch (400s)
# ---------------------------------------------------------------------------

def _err(field: str, message: str, gid: str | None = None) -> dict:
    return {"field": field, "message": message, **({"client_group_id": gid} if gid else {})}


def _zones_shape(z, field: str, gid: str | None = None, *, currency: bool) -> list[dict]:
    if not isinstance(z, dict):
        return [_err(field, "must be an object", gid)]
    out = []
    allowed = {"green_under", "red_over"} | ({"currency"} if currency else set())
    for k in set(z) - allowed:
        out.append(_err(f"{field}.{k}", "unknown field", gid))
    for k in ("green_under", "red_over"):
        if z.get(k) is not None and (not _num(z[k]) or z[k] < 0):
            out.append(_err(f"{field}.{k}", "must be a number, 0 or more", gid))
    if currency and z.get("currency") is not None and not (isinstance(z["currency"], str) and z["currency"]):
        out.append(_err(f"{field}.currency", "must be a currency code", gid))
    return out


def _modes_shape(m, field: str, gid: str | None = None) -> list[dict]:
    if not isinstance(m, dict):
        return [_err(field, "must be an object", gid)]
    out = []
    for k, v in m.items():
        if k not in MODE_KINDS:
            out.append(_err(f"{field}.{k}", "unknown change type", gid))
        elif v not in MODES:
            out.append(_err(f"{field}.{k}", "mode must be flag, ask or auto", gid))
    return out


def _numbers_shape(block, field: str, defaults: dict, *, positive_int=()) -> list[dict]:
    if not isinstance(block, dict):
        return [_err(field, "must be an object")]
    out = []
    for k, v in block.items():
        if k not in defaults:
            out.append(_err(f"{field}.{k}", "unknown field"))
        elif not _num(v) or v < 0 or (k in positive_int and int(v) != v):
            out.append(_err(f"{field}.{k}", "must be a whole number, 0 or more" if k in positive_int
                            else "must be a number, 0 or more"))
    return out


def _client_shape(gid: str, c) -> list[dict]:
    if not isinstance(c, dict):
        return [_err("clients", "must be an object", gid)]
    out = []
    for k in set(c) - {"batch_id", "treatment_ids", "zones", "notes", "on_hold", "prepaid", "modes"}:
        out.append(_err(f"clients.{k}", "unknown field", gid))
    if c.get("batch_id") is not None and not isinstance(c["batch_id"], str):
        out.append(_err("clients.batch_id", "must be a batch id", gid))
    if "treatment_ids" in c and not (isinstance(c["treatment_ids"], list)
                                     and all(isinstance(t, str) for t in c["treatment_ids"])):
        out.append(_err("clients.treatment_ids", "must be a list of treatment ids", gid))
    if "notes" in c and c["notes"] is not None and not isinstance(c["notes"], str):
        out.append(_err("clients.notes", "must be text", gid))
    if "on_hold" in c and not isinstance(c["on_hold"], bool):
        out.append(_err("clients.on_hold", "must be true or false", gid))
    if c.get("zones") is not None:
        out += _zones_shape(c["zones"], "clients.zones", gid, currency=False)
    if c.get("modes") is not None:
        out += _modes_shape(c["modes"], "clients.modes", gid)
    p = c.get("prepaid")
    if p is not None:
        tops = p.get("topups", []) if isinstance(p, dict) else None
        if not (isinstance(tops, list) and all(
                isinstance(t, dict) and isinstance(t.get("date"), str) and _num(t.get("amount"))
                and t["amount"] >= 0 for t in tops)):
            out.append(_err("clients.prepaid", "top-ups need a date and an amount", gid))
    return out


def shape_errors(patch: dict) -> list[dict]:
    """Per-field errors for a partial settings patch. Empty when every value has a valid shape."""
    out: list[dict] = []
    for key, val in patch.items():
        if key not in SETTINGS_KEYS:
            out.append(_err(key, "unknown setting"))
        elif key == "zones":
            out += _zones_shape(val, "zones", currency=True)
        elif key == "modes":
            out += _modes_shape(val, "modes")
        elif key == "creative":
            out += _numbers_shape(val, key, DEFAULT_SETTINGS[key], positive_int=(
                "max_ads_live", "winner_days", "winner_min_leads", "revive_after_days", "auto_max_adds"))
        elif key == "scoring":
            out += _numbers_shape(val, key, DEFAULT_SETTINGS[key], positive_int=(
                "min_days", "win_min_leads", "lose_max_leads", "rule_after"))
        elif key == "alerts":
            out += _numbers_shape(val, key, DEFAULT_SETTINGS[key])
        elif key == "batches":
            ok = isinstance(val, list) and all(
                isinstance(b, dict) and isinstance(b.get("id"), str) and isinstance(b.get("name"), str)
                and isinstance(b.get("weekdays"), list)
                and all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in b["weekdays"])
                for b in val)
            if not ok:
                out.append(_err(key, "each batch needs an id, a name and weekdays 0 to 6"))
        elif key == "treatments":
            if not (isinstance(val, list) and all(
                    isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("name"), str)
                    for t in val)):
                out.append(_err(key, "each treatment needs an id and a name"))
        elif key == "weekly":
            w = val if isinstance(val, dict) else None
            if w is None or set(w) - {"enabled", "weekday", "auto_post"} \
                    or any(k in w and not isinstance(w[k], bool) for k in ("enabled", "auto_post")) \
                    or ("weekday" in w and not (isinstance(w["weekday"], int)
                                                and not isinstance(w["weekday"], bool)
                                                and 0 <= w["weekday"] <= 6)):
                out.append(_err(key, "enabled and auto_post are true or false, weekday is 0 to 6"))
        elif key == "slack":
            s = val if isinstance(val, dict) else None
            if s is None or set(s) - {"channel_id", "channel_name", "also_tag_user_id", "templates"} \
                    or any(s.get(k) is not None and not isinstance(s[k], str)
                           for k in ("channel_id", "channel_name", "also_tag_user_id")) \
                    or ("templates" in s and not isinstance(s["templates"], dict)):
                out.append(_err(key, "invalid Slack settings"))
        elif key == "clients":
            if not isinstance(val, dict):
                out.append(_err(key, "must be an object"))
            else:
                for gid, c in val.items():
                    if c is not None:  # null removes the client's entry
                        out += _client_shape(gid, c)
    return out


def merge_settings(current: dict, patch: dict) -> dict:
    """Deep-merge a patch into settings. Objects merge, lists and scalars replace, and a null
    client entry removes that client's settings."""
    def merge(a, b):
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
        return out

    merged = merge(current, {k: v for k, v in patch.items() if k != "clients"})
    clients = copy.deepcopy(current.get("clients") or {})
    for gid, c in (patch.get("clients") or {}).items():
        if c is None:
            clients.pop(gid, None)
        else:
            clients[gid] = merge(clients.get(gid) or {}, c)
    merged["clients"] = clients
    return merged
