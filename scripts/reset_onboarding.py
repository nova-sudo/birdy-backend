"""
scripts/reset_onboarding.py
---------------------------
Put an account back at the first onboarding step, for a dry run of the wizard.

Two things have to go for the wizard to be worth walking:

    users.onboarding    reset to step 0. Setting it to `None` is not enough —
                        /api/onboarding/status grandfathers a null back to
                        "completed" the moment the account has any client group
                        or a GHL connection, which is every account worth
                        testing on.
    client_groups       removed. `import_subaccounts` skips a location that
                        already has a group, so leaving them makes the
                        sub-account review step offer nothing to import — which
                        is the one step most worth dry-running.

**Contacts and leads are deliberately left alone.** `ghl_contacts` upserts on
(user_id, location_id, contact_id), not on the client group, so a re-import
re-attaches the existing rows to the new groups rather than duplicating them.
Deleting 65,000 contacts to re-download them would be a slow, risky no-op.

Integrations are left connected by default, so a dry run lands straight in the
flow being tested instead of re-doing two OAuth hops. `--disconnect` clears them
for a genuinely blank-account run.

Every deleted document is written to a backup file first, and `--restore` puts
them back. That is not optional: the group documents carry the Meta cache,
targets and per-client settings, and rebuilding those means a full refresh cycle
per client.

    python -m scripts.reset_onboarding --user someone@example.com --dry-run
    python -m scripts.reset_onboarding --user someone@example.com --confirm
    python -m scripts.reset_onboarding --restore backups/reset_someone_2026....json
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")

FRESH_ONBOARDING = {"completed": False, "step": 0, "data": {}}


async def _snapshot(db, user_id: str) -> dict:
    """Everything this reset will change, as it is right now."""
    user = await db["users"].find_one({"user_id": user_id})
    if not user:
        raise SystemExit(f"No such account: {user_id}")
    groups = await db["client_groups"].find({"user_id": user_id}).to_list(None)
    return {
        "user_id": user_id,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "onboarding": user.get("onboarding"),
        "integrations": user.get("integrations"),
        "client_groups": groups,
    }


async def reset(db, user_id: str, disconnect: bool, dry_run: bool) -> None:
    snapshot = await _snapshot(db, user_id)
    groups = snapshot["client_groups"]

    # What survives, said out loud — the counts are the reassurance that this is
    # a reset and not a data loss event.
    contacts = await db["ghl_contacts"].count_documents({"user_id": user_id})
    leads = await db["facebook_leads"].count_documents({"user_id": user_id})

    logger.info("Account            : %s", user_id)
    # Just the shape of it — the full document carries the whole sub-account
    # review payload and is hundreds of lines.
    current = snapshot["onboarding"] or {}
    logger.info("Onboarding now     : completed=%s step=%s",
                current.get("completed"), current.get("step"))
    logger.info("Client groups      : %d  (will be removed)", len(groups))
    logger.info("Integrations       : %s", "will be cleared" if disconnect else "left connected")
    logger.info("ghl_contacts       : %d  (kept — re-attached on re-import)", contacts)
    logger.info("facebook_leads     : %d  (kept)", leads)
    for group in groups:
        logger.info("    - %s", group.get("name"))

    if dry_run:
        logger.info("")
        logger.info("Dry run. Nothing changed. Re-run with --confirm to apply.")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_user = "".join(ch if ch.isalnum() else "_" for ch in user_id)
    path = os.path.join(BACKUP_DIR, f"reset_{safe_user}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json_util.dumps(snapshot, indent=2))
    logger.info("")
    logger.info("Backup written     : %s", path)

    changes = {"onboarding": FRESH_ONBOARDING, "updated_at": datetime.now(timezone.utc)}
    if disconnect:
        changes["integrations"] = {}
    await db["users"].update_one({"user_id": user_id}, {"$set": changes})

    removed = await db["client_groups"].delete_many({"user_id": user_id})

    logger.info("Removed %d client groups; onboarding is back at step 0.", removed.deleted_count)
    logger.info("Restore with: python -m scripts.reset_onboarding --restore %s", path)


async def restore(db, path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        snapshot = json_util.loads(fh.read())

    user_id = snapshot["user_id"]
    changes = {"onboarding": snapshot.get("onboarding"), "updated_at": datetime.now(timezone.utc)}
    if snapshot.get("integrations") is not None:
        changes["integrations"] = snapshot["integrations"]
    await db["users"].update_one({"user_id": user_id}, {"$set": changes})

    groups = snapshot.get("client_groups") or []
    if groups:
        # Clear whatever the dry run created before putting the originals back,
        # or the account ends up with both sets.
        await db["client_groups"].delete_many({"user_id": user_id})
        await db["client_groups"].insert_many(groups)

    logger.info("Restored %s: %d client groups, onboarding %s",
                user_id, len(groups), snapshot.get("onboarding"))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", help="account to reset")
    parser.add_argument("--confirm", action="store_true", help="actually apply the reset")
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument("--disconnect", action="store_true",
                        help="also clear GHL/Meta, for a blank-account run")
    parser.add_argument("--restore", metavar="BACKUP", help="restore from a backup file")
    args = parser.parse_args()

    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    try:
        if args.restore:
            await restore(db, args.restore)
        elif args.user:
            if not (args.confirm or args.dry_run):
                raise SystemExit("Pass --dry-run to preview, or --confirm to apply.")
            await reset(db, args.user, args.disconnect, dry_run=not args.confirm)
        else:
            parser.print_help()
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
