"""
ai/suggestions/creative_findings.py
-----------------------------------
CreativeTestJudge: deterministic findings for creative tests (spec §9), written
into ai_suggestions through the same store the weekly subagents use, so they
show on the dashboard and reach Slack with the existing apply/dismiss buttons.

Run by the creatives jobs, not the weekly suggestions cron:
  * tracking (track_test) and a failed post (posting._fail) call
    sync_creative_suggestions for one test;
  * the daily reconcile pass calls sync_adopt_suggestions per user.

Findings are derived only from the test doc and its creative_test_ads rows, so
a failed post (post_state "failed" + post_error) has one source of truth.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from ai.suggestions import store
from ai.suggestions.agents.useless_ad_purger import _symbol
from ai.suggestions.contracts import (
    ACTION_ADOPT_CREATIVE_AD,
    ACTION_PAUSE_ADS,
    ACTION_RETRY_CREATIVE_POST,
    ACTION_SCALE_CREATIVE,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_OPPORTUNITY,
    WINDOW_WEEKLY,
    Action,
    Evidence,
    Finding,
    Stat,
)
from services.creatives.store import LIVE_STATUSES

logger = logging.getLogger(__name__)

AGENT = "creative_test_judge"

RULE_WINNER = "creative_winner"
RULE_LOSER = "creative_loser"
RULE_ACCOUNT_FAIL = "creative_account_fail"
RULE_POST_FAILED = "creative_post_failed"
RULE_ADOPT = "creative_adopt"

_LIVE = LIVE_STATUSES


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _failing(ads: list[dict]) -> list[dict]:
    """Live ads scored fail that are not already paused."""
    return [a for a in ads
            if a.get("post_state") == "active" and (a.get("meta") or {}).get("ad_id")
            and (a.get("score") or {}).get("state") == "fail"
            and a.get("meta_status") != "PAUSED"]


def _client_name(groups_by_id: dict, gid: str | None) -> str:
    return (groups_by_id.get(gid) or {}).get("name") or "Client"


def _test_group(ads: list[dict]) -> str:
    """Test-level findings belong to the client group with the most test ads."""
    counts = Counter(a.get("client_group_id") for a in ads if a.get("client_group_id"))
    return counts.most_common(1)[0][0] if counts else ""


def _target(test: dict, ad: dict, groups_by_id: dict) -> dict:
    return {"object_id": ad["meta"]["ad_id"], "object_type": "ad",
            "name": f"{test.get('meta_ad_name') or test.get('code')} · "
                    f"{_client_name(groups_by_id, ad.get('client_group_id'))}"}


def _summary_stats(test: dict) -> list[Stat]:
    s = test.get("summary") or {}
    sym = _symbol(s.get("currency"))
    target = (((test.get("targets") or {}).get("metrics") or {}).get("cpl") or {}).get("value")
    cpl = s.get("cpl")
    stats = [Stat("CPL", f"{sym}{cpl:.2f}" if cpl is not None else f"— · {s.get('leads', 0)} leads",
                  bad=cpl is None or (target is not None and cpl > target))]
    if target is not None:
        stats.append(Stat("Target", f"{sym}{float(target):.2f}"))
    stats.append(Stat("Accounts passing", f"{s.get('accounts_pass', 0)}/{s.get('accounts_total', 0)}"))
    stats.append(Stat("Spent", f"{sym}{float(s.get('spend') or 0):.0f}"))
    return stats


def _account_stats(test: dict, ad: dict) -> list[Stat]:
    sc = ad.get("score") or {}
    sym = _symbol(ad.get("currency") or (test.get("summary") or {}).get("currency"))
    cpl = sc.get("cpl")
    stats = [Stat("CPL", f"{sym}{cpl:.2f}" if cpl is not None else f"— · {sc.get('leads') or 0} leads",
                  bad=True)]
    failed = [c["metric"] for c in sc.get("checks") or [] if not c.get("ok") and c.get("metric")]
    if failed:
        stats.append(Stat("Missed", ", ".join(failed), bad=True))
    stats.append(Stat("Spent", f"{sym}{float(sc.get('spend') or 0):.0f}"))
    return stats


def _finding(test: dict, *, rule_id: str, severity: str, title: str, description: str,
             client_group_id: str, groups_by_id: dict, stats: list[Stat], action: Action,
             icon: str, ad_account_id: str | None = None, raw: dict | None = None) -> Finding:
    key = f"{rule_id}:{test['id']}" + (f":{ad_account_id}" if ad_account_id else "")
    return Finding(
        agent=AGENT, client_group_id=client_group_id or "",
        client_name=_client_name(groups_by_id, client_group_id),
        severity=severity, title=title, description=description,
        evidence=Evidence(window=WINDOW_WEEKLY, stats=stats,
                          raw={"creative_test_id": test["id"], "code": test.get("code"),
                               **({"ad_account_id": ad_account_id} if ad_account_id else {}),
                               **(raw or {})}),
        action=action, icon=icon, dedup_key=key, rule_id=rule_id, confidence=0.9,
    )


def _retry_patch(test: dict, ad: dict) -> dict:
    """The one fix that can be applied without a choice: switch to the website URL."""
    fix = (ad.get("post_error") or {}).get("fix")
    has_url = ad.get("website_url") or (test.get("creative") or {}).get("website_url")
    return {"destination": "website"} if fix == "use_website_url" and has_url else {}


# ── rules ────────────────────────────────────────────────────────────────────

def findings_for_test(test: dict, ads: list[dict], groups_by_id: dict) -> list[Finding]:
    """Every finding the test reproduces right now (adopt findings come from reconcile)."""
    if test.get("status") not in (*_LIVE, "posting"):
        return []
    code = test.get("code") or "Creative test"
    name = test.get("name") or ""
    on_verdict = test.get("on_verdict") or {}
    verdict = test.get("verdict")
    out: list[Finding] = []
    tgid = _test_group(ads)

    if verdict == "winner":
        used = {a.get("ad_account_id") for a in ads}
        agency = {g.get("meta_ad_account_id") for g in groups_by_id.values()
                  if g.get("meta_ad_account_id")}
        unrun = sorted(agency - used)
        if unrun:
            out.append(_finding(
                test, rule_id=RULE_WINNER, severity=SEVERITY_OPPORTUNITY,
                title=f"{code} won — run it in more accounts",
                description=f"{name or code} passed in most accounts. "
                            f"{len(unrun)} of your ad accounts have not run it yet.",
                client_group_id=tgid, groups_by_id=groups_by_id, stats=_summary_stats(test),
                action=Action(ACTION_SCALE_CREATIVE, params={"test_id": test["id"]}),
                icon="trophy", raw={"unrun_ad_account_ids": unrun}))

    failing = _failing(ads)
    if verdict == "loser" and failing:
        out.append(_finding(
            test, rule_id=RULE_LOSER, severity=SEVERITY_HIGH,
            title=f"{code} lost — pause the failing ads",
            description=f"{name or code} missed its targets. Pause it in the "
                        f"{len(failing)} account(s) where it failed; passing accounts keep running.",
            client_group_id=tgid, groups_by_id=groups_by_id, stats=_summary_stats(test),
            action=Action(ACTION_PAUSE_ADS, targets=[_target(test, a, groups_by_id) for a in failing]),
            icon="pause-circle"))

    if test.get("status") == "testing" and on_verdict.get("suggest_pause_failing"):
        by_acct: dict[str, list[dict]] = {}
        for a in failing:
            by_acct.setdefault(a.get("ad_account_id"), []).append(a)
        for acct, acct_ads in sorted(by_acct.items(), key=lambda kv: str(kv[0])):
            gid = acct_ads[0].get("client_group_id")
            out.append(_finding(
                test, rule_id=RULE_ACCOUNT_FAIL, severity=SEVERITY_MEDIUM,
                title=f"{code} is failing for {_client_name(groups_by_id, gid)}",
                description=f"{name or code} missed its targets in this account. "
                            "Pause it here while the test continues elsewhere.",
                client_group_id=gid, groups_by_id=groups_by_id,
                stats=_account_stats(test, acct_ads[0]),
                action=Action(ACTION_PAUSE_ADS, targets=[_target(test, a, groups_by_id) for a in acct_ads]),
                icon="alert-triangle", ad_account_id=acct))

    for ad in ads:
        if ad.get("post_state") != "failed":
            continue
        err = ad.get("post_error") or {}
        gid = ad.get("client_group_id")
        f = _finding(
            test, rule_id=RULE_POST_FAILED, severity=SEVERITY_HIGH,
            title=f"{code} could not be posted to {_client_name(groups_by_id, gid)}",
            description=err.get("message") or "Meta rejected the ad.",
            client_group_id=gid, groups_by_id=groups_by_id,
            stats=[Stat("Step", err.get("step") or "—", bad=True)]
                  + ([Stat("Meta code", str(err["code"]))] if err.get("code") is not None else []),
            action=Action(ACTION_RETRY_CREATIVE_POST, params={
                "test_id": test["id"], "test_ad_id": ad["id"], "patch": _retry_patch(test, ad)}),
            icon="alert-octagon", ad_account_id=ad.get("ad_account_id"),
            raw={"test_ad_id": ad["id"], "post_error": err})
        # Each failure is its own card: a re-failure after an applied retry is new.
        if at := _failure_stamp(err.get("at")):
            f.dedup_key += f":{at}"
        out.append(f)
    return out


def _failure_stamp(at) -> str:
    if isinstance(at, datetime):
        return at.strftime("%Y%m%dT%H%M%S")
    return str(at) if at else ""


def scale_path(test_id: str) -> str:
    """Wizard step 2 prefilled from a winning test (dashboard redirect, Slack link)."""
    return f"/campaigns/creatives/new?scale={test_id}"


def adopt_findings(tests_by_id: dict, items: list[dict], groups_by_id: dict) -> list[Finding]:
    """One finding per adoptable hand-posted ad (items from reconcile.find_adoptable)."""
    out = []
    for item in items:
        test = tests_by_id.get(item["test_id"])
        if test is None:
            continue
        f = _finding(
            test, rule_id=RULE_ADOPT, severity=SEVERITY_MEDIUM,
            title=f"Track {item.get('ad_name') or item['ad_id']} in {test.get('code')}",
            description=f"This ad is named for {test.get('code')} but was posted by hand. "
                        "Link it so it counts towards the test.",
            client_group_id=item.get("client_group_id"), groups_by_id=groups_by_id,
            stats=[Stat("Ad account", str(item.get("ad_account_id") or "—"))],
            action=Action(ACTION_ADOPT_CREATIVE_AD, params={"test_id": test["id"], "ad_id": item["ad_id"]}),
            icon="link", raw={"ad_id": item["ad_id"]})
        f.dedup_key = f"{RULE_ADOPT}:{test['id']}:{item['ad_id']}"
        f.evidence.raw.pop("ad_account_id", None)
        out.append(f)
    return out


# ── persistence ──────────────────────────────────────────────────────────────

async def _groups(db, user_id: str) -> dict:
    rows = await db["client_groups"].find(
        {"user_id": user_id}, {"id": 1, "name": 1, "meta_ad_account_id": 1}).to_list(None)
    return {g["id"]: g for g in rows if g.get("id")}


async def _persist(db, user_id: str, findings: list[Finding]) -> tuple[set, list[dict], list[dict]]:
    """Upsert + log like the orchestrator. Returns (kept ids, created docs, refreshed Slack docs)."""
    keep, created_docs, updated_docs = set(), [], []
    for f in findings:
        doc, created, changed = await store.upsert_finding(db, user_id, f)
        if doc is None:
            continue  # applied, or dismissed within the cooldown
        keep.add(doc["_id"])
        if created:
            created_docs.append(doc)
            await store.log_activity(
                db, user_id, actor=store.ACTOR_BIRDY, kind=store.KIND_SUGGESTION_CREATED,
                title=f.title, client_name=f.client_name, client_group_id=f.client_group_id,
                ref_id=doc["_id"], window=f.evidence.window, label="Suggested by Birdy",
                rule_id=f.rule_id)
        elif changed and doc.get("slack_ts"):
            updated_docs.append(doc)
    return keep, created_docs, updated_docs


async def _notify(db, user_id: str, created_docs: list[dict], updated_docs: list[dict]) -> None:
    try:
        from services import slack_suggestion_notifier as slack
        if created_docs:
            await slack.post_new_suggestions(db, user_id, created_docs)
        if updated_docs:
            await slack.update_suggestions(db, user_id, updated_docs)
    except Exception as e:
        logger.warning("creative suggestions: Slack notify failed for %s: %s", user_id, e)


async def _open_ids(db, user_id: str, query: dict) -> set:
    rows = await db[store.SUGGESTIONS].find(
        {"user_id": user_id, "agent": AGENT, "status": store.STATUS_OPEN, **query}, {"_id": 1}
    ).to_list(None)
    return {r["_id"] for r in rows}


async def _auto_pause(db, mongo_client, user_id: str, test: dict, finding: Finding) -> str | None:
    """Pause the loser's failing ads once (test.auto_paused_at guards re-runs).

    Pauses by ad (never a whole account: a passing ad may share it). Returns
      "applied" — every target paused: the finding is stored applied by Birdy;
      "partial" — some or none paused: the caller keeps an open card for the
                  rest (`finding.action.targets` is narrowed to them) and flags
                  it auto_failed;
      None      — already auto-paused once, nothing done.
    """
    from services.creatives import posting
    from services.creatives.store import TESTS

    claimed = await db[TESTS].find_one_and_update(
        {"id": test["id"], "user_id": user_id, "auto_paused_at": None},
        {"$set": {"auto_paused_at": _utcnow()}})
    if claimed is None:
        return None
    targets = list(finding.action.targets)
    ad_ids = [t["object_id"] for t in targets]
    await posting.pause_ads(db, mongo_client, user_id, test["id"], None, ad_ids=ad_ids)
    rows = {r["meta"]["ad_id"]: r for r in await _rows_for(db, user_id, test["id"], ad_ids)}
    done = [t for t in targets if (rows.get(t["object_id"]) or {}).get("meta_status") == "PAUSED"]
    rest = [t for t in targets if t not in done]

    ref_id = store.suggestion_key(user_id, finding)
    if not rest:
        doc, _, _ = await store.upsert_finding(db, user_id, finding)
        if doc is None:
            return "applied"
        ref_id = doc["_id"]
        await store.mark_applied(
            db, user_id, ref_id, applied_targets=done, applied_action_type=ACTION_PAUSE_ADS,
            decided_by={"actor": store.ACTOR_BIRDY, "source": "creative_tracking", "by": None,
                        "at": _utcnow()})
    # One activity row per client group, so each group's daily pause cap counts it.
    by_group: dict[str, int] = {}
    for t in done:
        gid = (rows.get(t["object_id"]) or {}).get("client_group_id") or finding.client_group_id
        by_group[gid] = by_group.get(gid, 0) + 1
    groups_by_id = await _groups(db, user_id)
    for gid, n in sorted(by_group.items()):
        await store.log_activity(
            db, user_id, actor=store.ACTOR_BIRDY, kind=store.KIND_ACTION_APPLIED,
            title=f"{finding.title} ({n} ad{'s' if n != 1 else ''})",
            client_name=_client_name(groups_by_id, gid), client_group_id=gid, ref_id=ref_id,
            window=finding.evidence.window, label="Auto-run by Birdy", rule_id=finding.rule_id,
            applied_action_type=ACTION_PAUSE_ADS)
    if rest:
        finding.action.targets = rest
        return "partial"
    return "applied"


async def _rows_for(db, user_id: str, test_id: str, ad_ids: list[str]) -> list[dict]:
    from services.creatives.store import TEST_ADS
    return await db[TEST_ADS].find(
        {"user_id": user_id, "test_id": test_id, "meta.ad_id": {"$in": ad_ids}}).to_list(None)


async def close_test_items(db, user_id: str, test_id: str, keep: set | None = None, *,
                           include_adopt: bool = False) -> int:
    """Resolve this test's open items not in `keep` (adopt items belong to reconcile,
    except on archive: `include_adopt`).
    A direct, test-scoped update: a concurrent sync of another test can't close
    items it never read, and a live apply claim is never resolved."""
    now = _utcnow()
    res = await db[store.SUGGESTIONS].update_many(
        {"user_id": user_id, "agent": AGENT, "origin_window": WINDOW_WEEKLY,
         "status": store.STATUS_OPEN, "evidence.creative_test_id": test_id,
         "_id": {"$nin": list(keep or ())}, **store.unclaimed_filter(now),
         **({} if include_adopt else {"rule_id": {"$ne": RULE_ADOPT}})},
        {"$set": {"status": store.STATUS_RESOLVED, "resolved_at": now, "updated_at": now}},
    )
    return res.modified_count


async def sync_creative_suggestions(db, user_id: str, test: dict, ads: list[dict], *,
                                    mongo_client=None) -> int:
    """Write this test's findings and resolve the ones it no longer reproduces.
    Returns the number of findings upserted. `mongo_client` enables auto-pause."""
    groups_by_id = await _groups(db, user_id)
    findings = findings_for_test(test, ads, groups_by_id)

    partial = None
    on_verdict = test.get("on_verdict") or {}
    if mongo_client is not None and on_verdict.get("auto_pause_losers") and test.get("verdict") == "loser":
        loser = next((f for f in findings if f.rule_id == RULE_LOSER), None)
        if loser is not None:
            try:
                outcome = await _auto_pause(db, mongo_client, user_id, test, loser)
                if outcome == "applied":
                    findings.remove(loser)
                elif outcome == "partial":
                    partial = loser
            except Exception as e:
                logger.error("creative suggestions: auto-pause failed for %s: %s",
                             test["id"], e, exc_info=True)

    keep, created_docs, updated_docs = await _persist(db, user_id, findings)
    if partial is not None:
        await db[store.SUGGESTIONS].update_one(
            {"_id": store.suggestion_key(user_id, partial), "user_id": user_id},
            {"$set": {"auto_failed": True}})
    await close_test_items(db, user_id, test["id"], keep)
    await _notify(db, user_id, created_docs, updated_docs)
    return len(findings)


async def sync_adopt_suggestions(db, user_id: str, items: list[dict]) -> int:
    """One creative_adopt finding per adoptable ad; resolves adopt items no longer adoptable."""
    from services.creatives.store import TESTS

    test_ids = sorted({i["test_id"] for i in items})
    tests = await db[TESTS].find({"user_id": user_id, "id": {"$in": test_ids}}).to_list(None)
    findings = adopt_findings({t["id"]: t for t in tests}, items, await _groups(db, user_id))
    keep, created_docs, updated_docs = await _persist(db, user_id, findings)
    keep |= await _open_ids(db, user_id, {"rule_id": {"$ne": RULE_ADOPT}})
    await store.close_stale_open(db, user_id, AGENT, WINDOW_WEEKLY, keep)
    await _notify(db, user_id, created_docs, updated_docs)
    return len(findings)
