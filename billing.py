"""
billing.py
----------
Whop billing integration.

Uses the official Whop Python SDK (``whop-sdk``) to read memberships and the
Standard Webhooks signature scheme (``whop-sdk[webhooks]`` → ``standardwebhooks``)
to verify incoming webhooks.

Design notes
============
Whop does **not** have Paddle's "update the subscription's items in place" API.
Plan changes and cancellations happen through Whop's hosted customer portal
(``membership.manage_url``). So this module only needs to:

  * expose the user's subscription status (``GET /api/billing/status``),
  * hand back the Whop portal URL (``GET /api/billing/portal-url``), and
  * react to Whop membership / payment webhooks (``POST /api/billing/webhook``).

New subscriptions and the optional "extra client slot" add-on are started from
the frontend via Whop's embedded checkout; the webhook is what actually flips a
subscription live here.

User linking
============
In this codebase ``user_id`` *is* the account email (the JWT ``sub``). The
frontend prefills that email into Whop checkout, so a membership webhook is
matched back to the account by ``metadata.user_id`` (when a checkout session set
it) and otherwise by the buyer's Whop email.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from whop_sdk import AsyncWhop

# whop-sdk is unpinned in requirements.txt, so a Vercel build installs whatever
# is current — 1.x in production while this checkout has 0.0.41. A hard
# `from whop_sdk import APIStatusError` therefore risks an ImportError at module
# load, and billing.py is imported by main.py, so that takes down every route in
# the app rather than just the promo-codes screen. It did exactly that.
#
# Resolve the error types by lookup instead, falling back to sentinels that
# nothing can be an instance of: on an SDK that has moved these names, error
# translation degrades to the generic branch, which still reports the failure.
try:  # pragma: no cover - depends on the installed SDK
    from whop_sdk import APIConnectionError, APIStatusError
except ImportError:  # pragma: no cover
    class APIStatusError(Exception):
        """Placeholder — the installed whop-sdk does not export this."""

    class APIConnectionError(Exception):
        """Placeholder — the installed whop-sdk does not export this."""

# Standard Webhooks is only used as a fallback for `whsec_`-style secrets; Whop's
# dashboard webhooks use their own scheme (see `_verify_webhook_signature`), so
# keep this import soft.
try:
    from standardwebhooks import Webhook as StandardWebhook
except ImportError:  # pragma: no cover
    StandardWebhook = None

from dependencies import get_mongo_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Config ─────────────────────────────────────────────────────────────────────

WHOP_API_KEY        = os.getenv("WHOP_API_KEY", "")
WHOP_WEBHOOK_SECRET = os.getenv("WHOP_WEBHOOK_SECRET", "")
# Only needed for the promo-codes admin surface (list/create/delete); the
# membership/payment webhook flow above never needs it, which is why it can be
# unset or stale for a long time before anyone notices.
#
# It is the `biz_...` id of the company WHOP_API_KEY belongs to — the dashboard
# URL carries it. Whop resolves the key WITHIN the company you name, so a value
# from a different company answers "This Bot was not found" rather than
# anything about the id being wrong.
WHOP_COMPANY_ID     = os.getenv("WHOP_COMPANY_ID", "")

WHOP_PLAN_STARTER      = os.getenv("WHOP_PLAN_STARTER", "")
WHOP_PLAN_GROWTH       = os.getenv("WHOP_PLAN_GROWTH", "")
WHOP_PLAN_SCALE        = os.getenv("WHOP_PLAN_SCALE", "")
# Optional: a separate recurring "extra client slot" plan (Scale add-on).
WHOP_PLAN_EXTRA_CLIENT = os.getenv("WHOP_PLAN_EXTRA_CLIENT", "")

PLAN_METADATA = {
    WHOP_PLAN_STARTER: {"id": "starter", "name": "Starter", "max_clients": 3,  "base_price": 97},
    WHOP_PLAN_GROWTH:  {"id": "growth",  "name": "Growth",  "max_clients": 10, "base_price": 297},
    WHOP_PLAN_SCALE:   {"id": "scale",   "name": "Scale",   "max_clients": 25, "base_price": 497},
}
# Drop an empty-string key if any plan env var is unset, so a blank plan id
# from an unmapped membership never accidentally matches a configured plan.
PLAN_METADATA.pop("", None)

# Only the Scale plan supports extra client slots
EXTRA_CLIENTS_ALLOWED_PLANS = {"scale"}

# Whop membership statuses that grant access. "canceling" = set to cancel at
# period end but still valid until then.
ACTIVE_STATUSES = {"active", "trialing", "past_due", "canceling"}

# ── SDK client ─────────────────────────────────────────────────────────────────

def _whop() -> AsyncWhop:
    if not WHOP_API_KEY:
        raise HTTPException(status_code=500, detail="Billing is not configured (missing WHOP_API_KEY).")
    return AsyncWhop(api_key=WHOP_API_KEY, webhook_key=WHOP_WEBHOOK_SECRET or None)


def _require_company_id() -> str:
    if not WHOP_COMPANY_ID:
        raise HTTPException(status_code=500, detail="Promo codes are not configured (missing WHOP_COMPANY_ID).")
    return WHOP_COMPANY_ID


def _whop_reason(e: Exception) -> str:
    """Whop's own explanation, dug out of the SDK error.

    Whop answers a bad request with a JSON body naming the exact problem —
    "Actor is missing all required permissions: promo_code:basic:read", or
    "This Bot was not found" for a company id the key cannot act for. That
    sentence is the whole diagnosis, so it must not be swallowed.
    """
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    return str(e)


def _whop_http_error(e: Exception, action: str) -> HTTPException:
    """Turn an SDK exception into the status and message the caller deserves.

    Everything here used to collapse into a bare 502 "Could not reach Whop",
    which was both unactionable and untrue: we reach Whop fine, and it tells us
    precisely what is wrong. A 502 now means only what it says — the request
    never got an answer.
    """
    if isinstance(e, APIConnectionError):
        logger.error(f"Whop {action} could not connect: {e}", exc_info=True)
        return HTTPException(status_code=502, detail=f"Could not reach Whop to {action}.")

    status_code = getattr(e, "status_code", None)
    if isinstance(e, APIStatusError) or status_code is not None:
        reason = _whop_reason(e)
        logger.error(f"Whop {action} failed ({status_code}): {reason}")
        # 401/403 from Whop is about Birdy's own API key, not the admin's
        # session — passing it through would bounce them to /login, which is
        # the one place the problem cannot be fixed.
        status = 502 if status_code in (401, 403) else 400
        return HTTPException(status_code=status, detail=f"Whop rejected the request: {reason}")

    logger.error(f"Whop {action} failed: {e}", exc_info=True)
    return HTTPException(status_code=502, detail=f"Could not {action}: {e}")


def _targetable_plans() -> list[dict]:
    """The admin-facing list of plans a promo code can be scoped to: the
    subscription tiers + extra-client add-on (this module) and the credit
    top-up packs (credits.py). Lazy-imports credits for the same reason
    _handle_payment does — sidesteps an import cycle at module load time."""
    from credits import TOPUP_PACKS, _pack_plan_id

    targets = []
    for plan_id, meta in PLAN_METADATA.items():
        targets.append({"plan_id": plan_id, "label": meta["name"], "group": "subscription"})
    if WHOP_PLAN_EXTRA_CLIENT:
        targets.append({"plan_id": WHOP_PLAN_EXTRA_CLIENT, "label": "Extra client slot", "group": "subscription"})
    for pack in TOPUP_PACKS:
        plan_id = _pack_plan_id(pack)
        if plan_id:
            targets.append({"plan_id": plan_id, "label": f"{pack['credits']:,} credits pack", "group": "credits"})
    return targets


async def list_promo_codes() -> list:
    company_id = _require_company_id()
    try:
        async with _whop() as whop:
            return [code async for code in whop.promo_codes.list(company_id=company_id)]
    except HTTPException:
        raise
    except Exception as e:
        raise _whop_http_error(e, "list promo codes") from e


async def create_promo_code(**kwargs):
    kwargs.setdefault("company_id", _require_company_id())
    try:
        async with _whop() as whop:
            return await whop.promo_codes.create(**kwargs)
    except HTTPException:
        raise
    except Exception as e:
        raise _whop_http_error(e, "create the promo code") from e


async def delete_promo_code(promo_id: str) -> bool:
    _require_company_id()
    try:
        async with _whop() as whop:
            return bool(await whop.promo_codes.delete(promo_id))
    except HTTPException:
        raise
    except Exception as e:
        raise _whop_http_error(e, "delete the promo code") from e


async def cancel_membership(membership_id: str, *, immediate: bool = True):
    """Cancel one Whop membership.

    ``immediate`` revokes access now; the alternative bills to the end of the
    current period and stops renewing. Account deletion wants immediate — the
    account it belongs to is about to stop existing, so "keep access until the
    period ends" would mean billing nobody for access nobody can use.

    Needs the `membership:cancel` scope on WHOP_API_KEY. If the key is missing
    it, Whop answers 403 and `_whop_http_error` surfaces its sentence verbatim
    rather than a generic failure — that distinction matters here, because the
    caller has to be able to tell "this subscription is now cancelled" from
    "Birdy is not allowed to cancel it", and only the first makes deletion safe.
    """
    try:
        async with _whop() as whop:
            return await whop.memberships.cancel(
                membership_id,
                cancellation_mode="immediate" if immediate else "at_period_end",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise _whop_http_error(e, f"cancel membership {membership_id}") from e


async def uncancel_membership(membership_id: str):
    """Reverse a pending cancellation — the counterpart to cancel_membership
    with immediate=False. Needs the `member:manage` scope."""
    try:
        async with _whop() as whop:
            return await whop.memberships.uncancel(membership_id)
    except HTTPException:
        raise
    except Exception as e:
        raise _whop_http_error(e, f"reactivate membership {membership_id}") from e


def _membership_ids(sub: dict) -> list[str]:
    """Every Whop membership backing one Birdy account, base plan first.

    The extra-client-slot add-on is its own membership. Anything that ends a
    subscription has to end both, or the add-on keeps billing against a plan
    that no longer exists.
    """
    return [
        mid for mid in (
            (sub or {}).get("whop_membership_id"),
            (sub or {}).get("whop_extra_membership_id"),
        ) if mid
    ]


def _secret_fingerprint() -> str:
    """A short, non-reversible fingerprint of the configured webhook secret, so
    a config mismatch (wrong value / not redeployed) can be diagnosed from logs
    WITHOUT ever logging the secret itself. Compare it to the expected value:
        printf %s 'the-sandbox-signing-secret' | \
          python3 -c "import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:12])"
    """
    if not WHOP_WEBHOOK_SECRET:
        return "UNSET"
    return hashlib.sha256(WHOP_WEBHOOK_SECRET.encode()).hexdigest()[:12]


def _candidate_hmac_keys():
    """Every plausible HMAC key derivable from the configured webhook secret.

    Whop's ``ws_<hex>`` secret can be consumed as the raw string, as the 32
    bytes it hex-encodes, or (for ``whsec_`` secrets) as base64 — and Whop's
    own tooling isn't consistent about which. All derivations come from the
    same legitimate secret, so trying each keeps verification secure while being
    robust to the format Whop actually signs with.
    """
    secret = WHOP_WEBHOOK_SECRET
    keys = [("raw", secret.encode("utf-8"))]  # @whop/api makeWebhookValidator uses the raw string
    if secret.startswith("ws_"):
        try:
            keys.append(("hex", bytes.fromhex(secret[3:])))  # ws_<hex> -> 32 raw bytes
        except ValueError:
            pass
    b64_part = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        keys.append(("base64", base64.b64decode(b64_part + "==")))  # Standard Webhooks default
    except Exception:
        pass
    return keys


def _verify_webhook_signature(headers: dict, body: str) -> None:
    """Verify a Whop webhook signature, raising on any mismatch.

    Whop signs with the account webhook secret, but the framing depends on the
    delivery:

      * ``x-whop-signature: t=<ts>,v1=<hex>`` — HMAC-SHA256 over ``"{t}.{body}"``
        keyed by the raw secret string (matches @whop/api makeWebhookValidator).
      * Standard Webhooks headers (``webhook-id`` / ``webhook-timestamp`` /
        ``webhook-signature: v1,<base64>``) — HMAC over ``"{id}.{ts}.{body}"``,
        base64-encoded.

    Whop's ``ws_...`` secret isn't the base64 vanilla Standard Webhooks assumes,
    so for the base64 header we try each plausible key derivation and accept the
    first that matches (logging which one, so it can be pinned later). The
    handler is idempotent, so the ±300s replay window is intentionally skipped.
    """
    lower = {k.lower(): v for k, v in headers.items()}

    # ── Whop-native scheme: x-whop-signature ──
    xwhop = lower.get("x-whop-signature")
    if xwhop:
        fields = dict(p.split("=", 1) for p in xwhop.split(",") if "=" in p)
        ts, sent_sig = fields.get("t"), fields.get("v1")
        if not ts or not sent_sig:
            raise ValueError(f"malformed x-whop-signature: {xwhop!r}")
        for name, key in _candidate_hmac_keys():
            expected = hmac.new(key, f"{ts}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, sent_sig.strip()):
                if name != "raw":
                    logger.info("Whop webhook verified (x-whop-signature, key=%s)", name)
                return
        raise ValueError("x-whop-signature HMAC mismatch")

    # ── Standard Webhooks headers: webhook-signature ──
    msg_id = lower.get("webhook-id")
    msg_ts = lower.get("webhook-timestamp")
    sig_hdr = lower.get("webhook-signature")
    if msg_id and msg_ts and sig_hdr:
        signed = f"{msg_id}.{msg_ts}.{body}".encode("utf-8")
        # webhook-signature is a space-separated list of "v1,<base64>" entries.
        sent = [part.split(",", 1)[1] for part in sig_hdr.split(" ")
                if part.startswith("v1,") and "," in part]
        if not sent:
            raise ValueError(f"no v1 signature in webhook-signature: {sig_hdr!r}")
        for name, key in _candidate_hmac_keys():
            expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
            if any(hmac.compare_digest(expected, s) for s in sent):
                if name != "base64":
                    logger.info("Whop webhook verified (webhook-signature, key=%s)", name)
                return
        raise ValueError("No matching signature found")

    raise ValueError("no recognized Whop signature header (x-whop-signature / webhook-signature)")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _plan_meta(plan_id: str) -> dict:
    """Map a Whop plan id → internal plan metadata."""
    return PLAN_METADATA.get(plan_id or "", {"id": "unknown", "name": "Unknown", "max_clients": 0})


def _db(mongo_client):
    return mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]


async def _get_sub(user_id: str, mongo_client) -> Optional[dict]:
    user = await _db(mongo_client)["users"].find_one(
        {"user_id": user_id},
        projection={"subscription": 1, "_id": 0},
    )
    return user.get("subscription") if user else None


async def _save_sub(user_id: str, sub: dict, mongo_client):
    await _db(mongo_client)["users"].update_one(
        {"user_id": user_id},
        {"$set": {"subscription": sub, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def _client_count(user_id: str, mongo_client) -> int:
    return await _db(mongo_client)["client_groups"].count_documents({"user_id": user_id})


def _iso(value) -> Optional[str]:
    """Normalise a Whop timestamp (datetime, unix seconds, or ISO string) to ISO-8601."""
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ── GET /api/billing/status ───────────────────────────────────────────────────

@router.get("/api/billing/status")
async def billing_status(current_user: str = Depends(get_current_user)):
    user_id = current_user

    async with get_mongo_client() as mc:
        sub   = await _get_sub(user_id, mc)
        count = await _client_count(user_id, mc)

        if not sub or sub.get("status") not in ACTIVE_STATUSES:
            return {
                "subscribed": False,
                "plan": {"id": "free", "name": "Free", "max_clients": 0},
                "status": "inactive",
                "client_count": count,
                "client_limit": 0,
                "extra_clients_paid": 0,
                "can_add_extra_slots": False,
                "current_period_end": None,
                "cancel_at_period_end": False,
                "whop_membership_id": None,
                "_user_id": user_id,
            }

        plan = {
            "id":          sub.get("plan_id", "unknown"),
            "name":        sub.get("plan_name", "Unknown"),
            "max_clients": sub.get("max_clients", 0),
        }
        extra_paid = sub.get("extra_clients_paid", 0) if plan["id"] in EXTRA_CLIENTS_ALLOWED_PLANS else 0

        return {
            "subscribed": True,
            "plan": plan,
            "status": sub.get("status"),
            "client_count": count,
            "client_limit": plan["max_clients"] + extra_paid,
            "extra_clients_paid": extra_paid,
            "can_add_extra_slots": plan["id"] in EXTRA_CLIENTS_ALLOWED_PLANS,
            "current_period_end": sub.get("current_period_end"),
            "cancel_at_period_end": sub.get("cancel_at_period_end", False),
            "whop_membership_id": sub.get("whop_membership_id"),
            "_user_id": user_id,
        }


# ── GET /api/billing/portal-url ───────────────────────────────────────────────

@router.get("/api/billing/portal-url")
async def portal_url(current_user: str = Depends(get_current_user)):
    """Return the Whop-hosted URL where the customer can manage their membership
    (change plan, update payment method, cancel)."""
    user_id = current_user

    async with get_mongo_client() as mc:
        sub = await _get_sub(user_id, mc)
        if not sub or not sub.get("whop_membership_id"):
            raise HTTPException(status_code=400, detail="No active subscription found")
        membership_id = sub["whop_membership_id"]
        stored_url    = sub.get("manage_url")

    # Prefer a freshly fetched manage_url; fall back to the one captured at webhook time.
    url = stored_url
    try:
        async with _whop() as whop:
            membership = await whop.memberships.retrieve(membership_id)
        url = getattr(membership, "manage_url", None) or stored_url
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Whop portal fetch failed: {e}", exc_info=True)

    if not url:
        raise HTTPException(status_code=500, detail="Manage URL not available yet. Please try again shortly.")
    return {"portal_url": url}


# ── POST /api/billing/cancel ──────────────────────────────────────────────────

@router.post("/api/billing/cancel")
async def cancel_subscription(current_user: str = Depends(get_current_user)):
    """Cancel the signed-in user's subscription, in place.

    At period end, not immediately: they have already paid for the current
    period, and revoking access the moment they click cancel would be taking
    back something they own. `cancel_at_period_end` is what the UI reads to say
    "Cancels on <date>", and the plan keeps working until then.

    This used to be a trip to Whop's hosted portal, on the belief that Whop had
    no cancel API. It has one. The portal is still where plan changes and
    payment methods live — those genuinely need it — but ending a subscription
    does not, and sending someone to a third-party page to do the one thing
    they came to Settings for was the worst part of this screen.

    The local mirror is updated here rather than waited on: users.subscription
    is fed by webhooks and nothing reconciles it, so leaving the flag to arrive
    on its own would show a customer their cancellation had not registered.
    """
    async with get_mongo_client() as mc:
        sub = await _get_sub(current_user, mc)
        if not sub or sub.get("status") not in ACTIVE_STATUSES:
            raise HTTPException(status_code=400, detail="No active subscription to cancel.")

        # Idempotent: asking twice is a double-click or a stale tab, not an error.
        if sub.get("cancel_at_period_end"):
            return {
                "cancelled": True,
                "cancel_at_period_end": True,
                "current_period_end": sub.get("current_period_end"),
            }

        membership_ids = _membership_ids(sub)
        if not membership_ids:
            raise HTTPException(
                status_code=409,
                detail=(
                    "We can't find your Whop membership to cancel it. Please contact "
                    "support and we'll sort it out."
                ),
            )

        for membership_id in membership_ids:
            await cancel_membership(membership_id, immediate=False)

        sub["cancel_at_period_end"] = True
        await _save_sub(current_user, sub, mc)

    logger.info(f"{current_user} cancelled memberships {membership_ids} at period end")
    return {
        "cancelled": True,
        "cancel_at_period_end": True,
        "current_period_end": sub.get("current_period_end"),
    }


# ── POST /api/billing/reactivate ──────────────────────────────────────────────

@router.post("/api/billing/reactivate")
async def reactivate_subscription(current_user: str = Depends(get_current_user)):
    """Undo a scheduled cancellation, while the plan is still running."""
    async with get_mongo_client() as mc:
        sub = await _get_sub(current_user, mc)
        if not sub or not sub.get("cancel_at_period_end"):
            raise HTTPException(
                status_code=400, detail="You don't have a scheduled cancellation to undo."
            )

        membership_ids = _membership_ids(sub)
        if not membership_ids:
            raise HTTPException(
                status_code=409,
                detail=(
                    "We can't find your Whop membership to reactivate it. Please "
                    "contact support and we'll sort it out."
                ),
            )

        for membership_id in membership_ids:
            await uncancel_membership(membership_id)

        sub["cancel_at_period_end"] = False
        await _save_sub(current_user, sub, mc)

    logger.info(f"{current_user} reactivated memberships {membership_ids}")
    return {"cancel_at_period_end": False}


# ── POST /api/billing/webhook ─────────────────────────────────────────────────

@router.post("/api/billing/webhook")
async def whop_webhook(request: Request):
    raw = await request.body()
    payload_str = raw.decode("utf-8")

    if WHOP_WEBHOOK_SECRET:
        try:
            _verify_webhook_signature(dict(request.headers), payload_str)
        except Exception as e:
            present = {k.lower() for k in request.headers}
            logger.warning(
                "Whop webhook signature verification FAILED: %s "
                "| backend secret_fp=%s secret_len=%d body_len=%d "
                "| headers present: x-whop-signature=%s webhook-signature=%s",
                e, _secret_fingerprint(), len(WHOP_WEBHOOK_SECRET), len(payload_str),
                "x-whop-signature" in present, "webhook-signature" in present,
            )
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        logger.warning("Whop webhook received but WHOP_WEBHOOK_SECRET is not set — skipping verification")

    try:
        event = json.loads(payload_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Whop's event type lives in `type` (newer) or `action` (older API versions).
    event_type = event.get("type") or event.get("action") or ""
    data       = event.get("data") or {}

    logger.info(f"📦 Whop webhook: {event_type}")

    async with get_mongo_client() as mc:
        if event_type.startswith("membership."):
            await _handle_membership(data, mc)
        elif event_type in ("payment.succeeded", "payment_succeeded"):
            await _handle_payment(data, mc)
        elif event_type.startswith("payment."):
            logger.info(f"💳 Whop payment {event_type}: {data.get('id')}")

    # Upserts are idempotent (rebuilt from full membership state), so Whop's
    # at-least-once redelivery is safe to ack unconditionally.
    return {"success": True}


async def _resolve_user_id(data: dict, mongo_client) -> Optional[str]:
    """Resolve the birdy account (user_id == email) that a Whop membership belongs to."""
    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")
    if user_id:
        return user_id

    email = (data.get("user") or {}).get("email")
    if email:
        user = await _db(mongo_client)["users"].find_one(
            {"user_id": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
            projection={"user_id": 1, "_id": 0},
        )
        if user:
            return user["user_id"]
        # user_id IS the email in this system — use it directly if the account
        # row cannot be found (e.g. it will be upserted).
        return email

    return None


async def _handle_payment(data: dict, mongo_client):
    """Credit a purchased Birdy Credits top-up. Fires on Whop ``payment.succeeded``.

    Only the configured top-up plans (``WHOP_PLAN_TOPUP_*``) grant credits here.
    A subscription's recurring payment carries a *subscription* plan id, which
    maps to 0 top-up credits and is skipped — the membership webhook already
    tracks the subscription (and its monthly allowance) on its own.

    ``add_topup`` dedups on the Whop payment id, so Whop's at-least-once
    redelivery credits the account exactly once.
    """
    # Lazy import: keeps billing.py importable without the credits module loaded,
    # and sidesteps any import cycle at module load time.
    from credits import topup_plan_credits, add_topup

    payment_id   = data.get("id")
    plan_id      = (data.get("plan") or {}).get("id", "")
    pack_credits = topup_plan_credits(plan_id)

    if not pack_credits:
        logger.info(f"💳 Whop payment {payment_id}: plan {plan_id or '—'} is not a top-up pack; skipping credit")
        return
    if not payment_id:
        logger.error("Whop payment webhook missing id; cannot credit a top-up idempotently")
        return

    user_id = await _resolve_user_id(data, mongo_client)
    if not user_id:
        # Existing subscribers may check out while logged into Whop without an
        # email on the payment — fall back to the Whop user id linked on their sub.
        whop_user_id = (data.get("user") or {}).get("id")
        if whop_user_id:
            row = await _db(mongo_client)["users"].find_one(
                {"subscription.whop_user_id": whop_user_id},
                projection={"user_id": 1, "_id": 0},
            )
            user_id = row.get("user_id") if row else None
    if not user_id:
        logger.error(f"Whop payment {payment_id}: cannot resolve user for top-up (plan {plan_id})")
        return

    await add_topup(_db(mongo_client), user_id, pack_credits, payment_id)


async def _handle_membership(data: dict, mongo_client):
    membership_id = data.get("id")
    whop_plan_id  = (data.get("plan") or {}).get("id", "")
    status        = data.get("status", "")

    user_id = await _resolve_user_id(data, mongo_client)
    if not user_id:
        logger.error(f"Whop webhook: cannot resolve user for membership {membership_id} (plan {whop_plan_id})")
        return

    # Extra-client-slot add-on membership → adjust the slot count, leave the base plan alone.
    if WHOP_PLAN_EXTRA_CLIENT and whop_plan_id == WHOP_PLAN_EXTRA_CLIENT:
        await _handle_extra_slots(user_id, membership_id, data, status, mongo_client)
        return

    if whop_plan_id not in PLAN_METADATA:
        logger.warning(f"Whop webhook: membership {membership_id} has unmapped plan {whop_plan_id}; ignoring")
        return

    meta = _plan_meta(whop_plan_id)

    if status not in ACTIVE_STATUSES:
        await _deactivate(user_id, membership_id, status, mongo_client)
        return

    existing = await _get_sub(user_id, mongo_client) or {}
    extra_paid = existing.get("extra_clients_paid", 0) if meta["id"] in EXTRA_CLIENTS_ALLOWED_PLANS else 0

    sub_doc = {
        "whop_membership_id":   membership_id,
        "whop_user_id":         (data.get("user") or {}).get("id"),
        "whop_plan_id":         whop_plan_id,
        "plan_id":              meta["id"],
        "plan_name":            meta["name"],
        "status":               status,
        "max_clients":          meta["max_clients"],
        "extra_clients_paid":   extra_paid,
        "current_period_end":   _iso(data.get("renewal_period_end")),
        "cancel_at_period_end": bool(data.get("cancel_at_period_end", False)),
        "manage_url":           data.get("manage_url"),
        "updated_at":           datetime.now(timezone.utc),
    }
    # Preserve the extra-slot add-on membership link across base-plan rewrites.
    if meta["id"] in EXTRA_CLIENTS_ALLOWED_PLANS and existing.get("whop_extra_membership_id"):
        sub_doc["whop_extra_membership_id"] = existing["whop_extra_membership_id"]

    await _save_sub(user_id, sub_doc, mongo_client)
    logger.info(f"✅ Whop membership {membership_id} → {user_id} ({meta['name']}, {status}, extra={extra_paid})")


async def _handle_extra_slots(user_id: str, membership_id: str, data: dict, status: str, mongo_client):
    """A membership on the dedicated 'extra client slot' plan tracks how many
    additional Scale slots the user has purchased."""
    users = _db(mongo_client)["users"]

    if status in ACTIVE_STATUSES:
        try:
            qty = int(data.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        await users.update_one(
            {"user_id": user_id},
            {"$set": {
                "subscription.extra_clients_paid":      max(qty, 0),
                "subscription.whop_extra_membership_id": membership_id,
                "subscription.updated_at":              datetime.now(timezone.utc),
            }},
        )
        logger.info(f"✅ Whop extra-slot membership {membership_id} → {user_id} (qty={qty})")
    else:
        await users.update_one(
            {"user_id": user_id, "subscription.whop_extra_membership_id": membership_id},
            {"$set": {
                "subscription.extra_clients_paid": 0,
                "subscription.updated_at":         datetime.now(timezone.utc),
            }},
        )
        logger.info(f"❌ Whop extra-slot membership {membership_id} ended for {user_id}")


async def _deactivate(user_id: str, membership_id: str, status: str, mongo_client):
    """Mark the stored subscription inactive — but only if it is the membership
    that actually ended (a stale event for an old membership must not wipe a
    newer, active one)."""
    result = await _db(mongo_client)["users"].update_one(
        {"user_id": user_id, "subscription.whop_membership_id": membership_id},
        {"$set": {
            "subscription.status":               status or "canceled",
            "subscription.cancel_at_period_end": False,
            "subscription.updated_at":           datetime.now(timezone.utc),
        }},
    )
    if result.matched_count:
        logger.info(f"❌ Whop membership {membership_id} → {user_id} is now {status or 'canceled'}")
