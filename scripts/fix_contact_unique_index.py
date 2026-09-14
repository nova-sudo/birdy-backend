"""
scripts/fix_contact_unique_index.py
-----------------------------------
Re-grain the ghl_contacts unique index to match how contacts are actually written.

The index was `(location_id, contact_id)` UNIQUE, which asserts that a GHL
contact belongs to at most one Birdy account. That was never true: an agency and
a sub-agency can both connect the same GoHighLevel location, and on production
ten locations are held by two accounts at once.

Meanwhile the sync upserts on `(user_id, location_id, contact_id)`
(services/ghl_service.py). So when account A's FULL LOAD reaches a contact that
only account B has ever stored, the filter matches nothing *for A*, Mongo
attempts an insert, and the insert collides with B's row. bulk_write raises
E11000, the page loop catches it, and the whole load aborts:

    GHL FULL LOAD incomplete for Aura: stopped at page 1/7

Nothing is lost when that happens — the code deliberately skips pruning on a
partial load — but the initial sync never completes, `last_ghl_refresh` stays
unset, and every cron tick retries the same failure.

This drops the old index and builds `(user_id, location_id, contact_id)` UNIQUE,
which is the grain the writes already use. No documents change, and no read path
moves: every query on this collection already scopes by user_id.

    python -m scripts.fix_contact_unique_index --dry-run
    python -m scripts.fix_contact_unique_index --confirm
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))

OLD_INDEX = "location_contact_unique"
NEW_INDEX = "user_location_contact_unique"
NEW_KEYS = [("user_id", 1), ("location_id", 1), ("contact_id", 1)]


async def _duplicates_on_new_key(coll) -> list:
    """
    Rows that would make the new unique index impossible to build.

    Has to be checked before dropping anything: a failed createIndex after a
    successful dropIndex would leave the collection with no uniqueness at all.
    """
    return await coll.aggregate([
        {"$group": {
            "_id": {"u": "$user_id", "l": "$location_id", "c": "$contact_id"},
            "n": {"$sum": 1},
        }},
        {"$match": {"n": {"$gt": 1}}},
        {"$limit": 10},
    ], allowDiskUse=True).to_list(10)


async def run(db, dry_run: bool) -> None:
    coll = db["ghl_contacts"]

    existing = {ix["name"]: ix async for ix in coll.list_indexes()}
    total = await coll.estimated_document_count()

    logger.info("ghl_contacts documents : %d", total)
    logger.info("%-32s %s", OLD_INDEX + " present:", OLD_INDEX in existing)
    logger.info("%-32s %s", NEW_INDEX + " present:", NEW_INDEX in existing)

    # How much this actually fixes, in this database.
    shared = await db["client_groups"].aggregate([
        {"$match": {"ghl_location_id": {"$ne": None}}},
        {"$group": {"_id": "$ghl_location_id", "owners": {"$addToSet": "$user_id"}}},
        {"$match": {"$expr": {"$gt": [{"$size": "$owners"}, 1]}}},
        {"$count": "n"},
    ]).to_list(1)
    logger.info("Locations held by >1 account: %d  (each one can break a FULL LOAD)",
                shared[0]["n"] if shared else 0)

    logger.info("Checking for rows that would block the new unique index…")
    dupes = await _duplicates_on_new_key(coll)
    if dupes:
        logger.error("Cannot proceed — %d+ duplicate (user_id, location_id, contact_id) groups:", len(dupes))
        for d in dupes:
            logger.error("    %s  x%d", d["_id"], d["n"])
        raise SystemExit("Resolve these duplicates first; nothing has been changed.")
    logger.info("None — the new index can be built.")

    if dry_run:
        logger.info("")
        logger.info("Dry run. Nothing changed. Re-run with --confirm to apply.")
        return

    # Build first, drop second. The reverse order would leave a window with no
    # uniqueness on this collection, and a crash in between would leave it that
    # way permanently.
    if NEW_INDEX not in existing:
        logger.info("Building %s …", NEW_INDEX)
        await coll.create_index(NEW_KEYS, unique=True, name=NEW_INDEX, background=True)
        logger.info("Built.")
    else:
        logger.info("%s already exists — leaving it.", NEW_INDEX)

    if OLD_INDEX in existing:
        logger.info("Dropping %s …", OLD_INDEX)
        await coll.drop_index(OLD_INDEX)
        logger.info("Dropped.")
    else:
        logger.info("%s already gone.", OLD_INDEX)

    logger.info("")
    logger.info("Done. Indexes now:")
    async for ix in coll.list_indexes():
        logger.info("    %-34s %s%s", ix["name"], dict(ix["key"]),
                    "  UNIQUE" if ix.get("unique") else "")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="apply the change")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen")
    args = parser.parse_args()
    if not (args.confirm or args.dry_run):
        raise SystemExit("Pass --dry-run to preview, or --confirm to apply.")

    client = AsyncIOMotorClient(MONGO_URI)
    try:
        await run(client[DB_NAME], dry_run=not args.confirm)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
