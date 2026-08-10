"""
credits.py
----------
Birdy Credits — usage-based AI metering layered on top of the Whop subscription
plans (see billing.py).

A credit is worth about one cent of AI cost. Each plan includes a monthly
allowance (Starter 2,000 / Growth 6,000 / Scale 10,000) that resets every Whop
billing period; usage draws it down, and customers top up when it runs out.
Two rates, per the billing strategy:

  * BYOK    (customer's own model key)  — a flat 0.5 credit / 1,000 tokens.
  * Managed (Birdy runs the model)      — cost¢ × the model's markup.

The app runs on BYOK today, so ``CREDITS_RATE_MODE`` defaults to ``byok``; the
Managed math is implemented and ready to switch on.

This module owns: the credit math, the per-user balance (``users.credits``),
the usage ledger (``ai_usage``), and the ``/api/credits`` endpoints. Enforcement
lives in ``credits_middleware.py``; metering is recorded from the AI orchestrator
via :func:`record_usage`.
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends

from dependencies import get_mongo_client, get_current_user
from core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Pricing ─────────────────────────────────────────────────────────────────
# Per-model pricing ($ / 1M tokens) and the Managed markup, from the billing
# strategy rate card. BYOK ignores the per-model rate (it's a flat per-token
# charge), but the table lets Managed price any supported model.
MODEL_PRICING = {
    "gpt-4o":                    {"in": 2.50, "out": 10.0, "markup": 4.0},
    "gpt-4.1":                   {"in": 2.00, "out": 8.0,  "markup": 4.0},
    "gpt-5":                     {"in": 1.25, "out": 10.0, "markup": 5.0},
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.0,  "markup": 5.0},
    "claude-sonnet-5":           {"in": 2.00, "out": 10.0, "markup": 4.0},
    "claude-opus-4-8":           {"in": 5.00, "out": 25.0, "markup": 2.5},
}
DEFAULT_MODEL = "gpt-4o"
BYOK_CREDITS_PER_1K = 0.5   # flat, any model

# Default charging mode until the Managed path ships. "byok" | "managed".
DEFAULT_RATE_MODE = os.getenv("CREDITS_RATE_MODE", "byok")

# Staged rollout: while OFF, usage is metered and the balance is shown, but the
# stopper never blocks (so shipping metering can't lock existing users out of
# AI). The frontend reads this to decide whether to hard-block or just warn.
# Flip CREDITS_ENFORCE=true to turn the hard stop on. (credits_middleware reads
# the same flag for the server-side gate.)
CREDITS_ENFORCE = os.getenv("CREDITS_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")

# Included monthly credits per plan.
PLAN_ALLOWANCE = {"starter": 2000, "growth": 6000, "scale": 10000}
ACTIVE_STATUSES = {"active", "trialing", "past_due", "canceling"}

# Top-up packs. Each pack's Whop plan id comes from its env var, falling back to
# the wired-in default below so the buy buttons work out of the box; set the env
# var to point a pack at a different plan (e.g. a sandbox plan) without a deploy.
TOPUP_PACKS = [
    {"id": "small",  "credits": 1000, "price": 10, "plan_env": "WHOP_PLAN_TOPUP_SMALL",  "plan_id": "plan_Kz3g3c4VyOWAF"},
    {"id": "medium", "credits": 2500, "price": 25, "plan_env": "WHOP_PLAN_TOPUP_MEDIUM", "plan_id": "plan_wY3HbDVa0JX1E"},
    {"id": "large",  "credits": 5000, "price": 50, "plan_env": "WHOP_PLAN_TOPUP_LARGE",  "plan_id": "plan_wQMhklGNLIwgf"},
]


def _pack_plan_id(pack: dict) -> str:
    """A pack's active Whop plan id: env override, else the wired-in default."""
    return os.getenv(pack["plan_env"]) or pack.get("plan_id", "")

# Warn when the balance drops under 10% of the allowance (or under 100 credits).
LOW_BALANCE_FRACTION = 0.10
LOW_BALANCE_MIN = 100


# ── Runtime-adjustable pricing (admin-controlled, DB-backed) ────────────────
# The Managed markup and the active rate mode are editable at runtime from the
# admin console (no redeploy), stored in one small settings doc. Reads are cached
# in-process for a few seconds so metering doesn't hit the DB on every AI call.
_SETTINGS_COLLECTION = "platform_settings"
_SETTINGS_ID = "credits"
_SETTINGS_TTL = 20.0  # seconds
_settings_cache = {"data": None, "ts": 0.0}

DEFAULT_MARKUP = float(MODEL_PRICING[DEFAULT_MODEL]["markup"])  # gpt-4o → 4.0
MARKUP_MIN, MARKUP_MAX = 1.0, 20.0


def _default_settings() -> dict:
    return {"markup": DEFAULT_MARKUP, "rate_mode": DEFAULT_RATE_MODE, "updated_at": None, "updated_by": None}


async def get_credits_settings(db, *, fresh: bool = False) -> dict:
    """The current Managed markup + rate mode. Cached briefly; pass fresh=True to
    bypass the cache (used right after a write, and by the admin read endpoint)."""
    now = time.monotonic()
    cached = _settings_cache["data"]
    if not fresh and cached is not None and (now - _settings_cache["ts"]) < _SETTINGS_TTL:
        return cached
    doc = None
    try:
        doc = await db[_SETTINGS_COLLECTION].find_one({"_id": _SETTINGS_ID})
    except Exception as e:  # never let a settings read break metering
        logger.debug(f"credits settings read failed, using defaults: {e}")
    doc = doc or {}
    data = {
        "markup": float(doc.get("markup") or DEFAULT_MARKUP),
        "rate_mode": doc.get("rate_mode") or DEFAULT_RATE_MODE,
        # Enforcement (the hard stopper) is admin-toggleable; the env var is just
        # the initial default until an admin sets it.
        "enforce": bool(doc["enforce"]) if doc.get("enforce") is not None else CREDITS_ENFORCE,
        "updated_at": doc.get("updated_at"),
        "updated_by": doc.get("updated_by"),
    }
    _settings_cache["data"] = data
    _settings_cache["ts"] = now
    return data


async def set_credits_settings(db, *, markup=None, rate_mode=None, enforce=None, admin: Optional[str] = None) -> dict:
    """Update the markup, rate mode, and/or enforcement. Clamps markup to a sane
    range and validates the mode; invalidates the in-process cache so the change
    takes effect on the next charge/check."""
    update: dict = {}
    if markup is not None:
        update["markup"] = round(max(MARKUP_MIN, min(float(markup), MARKUP_MAX)), 2)
    if rate_mode is not None:
        rate_mode = str(rate_mode).strip().lower()
        if rate_mode not in ("byok", "managed"):
            raise ValueError("rate_mode must be 'byok' or 'managed'")
        update["rate_mode"] = rate_mode
    if enforce is not None:
        update["enforce"] = bool(enforce)
    if update:
        update["updated_at"] = datetime.now(timezone.utc)
        if admin:
            update["updated_by"] = admin
        await db[_SETTINGS_COLLECTION].update_one(
            {"_id": _SETTINGS_ID}, {"$set": update}, upsert=True
        )
        _settings_cache["data"] = None  # force a fresh read next time
    return await get_credits_settings(db, fresh=True)


# ── Credit math ─────────────────────────────────────────────────────────────

def _pricing(model: Optional[str]) -> dict:
    return MODEL_PRICING.get(model or "", MODEL_PRICING[DEFAULT_MODEL])


def managed_credits(model: Optional[str], in_tokens: int, out_tokens: int, markup: Optional[float] = None) -> float:
    """Managed rate: real model cost in cents × the markup. Uses the admin-set
    markup when provided, else the model's default markup."""
    p = _pricing(model)
    m = float(markup) if markup is not None else p["markup"]
    cost_cents = (in_tokens * p["in"] + out_tokens * p["out"]) / 1_000_000 * 100
    return round(cost_cents * m, 2)


def byok_credits(in_tokens: int, out_tokens: int) -> float:
    """BYOK rate: a flat 0.5 credit per 1,000 tokens, any model."""
    return round(BYOK_CREDITS_PER_1K * (in_tokens + out_tokens) / 1000.0, 2)


def credits_for_usage(mode: str, model: Optional[str], in_tokens: int, out_tokens: int, markup: Optional[float] = None) -> float:
    in_tokens = int(in_tokens or 0)
    out_tokens = int(out_tokens or 0)
    if in_tokens <= 0 and out_tokens <= 0:
        return 0.0
    if mode == "managed":
        return managed_credits(model, in_tokens, out_tokens, markup=markup)
    return byok_credits(in_tokens, out_tokens)


# ── Balance model (users.credits) ───────────────────────────────────────────

def _period_key(sub: dict) -> str:
    """Identifies the current billing period so the allowance resets with it."""
    if sub and sub.get("current_period_end"):
        return str(sub["current_period_end"])
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _allowance_for(sub: dict) -> int:
    if not sub or sub.get("status") not in ACTIVE_STATUSES:
        return 0
    return PLAN_ALLOWANCE.get((sub.get("plan_id") or ""), 0)


def _effective_credits(user_doc: dict) -> tuple[dict, bool]:
    """Return (credits_dict, changed). Resets ``used`` when the billing period
    rolls over and keeps ``allowance`` in sync with the current plan. Top-up
    balance carries across periods."""
    sub = (user_doc or {}).get("subscription") or {}
    credits = dict((user_doc or {}).get("credits") or {})
    pk = _period_key(sub)
    allowance = _allowance_for(sub)
    changed = False

    if credits.get("period_key") != pk:
        credits = {
            "allowance": allowance,
            "used": 0.0,
            # Top-ups (and their dedup set) carry across billing periods.
            "topup_balance": float(credits.get("topup_balance", 0.0) or 0.0),
            "credited_topup_ids": credits.get("credited_topup_ids", []),
            "period_key": pk,
        }
        changed = True
    elif credits.get("allowance") != allowance:
        credits["allowance"] = allowance
        changed = True

    credits.setdefault("allowance", allowance)
    credits.setdefault("used", 0.0)
    credits.setdefault("topup_balance", 0.0)
    credits.setdefault("credited_topup_ids", [])
    return credits, changed


def _available(credits: dict) -> float:
    remaining_allowance = max(0.0, (credits.get("allowance", 0) or 0) - (credits.get("used", 0.0) or 0.0))
    return remaining_allowance + (credits.get("topup_balance", 0.0) or 0.0)


async def is_blocked(db, user_id: str) -> bool:
    """True when enforcement is on AND the user has no credits left. Non-raising
    (unlike credits_middleware.check_credits) for background/non-HTTP paths —
    the suggestions cron and Slack. Fails open on any error."""
    try:
        settings = await get_credits_settings(db)
        if not settings["enforce"]:
            return False
        credits, _ = await _load_and_sync(db, user_id)
        return _available(credits) <= 0
    except Exception as e:
        logger.error(f"is_blocked check failed for {user_id}: {e}", exc_info=True)
        return False


async def _load_and_sync(db, user_id: str) -> tuple[dict, dict]:
    """Load the user's credits, persisting a period rollover / plan change if
    needed. Returns (credits, subscription)."""
    user = await db["users"].find_one(
        {"user_id": user_id}, {"credits": 1, "subscription": 1, "_id": 0}
    )
    credits, changed = _effective_credits(user or {})
    if changed:
        await db["users"].update_one(
            {"user_id": user_id},
            {"$set": {"credits": {**credits, "updated_at": datetime.now(timezone.utc)}}},
            upsert=True,
        )
    return credits, (user or {}).get("subscription") or {}


def _status_payload(credits: dict, sub: dict, rate_mode: Optional[str] = None, enforce: Optional[bool] = None) -> dict:
    allowance = credits.get("allowance", 0) or 0
    used = credits.get("used", 0.0) or 0.0
    topup = credits.get("topup_balance", 0.0) or 0.0
    remaining_allowance = max(0.0, allowance - used)
    balance = remaining_allowance + topup
    low_threshold = max(LOW_BALANCE_MIN, allowance * LOW_BALANCE_FRACTION) if allowance else LOW_BALANCE_MIN
    return {
        "balance": int(round(balance)),
        "balance_exact": round(balance, 2),
        "allowance": int(allowance),
        "used": int(round(used)),
        "used_exact": round(used, 2),
        "remaining_allowance": int(round(remaining_allowance)),
        "topup_balance": int(round(topup)),
        "plan_id": (sub or {}).get("plan_id", "free"),
        "plan_name": (sub or {}).get("plan_name"),
        "subscribed": (sub or {}).get("status") in ACTIVE_STATUSES,
        "current_period_end": (sub or {}).get("current_period_end"),
        "rate_mode": rate_mode or DEFAULT_RATE_MODE,
        "enforced": CREDITS_ENFORCE if enforce is None else enforce,
        "low": balance <= low_threshold,
        "out": balance <= 0,
    }


# ── Metering (called from the AI orchestrator) ──────────────────────────────

async def record_usage(
    db,
    user_id: str,
    *,
    model: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    model_calls: int = 1,
    mode: Optional[str] = None,
    source: str = "birdy",
    feature: str = "ask_birdy",
    session_id: Optional[str] = None,
) -> float:
    """Charge a question's token usage to the user's balance and append to the
    ``ai_usage`` ledger. Best-effort: never raises into the AI request path."""
    try:
        settings = await get_credits_settings(db)
        mode = mode or settings["rate_mode"]
        charge = credits_for_usage(mode, model, prompt_tokens, completion_tokens, markup=settings["markup"])
        if charge <= 0:
            return 0.0

        # Draw from the plan allowance first, then from purchased top-up credits.
        # Read-modify-write is acceptable: in practice a user runs one question
        # at a time, and a lost race only under-charges by a single question.
        credits, _ = await _load_and_sync(db, user_id)
        remaining_allowance = max(0.0, credits["allowance"] - credits["used"])
        from_allowance = min(charge, remaining_allowance)
        from_topup = charge - from_allowance
        new_used = round(credits["used"] + from_allowance, 4)
        new_topup = round(max(0.0, credits["topup_balance"] - from_topup), 4)

        await db["users"].update_one(
            {"user_id": user_id},
            {"$set": {
                "credits.allowance": credits["allowance"],
                "credits.period_key": credits["period_key"],
                "credits.used": new_used,
                "credits.topup_balance": new_topup,
                "credits.updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        await db["ai_usage"].insert_one({
            "user_id": user_id,
            "session_id": session_id,
            "source": source,
            "feature": feature,
            "model": model,
            "mode": mode,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "model_calls": int(model_calls or 1),
            "credits": charge,
            "created_at": datetime.now(timezone.utc),
        })

        logger.info(
            f"💳 Credits: {user_id} -{charge} ({feature}/{mode}/{model}; "
            f"{prompt_tokens}+{completion_tokens} tok over {model_calls} calls)"
        )
        return charge
    except Exception as e:
        # Metering must never break the AI request.
        logger.error(f"Credit metering failed for {user_id}: {e}", exc_info=True)
        return 0.0


# ── Top-ups (credited from the Whop webhook on a top-up purchase) ───────────

def topup_plan_credits(plan_id: Optional[str]) -> int:
    """How many credits a purchased Whop plan grants, or 0 if it isn't a
    configured top-up plan (subscription plans return 0 here)."""
    if not plan_id:
        return 0
    for pack in TOPUP_PACKS:
        # Match the env override AND the wired-in default, so a purchase credits
        # whether the plan id is set via env or falls back to the built-in one.
        if plan_id in {os.getenv(pack["plan_env"], ""), pack.get("plan_id", "")} - {""}:
            return pack["credits"]
    return 0


async def add_topup(db, user_id: str, credits_amount: int, payment_id: str) -> bool:
    """Add purchased top-up credits to a user's balance, idempotently.

    Whop redelivers webhooks at-least-once, so we dedup on the Whop payment id:
    the update only applies when payment_id isn't already in credited_topup_ids,
    which the same atomic op records. Returns True if credited this call.
    """
    try:
        res = await db["users"].update_one(
            {"user_id": user_id, "credits.credited_topup_ids": {"$ne": payment_id}},
            {
                "$inc": {"credits.topup_balance": float(credits_amount)},
                "$addToSet": {"credits.credited_topup_ids": payment_id},
                "$set": {"credits.updated_at": datetime.now(timezone.utc)},
            },
            upsert=False,  # a real purchase always has an existing account row
        )
        if res.modified_count:
            # Audit trail in the usage ledger (top-ups show as a negative charge).
            await db["ai_usage"].insert_one({
                "user_id": user_id,
                "feature": "topup",
                "credits": -float(credits_amount),
                "payment_id": payment_id,
                "created_at": datetime.now(timezone.utc),
            })
            logger.info(f"💳 Credits: {user_id} +{credits_amount} top-up (payment {payment_id})")
            return True
        logger.info(f"💳 Top-up {payment_id} already applied (or no account) for {user_id}; skipping")
        return False
    except Exception as e:
        logger.error(f"add_topup failed for {user_id}: {e}", exc_info=True)
        return False


async def create_ai_usage_indexes(mongo_client):
    """Index the usage ledger for the per-user usage view (called at startup)."""
    try:
        await get_db(mongo_client)["ai_usage"].create_index([("user_id", 1), ("created_at", -1)])
    except Exception as e:
        logger.warning(f"Could not create ai_usage indexes: {e}")


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/api/credits/status")
async def credits_status(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mc:
        db = get_db(mc)
        credits, sub = await _load_and_sync(db, current_user)
        settings = await get_credits_settings(db)
        return {
            **_status_payload(credits, sub, rate_mode=settings["rate_mode"], enforce=settings["enforce"]),
            "_user_id": current_user,
        }


@router.get("/api/credits/usage")
async def credits_usage(days: int = 30, current_user: str = Depends(get_current_user)):
    days = max(1, min(int(days or 30), 90))
    async with get_mongo_client() as mc:
        db = get_db(mc)
        credits, sub = await _load_and_sync(db, current_user)
        _st = await get_credits_settings(db)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        # Consumption only — top-up purchases live in the same ledger (as a
        # negative "topup" charge) but belong on the billing view, not here.
        rows = await db["ai_usage"].find(
            {"user_id": current_user, "created_at": {"$gte": since}, "feature": {"$ne": "topup"}},
            {"_id": 0},
        ).sort("created_at", -1).to_list(3000)

        by_day: dict = {}
        by_feature: dict = {}
        for r in rows:
            created = r.get("created_at")
            day = created.strftime("%Y-%m-%d") if isinstance(created, datetime) else str(created)[:10]
            slot = by_day.setdefault(day, {"date": day, "credits": 0.0, "questions": 0})
            slot["credits"] = round(slot["credits"] + (r.get("credits", 0) or 0), 2)
            slot["questions"] += 1
            feat = r.get("feature", "ask_birdy")
            by_feature[feat] = round(by_feature.get(feat, 0.0) + (r.get("credits", 0) or 0), 2)

        return {
            "total_credits": round(sum((r.get("credits", 0) or 0) for r in rows), 2),
            "total_questions": len(rows),
            "period_days": days,
            "by_day": sorted(by_day.values(), key=lambda x: x["date"]),
            "by_feature": by_feature,
            "recent": rows[:50],
            "status": _status_payload(credits, sub, rate_mode=_st["rate_mode"], enforce=_st["enforce"]),
        }


@router.get("/api/credits/topup-packs")
async def topup_packs(current_user: str = Depends(get_current_user)):
    """The buyable top-up packs, with their configured Whop plan IDs (if set)."""
    return {
        "packs": [
            {
                "id": p["id"],
                "credits": p["credits"],
                "price": p["price"],
                "plan_id": _pack_plan_id(p),
            }
            for p in TOPUP_PACKS
        ]
    }
