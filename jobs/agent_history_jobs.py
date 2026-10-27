"""
jobs/agent_history_jobs.py
--------------------------
The ad-history sync pass meta-tick runs every minute for the Media Buying Agent.

For each watched client group (ai/agents/history.py holds the per-group state) it
pulls daily ad rows from Meta into `agent_ad_days`: a newest-first backfill in
HISTORY_CHUNK_DAYS chunks back to history.window_start(today), then a top-up of the
last TOPUP_DAYS every TOPUP_EVERY. Up to AGENT_HISTORY_GROUPS_PER_TICK groups run in
parallel; each is claimed by CAS so overlapping ticks never work the same group.

The time budget is checked between chunks, never in the middle of a fetch; progress
is saved after every chunk, so the next tick resumes from `oldest_done - 1 day`.
One client failing (no token, no Meta access, anything else) is recorded on its own
doc and never stops the others.
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from ai.agents import history
from ai.agents.meta_state import backfill_paused_at, refresh_meta_state
from core.database import DB_NAME
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_ad_daily import (
    COLLECTION as AD_DAYS,
    MetaPermissionError,
    fetch_ad_daily,
    store_ad_daily,
)

logger = logging.getLogger(__name__)


def _day(v) -> date | None:
    return date.fromisoformat(v) if v else None


async def _check_scope(db, doc: dict) -> dict:
    """If the group's ad account or campaign filter changed, drop its history rows
    and restart the backfill so old-scope rows never mix with new ones."""
    group = await db["client_groups"].find_one({"id": doc["_id"]}) or {}
    campaign_ids = group.get("meta_campaign_ids")
    ad_account_id = group.get("meta_ad_account_id") or doc.get("ad_account_id")
    if campaign_ids == doc.get("campaign_ids") and ad_account_id == doc.get("ad_account_id"):
        return doc

    await db[AD_DAYS].delete_many({"user_id": doc["user_id"], "client_group_id": doc["_id"]})
    reset = {"campaign_ids": campaign_ids, "ad_account_id": ad_account_id,
             "status": history.STATUS_PENDING, "oldest_done": None, "newest_done": None,
             "last_topup_at": None, "error": None}
    await db[history.HISTORY].update_one({"_id": doc["_id"]}, {"$set": reset})
    logger.info("[agent-history] %s: campaign scope changed, history reset", doc["_id"])
    return {**doc, **reset}


async def _refresh_state(db, mongo_client, doc: dict, *, today: date, now: datetime) -> None:
    """Daily ad status refresh. Stamped even on failure so a broken refresh is retried
    tomorrow rather than every minute; it never stops the history work."""
    stamped = doc.get("state_refreshed_at")
    if stamped is not None and stamped >= now - history.STATE_REFRESH_EVERY:
        return
    gid = doc["_id"]
    try:
        await refresh_meta_state(db, mongo_client, doc["user_id"], gid, doc["ad_account_id"], today=today)
        await backfill_paused_at(db, doc["user_id"], gid)
    except Exception as e:
        logger.warning("[agent-history] %s: ad state refresh failed: %s", gid, e)
    await db[history.HISTORY].update_one({"_id": gid}, {"$set": {"state_refreshed_at": now}})


async def _advance_one(db, mongo_client, doc: dict, *, today: date, now: datetime,
                       budget_left, out: dict) -> None:
    gid = doc["_id"]
    try:
        doc = await _check_scope(db, doc)

        token = ((await get_facebook_token(doc["user_id"], mongo_client)) or {}).get("access_token")
        if not token:
            out["errors"] += 1
            await history.release(db, gid, status=history.STATUS_ERROR, error="no_token")
            return

        await _refresh_state(db, mongo_client, doc, today=today, now=now)

        acct, campaign_ids = doc["ad_account_id"], doc.get("campaign_ids")
        start = history.window_start(today)
        oldest, newest = _day(doc.get("oldest_done")), doc.get("newest_done")
        covered = oldest is not None and oldest <= start

        if not covered:
            while (oldest is None or oldest > start) and budget_left():
                until = oldest - timedelta(days=1) if oldest else today
                since = max(until - timedelta(days=history.HISTORY_CHUNK_DAYS - 1), start)
                rows = await fetch_ad_daily(acct, token, since, until, campaign_ids)
                if rows is None:
                    break
                await store_ad_daily(db, user_id=doc["user_id"], client_group_id=gid,
                                     ad_account_id=acct, rows=rows)
                oldest, newest = since, newest or until.isoformat()
                await db[history.HISTORY].update_one({"_id": gid}, {"$set": {
                    "oldest_done": oldest.isoformat(), "newest_done": newest,
                    "status": history.STATUS_RUNNING}})
                out["chunks"] += 1
            if oldest is not None and oldest <= start:
                out["ready"] += 1
                await history.release(db, gid, status=history.STATUS_READY, error=None,
                                      last_topup_at=datetime.utcnow())
            else:
                await history.release(db, gid, status=history.STATUS_RUNNING, error=None)
            return

        last = doc.get("last_topup_at")
        if last is not None and last >= now - history.TOPUP_EVERY:
            await history.release(db, gid)                     # claimed for the state refresh only
            return
        rows = await fetch_ad_daily(acct, token, today - timedelta(days=history.TOPUP_DAYS - 1),
                                    today, campaign_ids)
        if rows is None:
            await history.release(db, gid)                     # retried next tick
            return
        await store_ad_daily(db, user_id=doc["user_id"], client_group_id=gid, ad_account_id=acct, rows=rows)
        out["topups"] += 1
        await history.release(db, gid, status=history.STATUS_READY, error=None,
                              last_topup_at=datetime.utcnow(), newest_done=today.isoformat())
    except MetaPermissionError as e:
        logger.warning("[agent-history] %s: no Meta access: %s", gid, e)
        out["errors"] += 1
        await history.release(db, gid, status=history.STATUS_ERROR, error="no_access")
    except Exception as e:
        logger.error("[agent-history] %s failed: %s", gid, e, exc_info=True)
        out["errors"] += 1
        await history.release(db, gid, status=history.STATUS_ERROR, error="failed")


async def advance_agent_history(mongo_client, *, max_seconds: float, today: date | None = None) -> dict:
    """One sync pass. Returns {"claimed", "chunks", "topups", "ready", "errors"}."""
    db = mongo_client[DB_NAME]
    now = datetime.utcnow()
    today = today or now.date()
    started = time.monotonic()
    out = {"claimed": 0, "chunks": 0, "topups": 0, "ready": 0, "errors": 0}

    await history.ensure_docs(db)
    docs = await history.claim(db, now, history.AGENT_HISTORY_GROUPS_PER_TICK)
    out["claimed"] = len(docs)
    if not docs:
        return out

    def budget_left() -> bool:
        return time.monotonic() - started < max_seconds

    results = await asyncio.gather(
        *[_advance_one(db, mongo_client, d, today=today, now=now, budget_left=budget_left, out=out)
          for d in docs],
        return_exceptions=True,
    )
    for d, r in zip(docs, results):
        if isinstance(r, Exception):   # only reachable if the release itself failed
            logger.error("[agent-history] %s: could not release: %s", d["_id"], r)
    return out
