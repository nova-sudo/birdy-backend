"""
Backfill script — recovers Meta leads the incremental sync skipped.

Between 2026-07-16 (commit b55ca6e, which wired up incremental lead sync) and
the per-ad watermark fix, the sync held ONE account-wide watermark and let the
first ad that reached it end the scan for the entire account. Every ad not yet
reached was never read, so roughly 63% of leads were never stored. Nothing was
deleted — the rows simply were never written, and Meta still has them.

This walks every ad on every account and re-reads back to --since, then upserts.
Writes are keyed on (user_id, ad_account_id, lead_id), the same key the live
sync uses, so re-running is idempotent and cannot duplicate.

This DOES call the Meta API, once per ad, so it is not free — unlike
backfill_cohort_funnel.py. Start with --dry-run to see the scale.

Run with:
    python -m scripts.backfill_meta_leads --dry-run
    python -m scripts.backfill_meta_leads --since 2026-07-15
    python -m scripts.backfill_meta_leads --user someone@example.com
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from core.database import DB_NAME
from utils.phone_normalize import compute_match_keys
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.facebook_leads import stage1_get_all_ad_ids
from integrations.facebook_utils.meta_incremental_refresh import (
    _get_todays_leads_for_ad,
    _normalize_leads,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))

# The regression landed on 2026-07-16; a day of overlap costs nothing because
# the writes are upserts.
DEFAULT_SINCE = "2026-07-15"

CONCURRENT_ADS = 5


async def backfill_group(db, group, token, since_iso, dry_run):
    """Walk every ad on one group's account back to `since_iso`."""
    account = group.get("meta_ad_account_id")
    if not account:
        return 0, 0

    ad_ids = await stage1_get_all_ad_ids(account, token)
    if not ad_ids:
        logger.info("  %s: no ads", group.get("name"))
        return 0, 0

    fetched = []
    for i in range(0, len(ad_ids), CONCURRENT_ADS):
        batch = ad_ids[i:i + CONCURRENT_ADS]
        # No lead-id watermark, and a created_time floor of `since_iso`: each
        # ad walks back until it passes the cutoff, then stops. Every ad is
        # visited — that is the whole point of this script.
        results = await asyncio.gather(
            *[_get_todays_leads_for_ad(ad_id, token, None, since_iso) for ad_id in batch],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("  ad fetch failed: %s", r)
                continue
            leads, _hit = r
            fetched.extend(leads)
        if i + CONCURRENT_ADS < len(ad_ids):
            await asyncio.sleep(0.5)

    normalized = _normalize_leads(
        fetched, group["user_id"], account, group["id"], group.get("name", "")
    )
    if not normalized or dry_run:
        logger.info("  %s: %d ads, %d leads fetched%s",
                    group.get("name"), len(ad_ids), len(normalized),
                    " (dry run, nothing written)" if dry_run else "")
        return len(normalized), 0

    now = datetime.now()
    ops = []
    for lead in normalized:
        lead_id = lead.get("lead_id")
        if not lead_id:
            continue
        ops.append(UpdateOne(
            {"user_id": group["user_id"], "ad_account_id": account, "lead_id": lead_id},
            {
                "$set": {
                    "user_id": group["user_id"],
                    "ad_account_id": account,
                    "client_group_id": group["id"],
                    "client_group_name": group.get("name", ""),
                    "lead_id": lead_id,
                    "lead_data": lead,
                    "match_keys": compute_match_keys(
                        lead.get("email"), lead.get("phone_number")
                    ),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        ))

    inserted = 0
    for i in range(0, len(ops), 500):
        res = await db["facebook_leads"].bulk_write(ops[i:i + 500], ordered=False)
        inserted += res.upserted_count

    logger.info("  %s: %d ads, %d fetched, %d NEW rows",
                group.get("name"), len(ad_ids), len(normalized), inserted)
    return len(normalized), inserted


async def main(user_filter, since_iso, dry_run):
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    query = {"meta_ad_account_id": {"$exists": True, "$nin": [None, ""]}}
    if user_filter:
        query["user_id"] = user_filter

    groups = await db["client_groups"].find(
        query, {"id": 1, "name": 1, "user_id": 1, "meta_ad_account_id": 1,
                "client_status": 1, "_id": 0},
    ).to_list(None)
    groups = [g for g in groups if (g.get("client_status") or "Active") == "Active"]

    logger.info("Backfilling %d active groups back to %s%s",
                len(groups), since_iso, "  [DRY RUN]" if dry_run else "")

    tokens = {}
    total_fetched = total_new = 0
    for group in groups:
        uid = group["user_id"]
        if uid not in tokens:
            tok = await get_facebook_token(uid, client)
            tokens[uid] = (tok or {}).get("access_token")
        token = tokens[uid]
        if not token:
            logger.warning("  %s: no Meta token for %s — skipped", group.get("name"), uid)
            continue
        try:
            fetched, new = await backfill_group(db, group, token, since_iso, dry_run)
            total_fetched += fetched
            total_new += new
        except Exception as e:
            logger.warning("  %s: failed — %s", group.get("name"), e)

    logger.info("Done — %d leads fetched, %d new rows written", total_fetched, total_new)
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default=DEFAULT_SINCE, help="yyyy-mm-dd floor (default 2026-07-15)")
    p.add_argument("--user", help="only this user_id")
    p.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    a = p.parse_args()
    asyncio.run(main(a.user, f"{a.since}T00:00:00+0000", a.dry_run))
