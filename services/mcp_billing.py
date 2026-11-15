"""Credits for external MCP requests: a flat per-call price, not per token (the markup is not applied)."""

import logging

from credits import _apply_charge, _available, _load_and_sync, get_credits_settings

logger = logging.getLogger(__name__)

OUT_OF_CREDITS_MESSAGE = (
    "You're out of Birdy credits. Top up in Settings → Billing to keep using Birdy here."
)


def _parts(settings: dict, chars: int, live: bool) -> tuple[float, float, float]:
    request = float(settings["mcp_base_fee"])
    data = float(settings["mcp_per_10k_chars"]) * max(0, chars) / 10000
    live_fee = float(settings["mcp_live_fee"]) if live else 0.0
    return request, data, live_fee


def mcp_charge_amount(settings: dict, chars: int, live: bool) -> float:
    request, data, live_fee = _parts(settings, chars, live)
    return min(float(settings["mcp_max_per_call"]), round(request + data + live_fee, 2))


def _breakdown(settings: dict, chars: int, live: bool) -> dict:
    """{request, data, live, total}: the parts sum exactly to the total, scaled down together under the cap."""
    request, data, live_fee = _parts(settings, chars, live)
    total = mcp_charge_amount(settings, chars, live)
    raw = request + data + live_fee
    scale = min(1.0, total / raw) if raw > 0 else 0.0
    request_c = round(request * scale, 2)
    live_c = round(live_fee * scale, 2)
    data_c = round(total - request_c - live_c, 2)
    return {"request": request_c, "data": data_c, "live": live_c, "total": total}


async def charge_breakdown(db, chars: int, live: bool) -> dict:
    """What a call costs ({request, data, live, total}), without charging it."""
    return _breakdown(await get_credits_settings(db), chars, live)


async def charge_mcp_call(db, user_id: str, *, tool: str, grant_id: str | None, chars: int, live: bool) -> dict:
    settings = await get_credits_settings(db)
    result = _breakdown(settings, chars, live)
    if result["total"] > 0:
        await _apply_charge(
            db, user_id, result["total"],
            ledger={"source": "mcp", "feature": "mcp_request", "tool": tool, "grant_id": grant_id},
        )
    return result


async def has_credits(db, user_id: str) -> bool:
    """True when enforcement is off or the available balance is > 0. Fails open on a read error."""
    try:
        settings = await get_credits_settings(db, fresh=True)
        if not settings["enforce"]:
            return True
        credits, _ = await _load_and_sync(db, user_id)
        return _available(credits) > 0
    except Exception:
        logger.error("MCP credits check failed for %s", user_id, exc_info=True)
        return True
