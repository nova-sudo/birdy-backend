"""
ai/tools/creative_tools.py
--------------------------
Birdy chat tools for creative tests. Executors take (db, user_id, mongo_client=None, **args);
user_id always comes from the orchestrator, never from model arguments, and every query is
owner-scoped. Search, accounts, ad sets and the draft-only rules live in
services/creatives/reads.py, shared with routers/creatives.py.
"""

import re
from datetime import date, datetime

from ai.tools.registry import registry
from services.creatives import reads, store

CODE_RE = re.compile(r"^CT-\d+$", re.IGNORECASE)


def _json(value):
    """Strip Mongo ids, make dates ISO strings."""
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _fail(error: str, **extra) -> dict:
    return {"error": error, **extra}


async def _resolve_test(db, user_id: str, ref) -> dict | None:
    """A test of this user by id or by code ("CT-0042")."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if CODE_RE.match(ref):
        return await store.get_test_by_code(db, user_id, ref)
    return await store.get_test(db, user_id, ref)


def _thumbnail(test: dict):
    creative = test.get("creative") or {}
    media = {**creative, **(creative.get("media") or {})}
    return media.get("thumbnail_url") or media.get("image_url")


def _test_row(test: dict) -> dict:
    return _json({
        "id": test["id"], "code": test.get("code"), "name": test.get("name"),
        "status": test.get("status"), "verdict": test.get("verdict"), "origin": test.get("origin"),
        "summary": test.get("summary"), "thumbnail": _thumbnail(test),
        "created_at": test.get("created_at"), "started_at": test.get("started_at"),
    })


# ── read tools ───────────────────────────────────────────────────────────────

async def find_ads(db, user_id, mongo_client=None, q="", ad_account_id=None, limit=10, **_):
    try:
        limit = max(1, min(int(limit or 10), 25))
        hits = await reads.search_ads(db, user_id, str(q or ""), ad_account_id, limit)
    except reads.ReadError as e:
        return _fail(e.message)
    return {"ads": [{k: v for k, v in h.items() if k != "image"} for h in hits]}


async def list_ad_accounts(db, user_id, mongo_client=None, **_):
    return {"ad_accounts": await reads.ad_accounts(db, user_id)}


async def list_adsets(db, user_id, mongo_client=None, ad_account_id=None, destination=None, **_):
    if destination not in ("lead_form", "website", "whatsapp"):
        return _fail("destination must be lead_form, website or whatsapp")
    try:
        rows = await reads.account_adsets(db, mongo_client, user_id, ad_account_id, destination)
    except reads.ReadError as e:
        return _fail(e.message)
    return {"adsets": [{
        "id": a.get("id"), "name": a.get("name"), "status": a.get("effective_status") or a.get("status"),
        "campaign": (a.get("campaign") or {}).get("name"), "compatible": a["compatible"],
        "reason": a["reason"]} for a in rows]}


async def list_creative_tests(db, user_id, mongo_client=None, status=None, **_):
    tests = await store.list_tests(db, user_id, status=status or None)
    return {"tests": [_test_row(t) for t in tests]}


async def get_creative_test(db, user_id, mongo_client=None, code_or_id=None, **_):
    test = await _resolve_test(db, user_id, code_or_id)
    if test is None:
        return _fail("Creative test not found")
    ads = await store.list_test_ads(db, test["id"], user_id=user_id)
    return _json({
        **_test_row(test), "targets": test.get("targets"), "slug": test.get("slug"),
        "creative": {k: v for k, v in (test.get("creative") or {}).items() if k != "media"},
        "ads": [{
            "ad_account_id": a.get("ad_account_id"), "adset_name": a.get("adset_name"),
            "post_state": a.get("post_state"), "post_error": a.get("post_error"),
            "meta_status": a.get("meta_status"), "score": a.get("score"),
            "ad_id": (a.get("meta") or {}).get("ad_id")} for a in ads],
    })


# ── schemas ──────────────────────────────────────────────────────────────────

_TOOL_DEFS = {
    "find_ads": (
        find_ads,
        "Search the user's cached Meta ads across their ad accounts by name, ad code (CT-0042) or text, "
        "with 30-day spend/leads/CPL. Use it to find an ad to revive as a creative test.",
        {"type": "object", "properties": {
            "q": {"type": "string", "description": "Name, code or text to search for. Empty lists top spenders."},
            "ad_account_id": {"type": "string", "description": "Limit to one ad account (act_...)."},
            "limit": {"type": "integer", "description": "Max results, default 10."}}},
    ),
    "list_ad_accounts": (
        list_ad_accounts,
        "List the user's Meta ad accounts with the client names that share each one.",
        {"type": "object", "properties": {}},
    ),
    "list_adsets": (
        list_adsets,
        "List the ad sets of one ad account, flagged with whether each can serve a destination "
        "(lead_form, website or whatsapp).",
        {"type": "object", "properties": {
            "ad_account_id": {"type": "string"},
            "destination": {"type": "string", "enum": ["lead_form", "website", "whatsapp"]}},
         "required": ["ad_account_id", "destination"]},
    ),
    "list_creative_tests": (
        list_creative_tests,
        "List the user's creative tests (code, status, verdict, summary). Archived tests are hidden unless "
        "status is 'archived'.",
        {"type": "object", "properties": {"status": {"type": "string", "description":
            "draft, posting, testing, winner, loser, inconclusive or archived."}}},
    ),
    "get_creative_test": (
        get_creative_test,
        "One creative test with its per-account ads and scores. Accepts a code like CT-0042 or a test id.",
        {"type": "object", "properties": {"code_or_id": {"type": "string"}}, "required": ["code_or_id"]},
    ),
}

CREATIVE_READ_TOOLS = ("find_ads", "list_ad_accounts", "list_adsets",
                       "list_creative_tests", "get_creative_test")


def register_creative_tools(tool_registry=None):
    target = tool_registry or registry
    for name, (executor, description, parameters) in _TOOL_DEFS.items():
        target.register(name=name, description=description, parameters=parameters, executor=executor)
