"""
scripts/reset_agents.py
-----------------------
Wipe an account's agents so it starts agent onboarding from nothing.

Agent onboarding locks Media Buyer and Sales Coach until the owner has done
each one's interview. This script removes what an account built before that
existed, so the interview is the only source of an agent's setup:

    agents              the Media Buying Agent (its settings, schedule, Slack
                        channel) and every custom agent
    sub_agents          every sub-agent, proposed, active or paused
    team_agents         Sales Coach's record: its settings and setup progress
    agent_client_claims which agent holds which client
    by agent id         rules and their versions, lessons (agent_memory),
                        practice cases, proposals and queued jobs
    history             runs, cards, ad outcomes and stored Slack presses.
                        Removed too, because a card whose agent is gone can
                        no longer be applied or undone. `--keep-history` leaves
                        them in place.
    users               `mcp_default_agent_id` is cleared (it points at an
                        agent that no longer exists)

**Left alone:** clients and their targets, integrations, contacts, leads,
alerts, reports, chat conversations, credits and the normal onboarding.

Every deleted document is written to a backup file first, one file per account,
and `--restore` puts them back. Nothing is deleted without `--confirm`.

    python -m scripts.reset_agents --user someone@example.com --dry-run
    python -m scripts.reset_agents --user someone@example.com --confirm
    python -m scripts.reset_agents --all --dry-run
    python -m scripts.reset_agents --all --confirm
    python -m scripts.reset_agents --restore backups/reset_agents_someone_2026....json
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import BulkWriteError

from ai.agents import claims
from ai.agents import store as agent_store
from ai.agents.stock.scoreboard import OUTCOMES
from ai.agents.sub_agents.core import SUB_AGENTS
from ai.mcp.agent_mcp import DEFAULT_FIELD
from ai.team.memory import TEAM_AGENTS
from core.database import DB_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")

# Keyed by the account.
BY_USER = (agent_store.AGENTS, SUB_AGENTS, TEAM_AGENTS, claims.CLAIMS)
# Keyed by the id of an agent, a sub-agent or a team agent.
BY_AGENT = (agent_store.RULES, agent_store.RULE_VERSIONS, agent_store.MEMORY, agent_store.PRACTICE,
            agent_store.PROPOSALS, agent_store.JOBS)
HISTORY = (agent_store.RUNS, agent_store.CARDS, OUTCOMES, agent_store.SLACK_ACTIONS)


async def snapshot(db, user_id: str, keep_history: bool) -> dict:
    """Everything this reset will delete for one account, as it is right now."""
    user = await db["users"].find_one({"user_id": user_id}, {"user_id": 1, DEFAULT_FIELD: 1})
    if not user:
        raise SystemExit(f"No such account: {user_id}")
    docs = {coll: await db[coll].find({"user_id": user_id}).to_list(None) for coll in BY_USER}
    ids = [d["_id"] for coll in (agent_store.AGENTS, SUB_AGENTS, TEAM_AGENTS) for d in docs[coll]]
    for coll in BY_AGENT + (() if keep_history else HISTORY):
        docs[coll] = await db[coll].find({"agent_id": {"$in": ids}}).to_list(None) if ids else []
    return {
        "user_id": user_id,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "agent_ids": ids,
        DEFAULT_FIELD: user.get(DEFAULT_FIELD),
        "collections": docs,
    }


async def reset(db, user_id: str, *, keep_history: bool, dry_run: bool) -> int:
    """Reset one account. Returns how many documents were (or would be) deleted."""
    snap = await snapshot(db, user_id, keep_history)
    docs = snap["collections"]
    total = sum(len(rows) for rows in docs.values())
    if total == 0 and not snap[DEFAULT_FIELD]:
        return 0

    logger.info("Account %s", user_id)
    for coll, rows in docs.items():
        if rows:
            logger.info("    %-22s %d", coll, len(rows))
    for agent in docs[agent_store.AGENTS]:
        logger.info("    agent: %s (%s, %s)", agent.get("name"), agent.get("type"), agent.get("status"))
    if dry_run:
        return total

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_user = "".join(ch if ch.isalnum() else "_" for ch in user_id)
    path = os.path.join(BACKUP_DIR, f"reset_agents_{safe_user}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json_util.dumps(snap, indent=2))
    logger.info("    backup: %s", path)

    # Children first, so a failure part-way never leaves rows whose agent is gone
    # without a backup of that agent (the backup above already holds everything).
    ids = snap["agent_ids"]
    for coll in BY_AGENT + (() if keep_history else HISTORY):
        if ids:
            await db[coll].delete_many({"agent_id": {"$in": ids}})
    for coll in BY_USER:
        await db[coll].delete_many({"user_id": user_id})
    await db["users"].update_one({"user_id": user_id}, {"$unset": {DEFAULT_FIELD: ""}})
    logger.info("    removed %d documents. Restore with: python -m scripts.reset_agents --restore %s", total, path)
    return total


async def restore(db, path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        snap = json_util.loads(fh.read())
    user_id = snap["user_id"]
    put_back = 0
    for coll, rows in snap["collections"].items():
        if not rows:
            continue
        try:
            res = await db[coll].insert_many(rows, ordered=False)
            put_back += len(res.inserted_ids)
        except BulkWriteError as exc:
            # A document made since the reset has the same id or unique key: it
            # is kept, and the rest of the batch still goes in.
            put_back += exc.details.get("nInserted", 0)
            logger.warning("%s: %d documents already exist and were left as they are",
                           coll, len(exc.details.get("writeErrors", [])))
    if snap.get(DEFAULT_FIELD):
        await db["users"].update_one({"user_id": user_id}, {"$set": {DEFAULT_FIELD: snap[DEFAULT_FIELD]}})
    logger.info("Restored %s: %d documents put back", user_id, put_back)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--user", help="one account to reset")
    who.add_argument("--all", action="store_true", help="every account")
    who.add_argument("--restore", metavar="BACKUP", help="put one account's backup file back")
    parser.add_argument("--confirm", action="store_true", help="actually delete")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted")
    parser.add_argument("--keep-history", action="store_true", help="leave runs, cards and outcomes in place")
    args = parser.parse_args()

    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    try:
        if args.restore:
            await restore(db, args.restore)
            return
        if not (args.user or args.all):
            parser.print_help()
            return
        if args.confirm == args.dry_run:
            raise SystemExit("Pass exactly one of --dry-run (preview) or --confirm (delete).")
        users = [args.user] if args.user else await db["users"].distinct("user_id")
        total = accounts = 0
        for user_id in users:
            n = await reset(db, user_id, keep_history=args.keep_history, dry_run=args.dry_run)
            total += n
            accounts += bool(n)
        verb = "would be removed" if args.dry_run else "removed"
        logger.info("%d documents %s across %d of %d accounts.", total, verb, accounts, len(users))
        if args.dry_run:
            logger.info("Dry run. Nothing changed. Re-run with --confirm to apply.")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
