"""
scripts/reset_subscription.py
-----------------------------
Clear the stored `subscription` on user accounts, keyed by `user_id` (which IS
the email in this system).

Use this to remove a stale/legacy subscription doc — e.g. a Paddle-era record
that has an active status but no `whop_membership_id`, which reads as
"subscribed" on /api/billing/status yet 400s on /api/billing/portal-url, or a
sandbox test membership left over after switching Whop to production (its
`manage_url` points at sandbox.whop.com). After clearing, the account can
re-subscribe cleanly through Whop checkout.

⚠️  This revokes the account's access until it re-subscribes. Do NOT run it on a
    live paying customer unless you intend exactly that. Sandbox test
    memberships are NOT real paid subscriptions, so clearing those is safe.

Run with:
    python -m scripts.reset_subscription --show hello@soupgrowth.com     # inspect only
    python -m scripts.reset_subscription hello@soupgrowth.com            # clear one
    python -m scripts.reset_subscription --show --all-sandbox            # preview all sandbox leftovers
    python -m scripts.reset_subscription --all-sandbox                   # clear every sandbox test sub
"""

import argparse
import asyncio
import json
import logging
import os
import sys

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient
from core.database import DB_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


def _summary(sub: dict | None) -> str:
    if not sub:
        return "no subscription"
    return (
        f"plan={sub.get('plan_id') or sub.get('plan_name')} "
        f"status={sub.get('status')} "
        f"whop_membership_id={sub.get('whop_membership_id')} "
        f"paddle_subscription_id={sub.get('paddle_subscription_id')} "
        f"extra_clients_paid={sub.get('extra_clients_paid', 0)}"
    )


async def _clear_one(users, email: str, sub: dict | None, show_only: bool):
    logger.info("%s → current: %s", email, _summary(sub))
    if show_only:
        if sub:
            logger.info("%s → full subscription doc:\n%s", email, json.dumps(sub, indent=2, default=str))
        return
    if not sub:
        logger.info("%s → nothing to clear", email)
        return
    result = await users.update_one({"user_id": email}, {"$unset": {"subscription": ""}})
    if result.modified_count:
        logger.info("%s → subscription cleared ✅ (can now re-subscribe via Whop)", email)
    else:
        logger.warning("%s → no change applied", email)


async def run(emails: list[str], show_only: bool, all_sandbox: bool):
    client = AsyncIOMotorClient(MONGO_URI)
    users = client[DB_NAME]["users"]

    if all_sandbox:
        # Every account whose stored manage_url is a sandbox.whop.com link — i.e.
        # a sandbox test membership left behind by the production cutover.
        query = {"subscription.manage_url": {"$regex": r"sandbox\.whop\.com", "$options": "i"}}
        docs = await users.find(query, {"user_id": 1, "subscription": 1, "_id": 0}).to_list(None)
        logger.info("Found %d account(s) with a sandbox subscription", len(docs))
        for doc in docs:
            await _clear_one(users, doc["user_id"], doc.get("subscription"), show_only)
    else:
        for email in emails:
            doc = await users.find_one({"user_id": email}, {"subscription": 1, "_id": 0})
            if doc is None:
                logger.warning("No user found for %s — skipped", email)
                continue
            await _clear_one(users, email, doc.get("subscription"), show_only)

    client.close()


def main():
    parser = argparse.ArgumentParser(description="Clear a stale subscription doc from user accounts.")
    parser.add_argument("emails", nargs="*", help="account emails (user_id) to reset")
    parser.add_argument("--show", action="store_true", help="print the current subscription and exit without changing anything")
    parser.add_argument("--all-sandbox", action="store_true", help="target every account whose subscription manage_url is a sandbox.whop.com link")
    args = parser.parse_args()

    if not args.emails and not args.all_sandbox:
        parser.error("provide one or more emails, or --all-sandbox")

    asyncio.run(run(args.emails, args.show, args.all_sandbox))


if __name__ == "__main__":
    main()
