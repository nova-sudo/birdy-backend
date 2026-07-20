"""
scripts/set_admin_role.py
-------------------------
Grant (or revoke) the internal Admin role on user accounts. There is no
self-serve promotion — admin is assigned deliberately by running this script
against the known internal staff emails.

Sets `role: "admin"` (or `"user"` with --revoke) on the matching `users`
documents, keyed by `user_id` (which IS the email in this system).

Run with:
    python -m scripts.set_admin_role alice@trybytes.ai bob@trybytes.ai
    python -m scripts.set_admin_role --revoke someone@trybytes.ai
    python -m scripts.set_admin_role --list
"""

import argparse
import asyncio
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


async def run(emails: list[str], revoke: bool, list_admins: bool):
    client = AsyncIOMotorClient(MONGO_URI)
    users = client[DB_NAME]["users"]

    if list_admins:
        admins = await users.find({"role": "admin"}, {"user_id": 1, "name": 1}).to_list(None)
        if not admins:
            logger.info("No admin users found.")
        for a in admins:
            logger.info("  admin: %s (%s)", a.get("user_id"), a.get("name") or "—")
        client.close()
        return

    new_role = "user" if revoke else "admin"
    for email in emails:
        result = await users.update_one({"user_id": email}, {"$set": {"role": new_role}})
        if result.matched_count == 0:
            logger.warning("No user found for %s — skipped", email)
        else:
            logger.info("Set role=%s for %s", new_role, email)

    client.close()


def main():
    parser = argparse.ArgumentParser(description="Assign/revoke the internal Admin role.")
    parser.add_argument("emails", nargs="*", help="user emails to update")
    parser.add_argument("--revoke", action="store_true", help="set role back to 'user' instead of 'admin'")
    parser.add_argument("--list", action="store_true", help="list current admins and exit")
    args = parser.parse_args()

    if not args.list and not args.emails:
        parser.error("provide at least one email, or use --list")

    asyncio.run(run(args.emails, args.revoke, args.list))


if __name__ == "__main__":
    main()
