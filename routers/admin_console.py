"""
routers/admin_console.py
------------------------
Internal Admin console API. All endpoints are gated by `require_admin`
(dependencies.py) EXCEPT `/api/admin/impersonate/stop`, which by design runs
while the caller holds an impersonation token (see its docstring).

Two headline capabilities:
  1. Impersonate an agency owner — mint a short-lived token as that user so
     every existing product endpoint transparently runs as them. Fully
     audited to `admin_audit`.
  2. Read the durable conversation archive (`ai_conversation_log`) + platform
     analytics that power the three console views.

Kept separate from the existing routers/admin.py (ops/backfill endpoints,
which are NOT role-gated and predate this console).
"""

import logging
from datetime import datetime, timedelta

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core.config import JWT_SECRET, JWT_ALGORITHM
from core.database import DB_NAME
from core.utils import set_cookie
from dependencies import (
    get_mongo_client,
    get_current_claims,
    generate_tokens,
    require_admin,
)
from services.query_classifier import CATEGORY_LABELS
from credits import (
    get_credits_settings,
    set_credits_settings,
    _effective_credits,
    _available,
    MARKUP_MIN,
    MARKUP_MAX,
    DEFAULT_MODEL,
    MODEL_PRICING,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Impersonation sessions are deliberately short — long enough to debug, short
# enough that a forgotten session auto-reverts to the admin's own account
# (the admin's untouched refresh_token cookie restores them on expiry).
IMPERSONATION_TTL_MINUTES = 45


# ── Plan resolution ────────────────────────────────────────────────────────────
def _resolve_plan(subscription: dict | None) -> str:
    """Human plan name for a user's subscription doc. The billing webhook stores
    the resolved plan name on the subscription, so read it straight off the doc."""
    if not subscription:
        return "Free"
    return subscription.get("plan_name") or "Unknown"


async def _names_for(db, user_ids: list[str]) -> dict[str, str]:
    """Bulk email -> display-name lookup for building feeds/tables."""
    if not user_ids:
        return {}
    docs = await db["users"].find(
        {"user_id": {"$in": list(set(user_ids))}}, {"user_id": 1, "name": 1}
    ).to_list(None)
    return {d["user_id"]: (d.get("name") or d["user_id"]) for d in docs}


# ══════════════════════════════════════════════════════════════════════════════
# Impersonation
# ══════════════════════════════════════════════════════════════════════════════

class ImpersonateRequest(BaseModel):
    target_email: str


@router.post("/api/admin/impersonate")
async def impersonate(
    body: ImpersonateRequest,
    response: Response,
    admin_email: str = Depends(require_admin),
):
    """
    Start impersonating `target_email`. Mints a short-lived access token whose
    `sub` is the target (so every product endpoint runs as them) carrying
    `act`=admin and `imp`=True (so require_admin can reject it and /api/me can
    render the banner). Only the auth_token cookie is overwritten — the admin's
    refresh_token is left intact so the session cleanly reverts to the admin on
    TTL expiry.
    """
    target_email = body.target_email.strip().lower()
    if target_email == admin_email.lower():
        raise HTTPException(status_code=400, detail="You cannot impersonate yourself")

    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        target = await db["users"].find_one({"user_id": target_email}, {"user_id": 1, "name": 1})
        if not target:
            raise HTTPException(status_code=404, detail="Target user not found")

        exp = int((datetime.utcnow() + timedelta(minutes=IMPERSONATION_TTL_MINUTES)).timestamp())
        token = pyjwt.encode(
            {
                "sub": target_email,
                "act": admin_email,
                "imp": True,
                "type": "access",
                "exp": exp,
            },
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        set_cookie(response, "auth_token", token, IMPERSONATION_TTL_MINUTES * 60)

        await db["admin_audit"].insert_one({
            "admin": admin_email,
            "target": target_email,
            "action": "impersonate_start",
            "ts": datetime.utcnow(),
        })

    logger.info(f"[admin] {admin_email} started impersonating {target_email}")
    return {
        "impersonating": target_email,
        "name": target.get("name"),
        "expires_in": IMPERSONATION_TTL_MINUTES * 60,
    }


@router.post("/api/admin/impersonate/stop")
async def impersonate_stop(request: Request, response: Response):
    """
    End an impersonation session and restore the acting admin. This runs while
    the caller holds the *impersonation* token (not an admin token), so it does
    NOT use require_admin. It reads `act` off the current token — tolerating an
    already-expired one (verify_exp=False) so a lapsed session can still be
    reverted — verifies it is genuinely an impersonation token, then mints fresh
    admin tokens for `act`.
    """
    claims = await get_current_claims(request, verify_exp=False)
    if not claims.get("imp") or not claims.get("act"):
        raise HTTPException(status_code=400, detail="Not an impersonation session")

    admin_email = claims["act"]
    target_email = claims.get("sub")

    access_token, refresh_token = await generate_tokens(admin_email)
    from core.config import JWT_EXPIRY_MINUTES, JWT_REFRESH_EXPIRY_DAYS
    set_cookie(response, "auth_token", access_token, JWT_EXPIRY_MINUTES * 60)
    set_cookie(response, "refresh_token", refresh_token, JWT_REFRESH_EXPIRY_DAYS * 24 * 60 * 60)

    async with get_mongo_client() as mongo_client:
        await mongo_client[DB_NAME]["admin_audit"].insert_one({
            "admin": admin_email,
            "target": target_email,
            "action": "impersonate_stop",
            "ts": datetime.utcnow(),
        })

    logger.info(f"[admin] {admin_email} stopped impersonating {target_email}")
    return {"restored": admin_email}


# ══════════════════════════════════════════════════════════════════════════════
# View 1a — Agency owners table
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/admin/agencies")
async def list_agencies(
    search: str = "",
    skip: int = 0,
    limit: int = 50,
    admin_email: str = Depends(require_admin),
):
    """Owner/agency table with per-owner sub-account, lead, and AI-query counts."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        user_query = {}
        if search:
            user_query = {"$or": [
                {"user_id": {"$regex": search, "$options": "i"}},
                {"name": {"$regex": search, "$options": "i"}},
            ]}
        users = await db["users"].find(
            user_query,
            {"user_id": 1, "name": 1, "subscription": 1, "created_at": 1, "updated_at": 1, "role": 1},
        ).to_list(None)

        # Three aggregations (not per-user) then joined in Python — cheap at
        # internal-console scale (tens of accounts).
        subs = {d["_id"]: d["n"] for d in await db["client_groups"].aggregate(
            [{"$group": {"_id": "$user_id", "n": {"$sum": 1}}}]
        ).to_list(None)}
        leads = {d["_id"]: d["n"] for d in await db["facebook_leads"].aggregate(
            [{"$group": {"_id": "$user_id", "n": {"$sum": 1}}}]
        ).to_list(None)}
        queries = {d["_id"]: d for d in await db["ai_conversation_log"].aggregate([
            {"$match": {"role": "user"}},
            {"$group": {"_id": "$user_id", "n": {"$sum": 1}, "last": {"$max": "$created_at"}}},
        ]).to_list(None)}

        rows = []
        for u in users:
            uid = u["user_id"]
            q = queries.get(uid, {})
            rows.append({
                "email": uid,
                "owner": u.get("name") or uid,
                "role": u.get("role", "user"),
                "plan": _resolve_plan(u.get("subscription")),
                "sub_accounts": subs.get(uid, 0),
                "leads": leads.get(uid, 0),
                "ai_queries": q.get("n", 0),
                "last_active": q.get("last") or u.get("updated_at") or u.get("created_at"),
            })

        rows.sort(key=lambda r: (r["last_active"] or datetime.min), reverse=True)
        total = len(rows)
        return {"total": total, "agencies": rows[skip: skip + limit]}


# ══════════════════════════════════════════════════════════════════════════════
# View 1c — Platform stats dashboard
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/admin/stats")
async def platform_stats(admin_email: str = Depends(require_admin)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)
        six_weeks_ago = now - timedelta(weeks=6)

        agencies_total = await db["users"].count_documents({})
        sub_accounts_total = await db["client_groups"].count_documents({})
        leads_total = await db["facebook_leads"].count_documents({})
        ai_queries_total = await db["ai_conversation_log"].count_documents({"role": "user"})
        ai_queries_7d = await db["ai_conversation_log"].count_documents(
            {"role": "user", "created_at": {"$gte": week_ago}}
        )
        weekly_active = len(await db["ai_conversation_log"].distinct(
            "user_id", {"created_at": {"$gte": week_ago}}
        ))

        # Weekly growth (new agencies + sub-accounts per ISO week, last 6 weeks)
        def _weekly(coll):
            return db[coll].aggregate([
                {"$match": {"created_at": {"$gte": six_weeks_ago}}},
                {"$group": {
                    "_id": {"$isoWeek": "$created_at"},
                    "n": {"$sum": 1},
                    "wk": {"$min": {"$isoWeek": "$created_at"}},
                }},
                {"$sort": {"_id": 1}},
            ]).to_list(None)

        agencies_by_week = {d["_id"]: d["n"] for d in await _weekly("users")}
        subs_by_week = {d["_id"]: d["n"] for d in await _weekly("client_groups")}
        weeks = sorted(set(list(agencies_by_week.keys()) + list(subs_by_week.keys())))
        growth = [
            {"week": f"Wk {i + 1}", "agencies": agencies_by_week.get(w, 0), "sub_accounts": subs_by_week.get(w, 0)}
            for i, w in enumerate(weeks)
        ]

        # Top agencies by leads
        top = await db["facebook_leads"].aggregate([
            {"$group": {"_id": "$user_id", "leads": {"$sum": 1}}},
            {"$sort": {"leads": -1}},
            {"$limit": 5},
        ]).to_list(None)
        names = await _names_for(db, [t["_id"] for t in top])
        top_agencies = [
            {"email": t["_id"], "owner": names.get(t["_id"], t["_id"]), "leads": t["leads"]}
            for t in top
        ]

        # Live activity feed — merge recent signups, sub-accounts, AI queries
        activity = []
        for u in await db["users"].find({}, {"user_id": 1, "name": 1, "created_at": 1}).sort("created_at", -1).limit(5).to_list(None):
            if u.get("created_at"):
                activity.append({"type": "signup", "label": f"New agency signed up · {u.get('name') or u['user_id']}", "ts": u["created_at"]})
        for g in await db["client_groups"].find({}, {"user_id": 1, "name": 1, "created_at": 1}).sort("created_at", -1).limit(5).to_list(None):
            if g.get("created_at"):
                activity.append({"type": "sub_account", "label": f"New sub-account added · {g.get('name') or g['user_id']}", "ts": g["created_at"]})
        recent_q = await db["ai_conversation_log"].find(
            {"role": "user"}, {"user_id": 1, "created_at": 1}
        ).sort("created_at", -1).limit(5).to_list(None)
        q_names = await _names_for(db, [q["user_id"] for q in recent_q])
        for q in recent_q:
            activity.append({"type": "ai_query", "label": f"{q_names.get(q['user_id'], q['user_id'])} asked Birdy", "ts": q["created_at"]})
        activity.sort(key=lambda a: a["ts"], reverse=True)

        return {
            "kpis": {
                "agencies": agencies_total,
                "weekly_active": weekly_active,
                "sub_accounts": sub_accounts_total,
                "leads": leads_total,
                "ai_queries_total": ai_queries_total,
                "ai_queries_7d": ai_queries_7d,
            },
            "growth": growth,
            "top_agencies": top_agencies,
            "activity": activity[:12],
        }


# ══════════════════════════════════════════════════════════════════════════════
# View 1b — AI query analytics
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/admin/ai-queries")
async def ai_query_analytics(days: int = 7, admin_email: str = Depends(require_admin)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        now = datetime.utcnow()
        window_start = now - timedelta(days=days)

        # Theme clusters over the window
        cluster_rows = await db["ai_conversation_log"].aggregate([
            {"$match": {"role": "user", "created_at": {"$gte": window_start}}},
            {"$group": {"_id": "$category", "n": {"$sum": 1}}},
        ]).to_list(None)
        total = sum(r["n"] for r in cluster_rows) or 1
        clusters = sorted(
            [
                {
                    "category": (r["_id"] or "other"),
                    "label": CATEGORY_LABELS.get(r["_id"] or "other", "Other"),
                    "count": r["n"],
                    "pct": round(r["n"] * 100 / total),
                }
                for r in cluster_rows
            ],
            key=lambda c: c["count"], reverse=True,
        )

        # Roadmap signal — top two themes drive the callout
        roadmap_signal = None
        if clusters:
            top_two = clusters[:2]
            pct_sum = sum(c["pct"] for c in top_two)
            labels = " and ".join(c["label"].lower() for c in top_two)
            roadmap_signal = f"{pct_sum}% of queries are about {labels} — a strong signal for where to invest next."

        # Volume series — daily counts over last 14 days
        vol_rows = await db["ai_conversation_log"].aggregate([
            {"$match": {"role": "user", "created_at": {"$gte": now - timedelta(days=14)}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]).to_list(None)
        volume = [{"date": r["_id"], "count": r["n"]} for r in vol_rows]

        # Recent queries feed
        recent = await db["ai_conversation_log"].find(
            {"role": "user"},
            {"user_id": 1, "content": 1, "category": 1, "source": 1, "created_at": 1, "session_id": 1},
        ).sort("created_at", -1).limit(20).to_list(None)
        names = await _names_for(db, [r["user_id"] for r in recent])
        recent_queries = [
            {
                "email": r["user_id"],
                "owner": names.get(r["user_id"], r["user_id"]),
                "category": r.get("category") or "other",
                "category_label": CATEGORY_LABELS.get(r.get("category") or "other", "Other"),
                "source": r.get("source", "birdy"),
                "content": r.get("content", ""),
                "session_id": r.get("session_id"),
                "created_at": r.get("created_at"),
            }
            for r in recent
        ]

        return {
            "window_days": days,
            "total": sum(r["n"] for r in cluster_rows),
            "clusters": clusters,
            "roadmap_signal": roadmap_signal,
            "volume": volume,
            "recent_queries": recent_queries,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Conversation review
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/admin/users/{email}/conversations")
async def user_conversations(email: str, admin_email: str = Depends(require_admin)):
    """List a user's conversation sessions (grouped) for the review picker."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        sessions = await db["ai_conversation_log"].aggregate([
            {"$match": {"user_id": email}},
            {"$sort": {"created_at": 1}},
            {"$group": {
                "_id": "$session_id",
                "source": {"$first": "$source"},
                "started_at": {"$first": "$created_at"},
                "last_at": {"$last": "$created_at"},
                "messages": {"$sum": 1},
                "first_user_msg": {"$first": "$content"},
                "first_category": {"$first": "$category"},
            }},
            {"$sort": {"last_at": -1}},
        ]).to_list(None)

        return {
            "email": email,
            "sessions": [
                {
                    "session_id": s["_id"],
                    "source": s.get("source", "birdy"),
                    "started_at": s.get("started_at"),
                    "last_at": s.get("last_at"),
                    "message_count": s.get("messages", 0),
                    "preview": s.get("first_user_msg", ""),
                    "category": s.get("first_category") or "other",
                    "category_label": CATEGORY_LABELS.get(s.get("first_category") or "other", "Other"),
                }
                for s in sessions
            ],
        }


@router.get("/api/admin/conversations/{session_id}")
async def conversation_detail(session_id: str, admin_email: str = Depends(require_admin)):
    """Full ordered thread for one conversation session."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        msgs = await db["ai_conversation_log"].find(
            {"session_id": session_id}
        ).sort("created_at", 1).to_list(None)
        if not msgs:
            raise HTTPException(status_code=404, detail="Conversation not found")

        owner_id = msgs[0].get("user_id")
        names = await _names_for(db, [owner_id])
        return {
            "session_id": session_id,
            "email": owner_id,
            "owner": names.get(owner_id, owner_id),
            "source": msgs[0].get("source", "birdy"),
            "messages": [
                {
                    "role": m.get("role"),
                    "content": m.get("content", ""),
                    "tools_used": m.get("tools_used", []),
                    "category": m.get("category"),
                    "created_at": m.get("created_at"),
                }
                for m in msgs
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# View 4 — Birdy Credits: pricing controls + per-account balances
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/admin/credits/config")
async def credits_config(admin_email: str = Depends(require_admin)):
    """Current Managed markup + active rate mode, plus the model rate card the
    markup multiplies (so the console can preview what a question costs)."""
    async with get_mongo_client() as mongo_client:
        s = await get_credits_settings(mongo_client[DB_NAME], fresh=True)
    return {
        "markup": s["markup"],
        "rate_mode": s["rate_mode"],
        "markup_min": MARKUP_MIN,
        "markup_max": MARKUP_MAX,
        "model": DEFAULT_MODEL,
        "model_pricing": MODEL_PRICING.get(DEFAULT_MODEL),
        "updated_at": s.get("updated_at"),
        "updated_by": s.get("updated_by"),
    }


class CreditsConfigUpdate(BaseModel):
    markup: float | None = None
    rate_mode: str | None = None


@router.put("/api/admin/credits/config")
async def update_credits_config(body: CreditsConfigUpdate, admin_email: str = Depends(require_admin)):
    """Set the Managed markup (clamped to a sane range) and/or the active rate
    mode. Takes effect on the next charge; audited to admin_audit."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            s = await set_credits_settings(
                db, markup=body.markup, rate_mode=body.rate_mode, admin=admin_email
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await db["admin_audit"].insert_one({
            "admin": admin_email,
            "action": "credits_config_update",
            "markup": s["markup"],
            "rate_mode": s["rate_mode"],
            "ts": datetime.utcnow(),
        })
    logger.info(f"[admin] {admin_email} set credits config → markup={s['markup']} mode={s['rate_mode']}")
    return s


@router.get("/api/admin/credits")
async def credits_accounts(
    search: str = "",
    skip: int = 0,
    limit: int = 50,
    admin_email: str = Depends(require_admin),
):
    """Per-account credit view: current balance, this period's usage, and
    all-time credits used vs. purchased (from the ai_usage ledger)."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        user_query = {}
        if search:
            user_query = {"$or": [
                {"user_id": {"$regex": search, "$options": "i"}},
                {"name": {"$regex": search, "$options": "i"}},
            ]}
        users = await db["users"].find(
            user_query,
            {"user_id": 1, "name": 1, "subscription": 1, "credits": 1},
        ).to_list(None)

        # One ledger aggregation → all-time used / purchased / counts per user.
        # Usage rows carry a positive charge; top-ups a negative "topup" charge.
        agg = {d["_id"]: d for d in await db["ai_usage"].aggregate([
            {"$group": {
                "_id": "$user_id",
                "used_total": {"$sum": {"$cond": [{"$gt": ["$credits", 0]}, "$credits", 0]}},
                "purchased_total": {"$sum": {"$cond": [{"$eq": ["$feature", "topup"]}, {"$abs": "$credits"}, 0]}},
                "topups": {"$sum": {"$cond": [{"$eq": ["$feature", "topup"]}, 1, 0]}},
                "questions": {"$sum": {"$cond": [{"$ne": ["$feature", "topup"]}, 1, 0]}},
                "last_used": {"$max": "$created_at"},
            }},
        ]).to_list(None)}

        rows = []
        totals = {"balance": 0.0, "used_total": 0.0, "purchased_total": 0.0, "topup_balance": 0.0}
        for u in users:
            uid = u["user_id"]
            credits, _ = _effective_credits(u)  # in-memory period rollover; no write
            a = agg.get(uid, {})
            balance = _available(credits)
            topup_balance = float(credits.get("topup_balance", 0.0) or 0.0)
            row = {
                "email": uid,
                "name": u.get("name") or uid,
                "plan": _resolve_plan(u.get("subscription")),
                "allowance": int(credits.get("allowance", 0) or 0),
                "used_period": round(float(credits.get("used", 0.0) or 0.0), 2),
                "topup_balance": round(topup_balance, 2),
                "balance": round(balance, 2),
                "used_total": round(float(a.get("used_total", 0.0) or 0.0), 2),
                "purchased_total": round(float(a.get("purchased_total", 0.0) or 0.0), 2),
                "topups": int(a.get("topups", 0) or 0),
                "questions": int(a.get("questions", 0) or 0),
                "last_used": a.get("last_used"),
            }
            rows.append(row)
            totals["balance"] += balance
            totals["used_total"] += row["used_total"]
            totals["purchased_total"] += row["purchased_total"]
            totals["topup_balance"] += topup_balance

        # Most-active first (all-time consumption, then purchases).
        rows.sort(key=lambda r: (r["used_total"], r["purchased_total"]), reverse=True)
        return {
            "total": len(rows),
            "accounts": rows[skip: skip + limit],
            "totals": {k: round(v, 2) for k, v in totals.items()},
        }
