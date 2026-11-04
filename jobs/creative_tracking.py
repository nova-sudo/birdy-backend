"""Nightly tracking of creative tests: fetch Meta insights, score, set verdicts."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone

from pymongo import ReturnDocument

from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_creative_reader import (
    MetaReadError,
    fetch_account_daily,
    fetch_ad_status,
    fetch_tracked_ad_days,
)
from services.creatives.store import (
    AD_DAYS,
    COUNTERS,
    FINAL_VERDICTS,
    LIVE_STATUSES,
    NOT_MANAGED,
    TEST_ADS,
    TESTS,
    TESTING,
    creative_format,
    list_test_ads,
)
from ai.agents.stock.apply import process_queued_lines
from services.creatives.posting import run_post_jobs
from services.creatives.reconcile import reconcile_all
from services.creatives.upload_grant import cleanup_archived_media
from services.creatives.targets import owning_group, resolve_account_targets
from services.creatives.verdict import (
    aggregate_days,
    derive,
    fx_rate,
    rollup,
    score_account,
    test_verdict,
    user_currency,
)
from services.daily_refresh_slot import DEFAULT_TIMEZONE, last_slot_utc

logger = logging.getLogger(__name__)

TICK_BUDGET_SECONDS = 45
# Posting gets the first slice of the tick, then stock agent lines waiting to be applied; tracking
# then runs against its own TICK_BUDGET_SECONDS deadline from the tick start (so always at least ~10 s).
# The stock budget is hard, since one Graph call can take 30 s. Stock stops starting lines before it
# runs out, so this cut is only a backstop, and a line it catches is read on the next tick.
POST_BUDGET_SECONDS = 25
STOCK_LINES_BUDGET_SECONDS = 10
MAX_TESTS_PER_TICK = 25


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _to_day(row: dict) -> dict:
    """Map a Meta insights row onto the shape aggregate_days expects."""
    return {
        "date": row.get("date_start") or row.get("date"),
        "spend": row.get("spend"),
        "impressions": row.get("impressions"),
        "clicks": row.get("clicks"),
        "link_clicks": row.get("inline_link_clicks", row.get("link_clicks")),
        "actions": row.get("actions"),
    }


async def _upsert_days(db, user_id: str, ad_id: str, rows: list[dict]) -> None:
    import uuid

    for row in rows:
        d = _to_day(row)
        if not d["date"]:
            continue
        await db[AD_DAYS].update_one(
            {"user_id": user_id, "ad_id": ad_id, "date": d["date"]},
            {"$set": {**{k: v for k, v in d.items() if k != "date"}, "updated_at": _naive_utc(datetime.now(timezone.utc))},
             "$setOnInsert": {"id": uuid.uuid4().hex}},
            upsert=True,
        )


def _baseline(account_rows: list[dict], ad_totals: dict, rate: float = 1.0) -> dict | None:
    """Account totals minus the test ad's own; both in the account's currency, the
    result's money converted by `rate`."""
    if not account_rows:
        return None
    acct = aggregate_days([_to_day(r) for r in account_rows])
    rest = {
        k: max(acct[k] - ad_totals.get(k, 0), 0)
        for k in ("spend", "impressions", "clicks", "link_clicks", "leads", "video_views_3s")
    }
    rest["spend"] = rest["spend"] * rate
    return derive(rest)


def _in_user_currency(test_targets: dict, ad: dict, group_cpl, rate: float) -> dict:
    """Targets for one ad, all in the user's currency. The test's own targets already are;
    a client group's CPL target and a per-account override are entered in the ad account's
    currency (the Targets tab and the wizard's per-account drawer show its symbol)."""
    override = (ad.get("targets_override") or {}).get("cpl")
    if override is not None:
        ad = {**ad, "targets_override": {**ad["targets_override"], "cpl": float(override) * rate}}
    if group_cpl not in (None, ""):
        try:
            group_cpl = float(group_cpl) * rate
        except (TypeError, ValueError):
            group_cpl = None
    return resolve_account_targets(test_targets, ad, group_cpl)


async def track_test(db, mongo_client, test: dict, *, today: date) -> dict:
    user_id = test["user_id"]
    ads = await list_test_ads(db, test["id"], user_id=user_id)
    live = [a for a in ads if a.get("post_state") == "active" and (a.get("meta") or {}).get("ad_id")]
    prev_by_ad = {a["id"]: ((a.get("score") or {}).get("state"), a.get("ad_account_id"))
                  for a in ads if a.get("post_state") == "active"}
    changed: list[dict] = []

    fb = await get_facebook_token(user_id, mongo_client)
    token = (fb or {}).get("access_token")
    if live and not token:
        raise MetaReadError("no Meta token for user")

    start = test.get("started_at")
    if isinstance(start, datetime):
        start_day = start.date()
    else:
        starts = [a["created_at"].date() for a in live if isinstance(a.get("created_at"), datetime)]
        start_day = min(starts) if starts else today
    until = today - timedelta(days=1)

    group_ids = list({a["client_group_id"] for a in live if a.get("client_group_id")})
    groups = await db["client_groups"].find({"user_id": user_id, "id": {"$in": group_ids}}).to_list(None)
    by_gid = {g["id"]: g for g in groups}
    accounts = {a.get("ad_account_id") for a in live}
    account_groups = await db["client_groups"].find(
        {"user_id": user_id, "meta_ad_account_id": {"$in": list(accounts)}}).to_list(None)

    # 1. removed-in-Meta check, then fetch days for the rest
    still_live = []
    for ad in live:
        status = await fetch_ad_status(ad["meta"]["ad_id"], token)
        if status is None:
            await db[TEST_ADS].update_one(
                {"id": ad["id"], "user_id": user_id},
                {"$set": {"post_state": "removed", "score": {"state": "removed"}}},
            )
        else:
            # Meta's effective_status as read now (ACTIVE, PAUSED, ADSET_PAUSED, ...): a
            # pause or a re-enable done in Ads Manager is what pause and findings see next.
            ad["meta_status"] = status
            await db[TEST_ADS].update_one(
                {"id": ad["id"], "user_id": user_id}, {"$set": {"meta_status": status}})
            still_live.append(ad)

    days_by_ad: dict[str, list[dict]] = {}
    by_account: dict[str, list[dict]] = {}
    for ad in still_live:
        by_account.setdefault(ad["ad_account_id"], []).append(ad)
    if start_day <= until:
        for acct, acct_ads in by_account.items():
            ids = [a["meta"]["ad_id"] for a in acct_ads]
            for row in await fetch_tracked_ad_days(acct, token, ids, start_day, until):
                days_by_ad.setdefault(row.get("ad_id"), []).append(row)
    for ad in still_live:
        await _upsert_days(db, user_id, ad["meta"]["ad_id"], days_by_ad.get(ad["meta"]["ad_id"], []))

    # 2. baseline (mode baseline only): account daily minus the test ad's own days
    mode = (test.get("targets") or {}).get("mode")
    account_rows: dict[str, list[dict]] = {}
    if mode == "baseline" and start_day <= until:
        for acct in by_account:
            account_rows[acct] = await fetch_account_daily(acct, token, start_day, until)

    # 3. score
    # Targets are in the user's currency, so each account's money is converted before it is
    # scored (CPL and CPC derive from the converted spend). Without a rate it is never scored.
    fmt = creative_format(test)
    now = _naive_utc(datetime.now(timezone.utc))
    user_cur = await user_currency(user_id, db)  # None: nothing is converted, nothing scored
    scored_ads = []
    account_curs: set = set()
    for ad in still_live:
        grp = by_gid.get(ad.get("client_group_id"))
        if grp is None:
            same = [g for g in account_groups if g.get("meta_ad_account_id") == ad["ad_account_id"]]
            grp = owning_group(same, ad.get("campaign_id")) if same else {}
        acct_cur = grp.get("ad_account_currency") or user_cur
        account_curs.add(acct_cur)
        # fx_rate is None without a user currency, so the account stays unscored.
        rate = fx_rate(acct_cur, user_cur)
        raw = aggregate_days([_to_day(r) for r in days_by_ad.get(ad["meta"]["ad_id"], [])])
        group_cpl = (grp.get("targets") or {}).get("cpl")
        if rate is None:
            score = {**derive(raw), "state": "scoring", "checks": [], "rate_missing": True}
            baseline, currency = None, acct_cur
        else:
            totals = {**raw, "spend": round(raw["spend"] * rate, 4)}
            targets = _in_user_currency(test.get("targets") or {}, ad, group_cpl, rate)
            baseline = (_baseline(account_rows.get(ad["ad_account_id"], []), raw, rate)
                        if mode == "baseline" else None)
            score = score_account(totals, targets, fmt=fmt, baseline=baseline)
            currency = user_cur
        score["baseline_cpl"] = baseline["cpl"] if baseline else None
        score["evaluated_at"] = now
        money = {"currency": currency, "account_currency": acct_cur, "fx_rate": rate}
        await db[TEST_ADS].update_one(
            {"id": ad["id"], "user_id": user_id},
            {"$set": {"score": score, **money, "updated_at": now}})
        scored_ads.append({**ad, "score": score, **money, "format": fmt})

    def _agg(states):
        if "fail" in states:
            return "fail"
        return "pass" if "pass" in states else "other"

    before_acct: dict = {}
    for st, acct in prev_by_ad.values():
        before_acct.setdefault(acct, []).append(st)
    after_acct: dict = {}
    for a in scored_ads:
        after_acct.setdefault(a["ad_account_id"], []).append(a["score"]["state"])
    for acct, states in after_acct.items():
        b, n = _agg(before_acct.get(acct, [])), _agg(states)
        if b == "pass" and n == "fail":
            changed.append({"kind": "account_fail", "ad_account_id": acct})
        elif b == "fail" and n == "pass":
            changed.append({"kind": "account_pass", "ad_account_id": acct})

    # 4. verdict and summary
    win_share = (test.get("targets") or {}).get("win_share")
    verdict = test_verdict([a["score"] for a in scored_ads], win_share)
    summary = await rollup(db, user_id, scored_ads)
    summary["mixed_currency"] = any(c != user_cur for c in account_curs)
    summary["updated_at"] = now

    fields: dict = {"summary": summary}
    old_verdict = test.get("verdict")
    if verdict in FINAL_VERDICTS:
        fields["verdict"] = verdict
        fields["status"] = verdict
        if not test.get("verdict_at"):
            fields["verdict_at"] = now
        if verdict != old_verdict:
            changed.append({"kind": "verdict"})
    elif test.get("status") in FINAL_VERDICTS:
        fields.update(verdict=None, status=TESTING)
    fresh = await _write_live(db, user_id, test["id"], fields)
    if fresh is None:  # archived (or reset) since it was loaded: leave it alone
        return {"test_id": test["id"], "verdict": None, "changed": [], "skipped": "not_live"}
    await _sync_suggestions(db, mongo_client, fresh)
    return {"test_id": test["id"], "verdict": verdict, "changed": changed}


async def _write_live(db, user_id: str, test_id: str, fields: dict) -> dict | None:
    """Write tracking results only while the test is still live: the status filter is
    part of the update, so an archive between load and write is never undone."""
    row = await db[TESTS].find_one_and_update(
        {"id": test_id, "user_id": user_id, "status": {"$in": list(LIVE_STATUSES)}},
        {"$set": {**fields, "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if row is not None and row.get("_id") != row.get("id"):
        row = dict(row)
        row.pop("_id", None)
    return row


async def _sync_suggestions(db, mongo_client, test: dict) -> None:
    """Refresh the test's suggestions; a failure here never fails tracking."""
    try:
        from ai.suggestions import creative_findings

        ads = await list_test_ads(db, test["id"], user_id=test["user_id"])
        await creative_findings.sync_creative_suggestions(
            db, test["user_id"], test, ads, mongo_client=mongo_client)
    except Exception as e:
        logger.error(f"[creatives-track] suggestions for {test.get('id')} failed: {e}", exc_info=True)


RETRY_AFTER = timedelta(minutes=20)


async def track_due_tests(db, mongo_client, now_utc: datetime, *, deadline: float | None = None) -> dict:
    now = _naive_utc(now_utc)
    today = now.date()
    tz_cache: dict[str, str | None] = {}
    blocked_users: set[str] = set()
    tracked = skipped = 0
    events: list[dict] = []

    docs = await db[TESTS].find({"status": {"$in": [TESTING, *FINAL_VERDICTS]}, **NOT_MANAGED}).to_list(None)
    docs.sort(key=lambda d: (d.get("tracked_at") is not None, _naive_utc(d["tracked_at"]) if d.get("tracked_at") else datetime.min))
    for doc in docs:
        if tracked >= MAX_TESTS_PER_TICK or (deadline and time.monotonic() > deadline):
            break
        test = dict(doc)
        if test.get("_id") != test.get("id"):
            test.pop("_id", None)
        uid = test["user_id"]
        if uid in blocked_users:
            continue
        retry = test.get("retry_after")
        if retry is not None and _naive_utc(retry) > now:
            continue
        if uid not in tz_cache:
            u = await db["users"].find_one({"user_id": uid}, {"timezone": 1})
            tz_cache[uid] = (u or {}).get("timezone") or DEFAULT_TIMEZONE
        last = test.get("tracked_at")
        if last is not None and _naive_utc(last) >= last_slot_utc(now, tz_cache[uid]):
            continue
        key = {"id": test["id"], "user_id": uid}
        try:
            res = await track_test(db, mongo_client, test, today=today)
        except MetaReadError as e:
            skipped += 1
            if e.retryable:
                logger.warning(f"[creatives-track] {test['id']} rate limited, retry later: {e}")
                await db[TESTS].update_one(key, {"$set": {"retry_after": now + RETRY_AFTER}})
                blocked_users.add(uid)
            else:
                logger.warning(f"[creatives-track] {test['id']} failed: {e}")
                await _stamp_error(db, key, now, getattr(e, "code", None), str(e))
            continue
        except Exception as e:
            logger.error(f"[creatives-track] {test['id']} failed: {e}", exc_info=True)
            skipped += 1
            await _stamp_error(db, key, now, None, str(e))
            continue
        await db[TESTS].update_one(key, {"$set": {"tracked_at": now}, "$unset": {"track_error": "", "retry_after": ""}})
        tracked += 1
        events.extend({**ev, "test_id": test["id"], "user_id": uid} for ev in res["changed"])
    return {"tracked": tracked, "skipped": skipped, "events": events}


async def _stamp_error(db, key: dict, now: datetime, code, message: str) -> None:
    """Wait for the next nightly slot instead of retrying every minute."""
    await db[TESTS].update_one(key, {
        "$set": {"tracked_at": now, "track_error": {"code": code, "message": message[:300], "at": now}},
        "$unset": {"retry_after": ""},
    })


async def run_creatives_tick(now_utc: datetime | None = None) -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    deadline = time.monotonic() + TICK_BUDGET_SECONDS
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        result: dict = {}
        # Posting first: a Post click should not wait behind a long tracking pass.
        try:
            result["posting"] = await run_post_jobs(
                db, mongo_client, now_utc, deadline=time.monotonic() + POST_BUDGET_SECONDS)
        except Exception as e:
            logger.error(f"[creatives-post] tick failed: {e}", exc_info=True)
            result["posting"] = {"error": str(e)[:300]}
        try:
            budget = max(0.0, min(STOCK_LINES_BUDGET_SECONDS, deadline - time.monotonic()))
            result["stock_lines"] = await asyncio.wait_for(
                process_queued_lines(db, mongo_client, now_utc, deadline=time.monotonic() + budget), timeout=budget)
        except asyncio.TimeoutError:
            logger.info("[stock-lines] budget of %ss used, the next tick goes on", STOCK_LINES_BUDGET_SECONDS)
            result["stock_lines"] = {"timed_out": True}
        except Exception as e:
            logger.error(f"[stock-lines] tick failed: {e}", exc_info=True)
            result["stock_lines"] = {"error": str(e)[:300]}
        result["tracking"] = await track_due_tests(db, mongo_client, now_utc, deadline=deadline)
        day = _naive_utc(now_utc).date().isoformat()
        # Claim the once-per-UTC-day reconcile; only the first tick of the day wins.
        claimed = await db[COUNTERS].find_one_and_update(
            {"_id": "__daily__", "reconciled_on": {"$ne": day}},
            {"$set": {"reconciled_on": day}},
        )
        if claimed is None and await db[COUNTERS].find_one({"_id": "__daily__"}) is None:
            try:
                await db[COUNTERS].insert_one({"_id": "__daily__", "reconciled_on": day})
                claimed = True
            except Exception:  # lost the insert race: another tick has it
                claimed = None
        if claimed is not None:
            result["reconcile"] = await reconcile_all(db)
            try:
                result["media_cleanup"] = await cleanup_archived_media(db, now_utc)
            except Exception as e:
                logger.warning("creative media cleanup failed: %s", e)
        return result
