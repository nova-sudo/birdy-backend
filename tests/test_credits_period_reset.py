"""When the monthly allowance resets: at the end of the period in use, never on a plan change."""

from datetime import datetime, timezone

import pytest

import credits

USER = "dana@example.com"


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def sub(plan: str, end: str, status: str = "active") -> dict:
    return {"plan_id": plan, "status": status, "current_period_end": f"{end}T00:00:00+00:00"}


def roll(stored: dict, subscription: dict, now: str) -> dict:
    out, _ = credits._effective_credits({"credits": stored, "subscription": subscription}, now=at(now))
    return out


def start(subscription: dict, now: str, used: float) -> dict:
    c = roll({}, subscription, now)
    c["used"] = used
    return c


def test_switching_plans_back_and_forth_never_resets_the_allowance():
    c = start(sub("starter", "2026-12-10"), "2026-11-20", used=1900.0)   # 100 of 2,000 left

    c = roll(c, sub("scale", "2026-12-26"), "2026-11-26")                 # upgrade: new membership, new end
    assert c["used"] == 1900.0 and c["allowance"] == 10000
    c["used"] = 9900.0                                                    # spends almost all of Scale's

    c = roll(c, sub("starter", "2026-12-27"), "2026-11-27")               # downgrade the next day
    assert c["used"] == 9900.0 and credits._available(c) == 0             # nothing fresh to spend

    c = roll(c, sub("starter", "2026-12-27"), "2026-12-11")               # the old Starter end has passed
    assert c["used"] == 9900.0                                            # still no reset: the current plan's cycle rules

    c = roll(c, sub("starter", "2027-01-27"), "2026-12-27")               # the current plan's period ends
    assert c["used"] == 0.0 and c["allowance"] == 2000


def test_renewal_resets_when_the_period_ends():
    c = start(sub("growth", "2026-12-10"), "2026-11-20", used=500.0)
    assert roll(c, sub("growth", "2026-12-10"), "2026-12-09")["used"] == 500.0
    c = roll(c, sub("growth", "2027-01-10"), "2026-12-10")
    assert c["used"] == 0.0 and c["resets_at"].startswith("2027-01-10")


def test_a_renewal_webhook_that_lands_early_still_resets_on_time():
    c = start(sub("growth", "2026-12-10"), "2026-11-20", used=500.0)
    c = roll(c, sub("growth", "2027-01-10"), "2026-12-09")    # Whop sends the new period a day early
    assert c["used"] == 500.0 and c["resets_at"].startswith("2026-12-10")
    c = roll(c, sub("growth", "2027-01-10"), "2026-12-10")    # no further webhook needed
    assert c["used"] == 0.0 and c["resets_at"].startswith("2027-01-10")


def test_cancelling_and_resubscribing_mid_period_keeps_what_was_used():
    c = start(sub("growth", "2026-12-10"), "2026-11-20", used=5000.0)
    c = roll(c, sub("growth", "2026-12-10", status="canceled"), "2026-11-21")
    c = roll(c, sub("growth", "2026-12-22"), "2026-11-22")
    assert c["used"] == 5000.0 and c["allowance"] == 6000


def test_no_subscription_resets_with_the_calendar_month():
    c = start({}, "2026-11-20", used=3.0)
    assert roll(c, {}, "2026-11-30")["used"] == 3.0
    c = roll(c, {}, "2026-12-01")
    assert c["used"] == 0.0 and c["period_key"] == "2026-12"


def test_a_stale_period_end_does_not_reset_on_every_read():
    # The subscription says "active" but its period ended (a missed webhook): one reset, then hold.
    c = start(sub("growth", "2026-12-10"), "2026-11-20", used=500.0)
    c = roll(c, sub("growth", "2026-12-10"), "2026-12-15")
    assert c["used"] == 0.0 and c["resets_at"] is None
    c["used"] = 700.0
    assert roll(c, sub("growth", "2026-12-10"), "2026-12-20")["used"] == 700.0
    assert roll(c, sub("growth", "2027-01-10"), "2026-12-21")["used"] == 0.0    # the next real period resets


def test_balances_written_before_this_change_keep_working():
    legacy = {"allowance": 6000, "used": 1200.0, "topup_balance": 50.0, "credited_topup_ids": ["p1"],
              "period_key": "2026-12-10T00:00:00+00:00", "rev": 4}
    c = roll(legacy, sub("growth", "2026-12-10"), "2026-11-25")
    assert c["used"] == 1200.0 and c["resets_at"].startswith("2026-12-10") and c["rev"] == 4
    old = {**legacy, "period_key": "2026-10-10T00:00:00+00:00"}
    c = roll(old, sub("growth", "2026-12-10"), "2026-11-25")                    # its period is long over
    assert c["used"] == 0.0 and c["topup_balance"] == 50.0 and c["credited_topup_ids"] == ["p1"]


@pytest.mark.asyncio
async def test_a_plan_switch_is_persisted_without_a_reset(mock_db):
    await mock_db["users"].insert_one({"user_id": USER, "subscription": sub("starter", "2099-12-10"),
                                       "credits": start(sub("starter", "2099-12-10"), "2099-11-20", used=1900.0)})
    await mock_db["users"].update_one({"user_id": USER}, {"$set": {"subscription": sub("scale", "2099-12-26")}})
    c, _ = await credits._load_and_sync(mock_db, USER)
    stored = (await mock_db["users"].find_one({"user_id": USER}))["credits"]
    assert c["used"] == stored["used"] == 1900.0
    assert stored["allowance"] == 10000 and stored["plan_id"] == "scale"
