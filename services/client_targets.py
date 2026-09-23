"""
services/client_targets.py
--------------------------
The monthly goals a client group is measured against, and the agency-wide
defaults new clients inherit.

One canonical field list, because there are three writers — the onboarding
wizard's KPI step, the Targets tab in the client settings modal, and the
default-seeding below — and they disagreed once already. The wizard sent the
cost box as `cpa` when no such field existed, the unknown name took the whole
request down with it, and `monthly_wins` was lost in the same failure. That
field is what the weekly health pass measures a client against, and a client
with no target has nothing to miss, so it resolved to Healthy: the account
stopped being monitored and nothing said so.

`cpa` and `cpl` are both real and both stored. They are not the same number
and must not be folded into one: cost per lead is spend ÷ leads, cost per
acquisition is spend ÷ clients actually won, and they differ by the width of
the funnel. Storing an agency's CPA answer under `cpl` reports a target they
never set, several times smaller than the one they did.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Every stored target, in the order the Targets tab lists them. The API's
# request model and the agency defaults below are both built from this, so a
# new goal is added in one place.
TARGET_FIELDS = (
    "cpl", "cpa", "monthly_wins", "monthly_revenue",
    "conversion_rate", "monthly_spend", "aov",
)

# Fields that are not goals and must never be treated as one when deciding
# whether a client has been given any target at all.
_NON_GOAL_KEYS = {"updated_at"}


def clean(targets: Optional[dict]) -> dict:
    """The goal fields of a stored `targets` object, dropping bookkeeping keys
    and anything unset."""
    if not targets:
        return {}
    return {
        field: targets[field]
        for field in TARGET_FIELDS
        if field not in _NON_GOAL_KEYS and targets.get(field) is not None
    }


async def defaults_for(user_id: str, db: Any) -> dict:
    """The agency's saved default targets, shaped as a new client group's
    ``targets``.

    The wizard's "Save these as your defaults?" step says, in as many words,
    that every new client will be pre-filled with these numbers. Nothing read
    them: they were written to ``users.default_targets`` and never looked at
    again, so an agency that set a monthly-wins target and then imported
    twenty-four sub-accounts got twenty-four clients with no target, no
    expectation to miss, and a Healthy badge each.

    Degrades to ``{}`` — a client with no targets is the old behaviour and is
    recoverable from the Targets tab; failing the creation it is attached to
    is not.
    """
    try:
        user = await db["users"].find_one({"user_id": user_id}, {"default_targets": 1, "_id": 0})
    except Exception as e:
        logger.warning(f"default targets lookup failed for {user_id}: {e}")
        return {}
    return clean((user or {}).get("default_targets"))
