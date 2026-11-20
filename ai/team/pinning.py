"""
ai/team/pinning.py
------------------
Client pinning for the tools that take no client argument (design section 8: "An agent asked about
Aura cannot read another client").

The orchestrator pins every client-filtered read by overwriting its group argument
(_AGENT_PINNED_GROUP_TOOLS). The tools here take no group argument at all, or are keyed by ad
account, so in a one-client agent run they are not executed as usual: `run_pinned` runs a
client-scoped version instead, which resolves the client's own ad account server-side and refuses
or filters everything else.

  get_alerts           only alerts that target this client; other clients' names are removed, and an
                       alert that also covers other clients loses its trigger history (it mixes them)
  find_ads             only this client's cached ads (its own campaigns on a shared account)
  list_ad_accounts     only this client's ad account, listing only this client
  list_adsets          only this client's ad account (any other is refused), only its ad sets
  list_creative_tests  only tests with an ad posted for this client
  get_creative_test    refused unless the test has an ad for this client; other clients' ads removed
  list_lessons         this client's lessons and the agency-wide ones, never another client's

Client-named tools (Birdy's client note and target tools) take the client's id instead:
CLIENT_ARG_TOOLS have their `client` argument overwritten, the same way group ids are.
"""

import json

PINNED_TOOLS = frozenset({"get_alerts", "find_ads", "list_ad_accounts", "list_adsets",
                          "list_creative_tests", "get_creative_test", "list_lessons"})

# Tools whose `client` argument names the client (an id or a name): pinned to the run's client id.
CLIENT_ARG_TOOLS = frozenset({"get_client", "get_client_notes", "add_client_note", "delete_client_note",
                              "set_client_target"})


def _dump(value) -> str:
    return json.dumps(value, default=str)


def _refuse(text: str) -> str:
    return _dump({"error": text})


async def _group(db, user_id: str, gid: str) -> dict | None:
    return await db["client_groups"].find_one({"user_id": user_id, "id": gid}, {"_id": 0})


async def _account_shared(db, user_id: str, act: str, gid: str) -> bool:
    return await db["client_groups"].count_documents(
        {"user_id": user_id, "meta_ad_account_id": act, "id": {"$ne": gid}}) > 0


# ── alerts ────────────────────────────────────────────────────────────────────

async def _alerts(db, user_id, gid, group, args, **_):
    from ai.tools.alert_tools import get_alerts
    status = args.get("status")
    q = {"user_id": user_id, "target_group_ids": gid}
    if status:
        q["status"] = status
    docs = {d.get("id"): d for d in await db["alerts"].find(q, {"_id": 0}).to_list(None)}
    # The listing's shape comes from the one place that builds it, kept to this client's alerts.
    listing = await get_alerts(db, user_id, status=status)
    name = group.get("name")
    out, counts = [], {"total": 0, "active": 0, "triggered": 0, "paused": 0}
    for a in listing.get("alerts") or []:
        doc = docs.get(a.get("id"))
        if doc is None:
            continue
        row = dict(a, target_group_names=[name] if name else [])
        if [g for g in doc.get("target_group_ids") or [] if g != gid]:
            # Its trigger history counts every client it covers.
            row.update(last_triggered_at=None, trigger_count=None,
                       note="This alert also covers other clients; its trigger history is not shown here.")
        out.append(row)
        s = row.get("status") or "active"
        counts["total"] += 1
        if s in counts:
            counts[s] += 1
    return {"alerts": out, "counts": counts}


# ── ads and accounts ──────────────────────────────────────────────────────────

def _no_account(group: dict) -> str:
    return _refuse(f"{group.get('name') or 'This client'} has no Meta ad account connected.")


async def _find_ads(db, user_id, gid, group, args, **_):
    from services.creatives import reads
    from services.facebook_cache_shape import read_preset
    act = group.get("meta_ad_account_id")
    if not act:
        return {"ads": []}
    if args.get("ad_account_id") and args["ad_account_id"] != act:
        return {"error": "That ad account is not this client's. Only this client's ads can be searched here.",
                "ad_account_id": act}
    try:
        limit = max(1, min(int(args.get("limit") or 10), 25))
    except (TypeError, ValueError):
        limit = 10
    needle = str(args.get("q") or "").strip().lower()
    seen, hits = set(), []
    for ad in read_preset(group.get("facebook_cache") or {}, "last_30d").get("ads") or []:
        tier = reads.rank(ad.get("name"), needle)
        if tier is None or str(ad.get("id")) in seen:
            continue
        seen.add(str(ad.get("id")))
        hit = reads._hit(group, ad)
        hits.append((tier, -hit["metrics"]["spend"], hit))
    hits.sort(key=lambda h: (h[0], h[1]))
    return {"ads": [{k: v for k, v in h[2].items() if k != "image"} for h in hits[:limit]]}


async def _ad_accounts(db, user_id, gid, group, args, **_):
    act = group.get("meta_ad_account_id")
    if not act:
        return {"ad_accounts": []}
    return {"ad_accounts": [{"ad_account_id": act, "name": group.get("name") or act,
                             "currency": group.get("ad_account_currency"),
                             "clients": [{"id": gid, "name": group.get("name")}]}]}


async def _adsets(db, user_id, gid, group, args, mongo_client=None, **_):
    act = group.get("meta_ad_account_id")
    if not act:
        return None
    asked = args.get("ad_account_id")
    if asked and asked != act:
        return {"error": "That ad account is not this client's. Use this client's own account.",
                "ad_account_id": act}
    from services.creatives import reads
    destination = args.get("destination")
    if destination not in ("lead_form", "website", "whatsapp"):
        return {"error": "destination must be lead_form, website or whatsapp"}
    try:
        rows = await reads.account_adsets(db, mongo_client, user_id, act, destination)
    except reads.ReadError as e:
        return {"error": e.message}
    return {"adsets": [{
        "id": a.get("id"), "name": a.get("name"), "status": a.get("effective_status") or a.get("status"),
        "campaign": (a.get("campaign") or {}).get("name"), "compatible": a["compatible"],
        "reason": a["reason"]} for a in rows if a.get("owning_client_group_id") == gid]}


# ── creative tests ────────────────────────────────────────────────────────────

async def _ad_is_this_clients(db, user_id, gid, group, ad: dict) -> bool:
    owner = ad.get("client_group_id")
    if owner:
        return owner == gid
    act = group.get("meta_ad_account_id")
    # An old row without its client: only when the account is this client's alone.
    return bool(act) and ad.get("ad_account_id") == act and not await _account_shared(db, user_id, act, gid)


async def _client_test_ids(db, user_id, gid, group) -> set:
    from services.creatives.store import TEST_ADS
    act = group.get("meta_ad_account_id")
    q = {"user_id": user_id, "$or": [{"client_group_id": gid}]}
    if act:
        q["$or"].append({"ad_account_id": act, "client_group_id": {"$in": [None, ""]}})
    ids = set()
    async for ad in db[TEST_ADS].find(q, {"_id": 0, "test_id": 1, "client_group_id": 1, "ad_account_id": 1}):
        if await _ad_is_this_clients(db, user_id, gid, group, ad):
            ids.add(ad.get("test_id"))
    return ids


async def _creative_tests(db, user_id, gid, group, args, **_):
    from ai.tools.creative_tools import _test_row
    from services.creatives import store
    ids = await _client_test_ids(db, user_id, gid, group)
    tests = await store.list_tests(db, user_id, status=args.get("status") or None)
    return {"tests": [_test_row(t) for t in tests if t.get("id") in ids]}


async def _creative_test(db, user_id, gid, group, args, mongo_client=None, **_):
    from ai.tools.creative_tools import get_creative_test, resolve_test
    test = await resolve_test(db, user_id, args.get("code_or_id"))
    if test is None or test.get("id") not in await _client_test_ids(db, user_id, gid, group):
        return {"error": "Creative test not found for this client"}
    from services.creatives.store import TEST_ADS
    keep = set()
    async for ad in db[TEST_ADS].find({"test_id": test["id"], "user_id": user_id}):
        if await _ad_is_this_clients(db, user_id, gid, group, ad):
            keep.add(((ad.get("meta") or {}).get("ad_id"), ad.get("ad_account_id"), ad.get("adset_name")))
    out = await get_creative_test(db, user_id, mongo_client, code_or_id=test["id"])
    out["ads"] = [a for a in out.get("ads") or []
                  if (a.get("ad_id"), a.get("ad_account_id"), a.get("adset_name")) in keep]
    return out


# ── lessons ───────────────────────────────────────────────────────────────────

async def _lessons(db, user_id, gid, group, args, **_):
    from ai.tools.stock_lesson_tools import list_lessons
    return await list_lessons(db, user_id, only_client=gid)


_RUNNERS = {
    "get_alerts": _alerts,
    "find_ads": _find_ads,
    "list_ad_accounts": _ad_accounts,
    "list_adsets": _adsets,
    "list_creative_tests": _creative_tests,
    "get_creative_test": _creative_test,
    "list_lessons": _lessons,
}
assert set(_RUNNERS) == PINNED_TOOLS


async def run_pinned(name: str, args: dict, *, db, user_id: str, client_group_id: str, mongo_client=None) -> str:
    """The client-scoped result of a PINNED_TOOLS call, as the tool result string."""
    group = await _group(db, user_id, client_group_id)
    if group is None:
        return _refuse("This conversation's client was not found.")
    try:
        out = await _RUNNERS[name](db, user_id, client_group_id, group, dict(args or {}), mongo_client=mongo_client)
    except Exception as e:  # a read failing must not take down the reply
        return _refuse(f"{name} failed: {e}")
    if out is None:
        return _no_account(group)
    return _dump(out)


def pin_client_arg(name: str, args: dict, client_group_id: str) -> None:
    if name in CLIENT_ARG_TOOLS:
        args["client"] = client_group_id
