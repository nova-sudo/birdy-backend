"""Ad-history sync state, one doc per watched client group.

The collection (`_id` = client_group_id) tracks how far each client's daily ad rows
have been pulled from Meta into `agent_ad_days`: a newest-first backfill over
HISTORY_TOTAL_DAYS, then a rolling top-up of the last TOPUP_DAYS. The cron pass
(jobs/agent_history_jobs.py) claims groups through `claim`/`release`; the runner
asks `statuses`/`is_ready` whether a client's history is usable yet.

A doc whose Meta read keeps failing backs off through `failures`/`retry_at`; a doc
no agent watches any more is `orphaned` and never claimed until it is watched again.
"""

from datetime import date, datetime, timedelta

from ai.agents.store import AGENTS

HISTORY = "agent_ad_history"

STATUS_PENDING, STATUS_RUNNING, STATUS_READY, STATUS_ERROR = "pending", "running", "ready", "error"
STATUS_ORPHANED = "orphaned"

GRAPH_BASE_URL = "https://graph.facebook.com/v25.0"
HISTORY_TOTAL_DAYS = 395
HISTORY_CHUNK_DAYS = 30
TOPUP_DAYS = 28
TOPUP_EVERY = timedelta(hours=5)
STATE_REFRESH_EVERY = timedelta(days=1)
AGENT_HISTORY_BUDGET_SECONDS = 200
AGENT_HISTORY_GROUPS_PER_TICK = 3
HISTORY_CLAIM_STALE = timedelta(minutes=10)
HISTORY_ERROR_RETRY = timedelta(hours=6)
CLAIM_CANDIDATES_PER_SLOT = 4


def window_start(today: date) -> date:
    """Oldest day the backfill has to reach."""
    return today - timedelta(days=HISTORY_TOTAL_DAYS - 1)


async def ensure_docs(db) -> int:
    """Create a pending history doc for every watched client group (any agent status)
    that has a Meta ad account; existing docs keep their progress. Docs no agent
    watches become `orphaned`; an orphaned doc watched again goes back to pending with
    its progress kept, so a finished backfill only needs a top-up. Returns how many
    docs were inserted."""
    gids: set[str] = set()
    async for agent in db[AGENTS].find({}, {"scope": 1}):
        gids.update((agent.get("scope") or {}).get("client_group_ids") or [])

    now = datetime.utcnow()
    await db[HISTORY].update_many(
        {"_id": {"$nin": sorted(gids)}, "status": {"$ne": STATUS_ORPHANED}},
        {"$set": {"status": STATUS_ORPHANED, "updated_at": now}},
    )
    if not gids:
        return 0

    inserted = 0
    async for group in db["client_groups"].find({"id": {"$in": sorted(gids)}}):
        if not group.get("meta_ad_account_id"):
            continue
        res = await db[HISTORY].update_one(
            {"_id": group["id"]},
            {"$setOnInsert": {
                "user_id": group.get("user_id"),
                "ad_account_id": group["meta_ad_account_id"],
                "campaign_ids": group.get("meta_campaign_ids"),
                "status": STATUS_PENDING,
                "oldest_done": None,
                "newest_done": None,
                "last_topup_at": None,
                "state_refreshed_at": None,
                "error": None,
                "failures": 0,
                "retry_at": None,
                "claimed_at": None,
                "created_at": now,
            }},
            upsert=True,
        )
        if res.upserted_id is not None:
            inserted += 1
            continue
        await db[HISTORY].update_one(
            {"_id": group["id"], "status": STATUS_ORPHANED},
            {"$set": {"status": STATUS_PENDING, "error": None, "failures": 0, "retry_at": None,
                      "updated_at": now}},
        )
    return inserted


def _older_than(field: str, cutoff: datetime) -> dict:
    return {"$or": [{field: None}, {field: {"$lt": cutoff}}]}


async def claim(db, now: datetime, n: int) -> list[dict]:
    """Claim up to n due docs: pending/running first, then ready/error ones that are
    due, each group oldest `updated_at` first so busy docs take turns. Docs backing
    off (`retry_at` in the future) and orphaned docs are skipped. Each claim is a CAS
    on the `claimed_at` value just read, so overlapping ticks never share a group."""
    unclaimed = {"$or": [{"claimed_at": None}, {"claimed_at": {"$lt": now - HISTORY_CLAIM_STALE}}]}
    not_backing_off = {"$or": [{"retry_at": None}, {"retry_at": {"$lte": now}}]}
    in_flight = {"status": {"$in": [STATUS_PENDING, STATUS_RUNNING]}}
    due_later = {"$or": [
        {"status": STATUS_READY, **_older_than("last_topup_at", now - TOPUP_EVERY)},
        {"status": STATUS_READY, **_older_than("state_refreshed_at", now - STATE_REFRESH_EVERY)},
        {"status": STATUS_ERROR, **_older_than("updated_at", now - HISTORY_ERROR_RETRY)},
    ]}

    claimed: list[dict] = []
    for due in (in_flight, due_later):
        if len(claimed) >= n:
            break
        # A few spares per slot cover candidates an overlapping tick claims first.
        candidates = await (db[HISTORY].find({"$and": [unclaimed, not_backing_off, due]})
                            .sort("updated_at", 1).limit(n * CLAIM_CANDIDATES_PER_SLOT)
                            .to_list(length=None))
        for cand in candidates:
            if len(claimed) >= n:
                break
            won = await db[HISTORY].find_one_and_update(
                {"_id": cand["_id"], "claimed_at": cand.get("claimed_at")},
                {"$set": {"claimed_at": now}},
                return_document=True,
            )
            if won is not None:
                claimed.append(won)
    return claimed


async def release(db, gid: str, **fields) -> None:
    await db[HISTORY].update_one(
        {"_id": gid}, {"$set": {**fields, "claimed_at": None, "updated_at": datetime.utcnow()}}
    )


async def statuses(db, gids: list[str]) -> dict[str, dict]:
    out = {gid: {"status": STATUS_PENDING, "error": None, "oldest_done": None, "newest_done": None,
                  "last_topup_at": None}
           for gid in gids}
    if not gids:
        return out
    async for doc in db[HISTORY].find({"_id": {"$in": list(gids)}}):
        status = doc.get("status") or STATUS_PENDING
        out[doc["_id"]] = {
            # Asked about a watched client whose doc is still orphaned: the next pass revives it.
            "status": STATUS_PENDING if status == STATUS_ORPHANED else status,
            "error": doc.get("error"),
            "oldest_done": doc.get("oldest_done"),
            "newest_done": doc.get("newest_done"),
            "last_topup_at": doc.get("last_topup_at"),
        }
    return out


def is_ready(doc: dict | None) -> bool:
    """Ready, or errored during a top-up (last_topup_at is only set once a backfill
    finished, so such a client stays usable)."""
    if not doc:
        return False
    status = doc.get("status")
    return status == STATUS_READY or (status == STATUS_ERROR and doc.get("last_topup_at") is not None)
