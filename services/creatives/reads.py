"""Creative-test reads and validation shared by the HTTP router and Birdy's chat tools.

Everything here is owner-scoped (`user_id` is always an argument, never inferred) and
raises `ReadError` instead of HTTPException so both callers can map it their own way.
Meta access is injectable (`get_token`, `fetch_adsets`) so callers keep their own seams.
"""

from __future__ import annotations

from typing import Optional

from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_creative_reader import (
    MetaReadError,
    adset_compatible,
    list_adsets,
)
from services.creatives import posting, store
from services.creatives.naming import meta_ad_name, slugify
from services.creatives.targets import owning_group
from services.facebook_cache_shape import read_preset


class ReadError(Exception):
    def __init__(self, message: str, status_code: int = 400, detail=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail if detail is not None else message


async def account_groups(db, user_id: str, act: str) -> list[dict]:
    groups = await db["client_groups"].find(
        {"user_id": user_id, "meta_ad_account_id": act}, {"_id": 0}
    ).to_list(length=None)
    if not groups:
        raise ReadError("Ad account not found", 404)
    return groups


async def meta_token(mongo_client, user_id: str, get_token=None) -> str:
    fb = await (get_token or get_facebook_token)(user_id, mongo_client)
    token = (fb or {}).get("access_token")
    if not token:
        raise ReadError("Meta is not connected", 400)
    return token


def meta_error(e: MetaReadError) -> ReadError:
    return ReadError(str(e), 502, {"message": str(e), "code": e.code})


async def ad_accounts(db, user_id: str) -> list[dict]:
    """The user's agency accounts, once each, labelled with every client that shares them."""
    groups = await db["client_groups"].find(
        {"user_id": user_id, "meta_ad_account_id": {"$nin": [None, ""]}},
        {"_id": 0, "id": 1, "name": 1, "meta_ad_account_id": 1, "ad_account_currency": 1},
    ).to_list(length=None)
    rows: dict[str, dict] = {}
    for g in groups:
        row = rows.setdefault(g["meta_ad_account_id"], {
            "ad_account_id": g["meta_ad_account_id"], "name": None, "currency": None, "clients": []})
        row["clients"].append({"id": g["id"], "name": g.get("name")})
        row["currency"] = row["currency"] or g.get("ad_account_currency")
    for row in rows.values():
        row["name"] = ", ".join(c["name"] for c in row["clients"] if c["name"]) or row["ad_account_id"]
    return list(rows.values())


async def account_adsets(db, mongo_client, user_id: str, act: str, destination: str, *,
                         get_token=None, fetch_adsets=None) -> list[dict]:
    """Ad sets of an owned account, each flagged with destination compatibility and owner."""
    groups = await account_groups(db, user_id, act)
    token = await meta_token(mongo_client, user_id, get_token)
    try:
        adsets = await (fetch_adsets or list_adsets)(act, token)
    except MetaReadError as e:
        raise meta_error(e)
    out = []
    for a in adsets:
        ok, reason = adset_compatible(a, destination)
        owner = owning_group(groups, (a.get("campaign") or {}).get("id"))
        out.append({**a, "compatible": ok, "reason": reason, "owning_client_group_id": owner.get("id")})
    return out


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def rank(name: str, q: str):
    """0 exact code, 1 name prefix, 2 substring, None no match (everything is 3 without q)."""
    if not q:
        return 3
    n = (name or "").lower()
    if n.split(" | ", 1)[0].strip() == q:
        return 0
    if n.startswith(q):
        return 1
    return 2 if q in n else None


async def _cached_ads(db, user_id: str, ad_account_id: Optional[str]):
    query: dict = {"user_id": user_id, "meta_ad_account_id": {"$nin": [None, ""]}}
    if ad_account_id:
        query["meta_ad_account_id"] = ad_account_id
    groups = await db["client_groups"].find(query, {"_id": 0}).to_list(length=None)
    if ad_account_id and not groups:
        raise ReadError("Ad account not found", 404)
    return [(g, ad) for g in groups
            for ad in read_preset(g.get("facebook_cache") or {}, "last_30d").get("ads") or []]


def _hit(g: dict, ad: dict) -> dict:
    spend = _num(ad.get("spend"))
    leads = _num(ad.get("total_leads") or ad.get("results"))
    return {
        "ad_id": str(ad.get("id")), "name": ad.get("name"), "ad_account_id": g["meta_ad_account_id"],
        "currency": g.get("ad_account_currency"),
        "client_name": g.get("name"), "status": ad.get("status"),
        "thumbnail": ad.get("creative_thumbnail"), "image": ad.get("creative_image"),
        "video_id": ad.get("creative_video_id"), "title": ad.get("creative_title"),
        "body": ad.get("creative_body"),
        "metrics": {"spend": spend, "leads": leads, "cpl": spend / leads if leads else None},
    }


async def search_ads(db, user_id: str, q: str = "", ad_account_id: Optional[str] = None,
                     limit: int = 30) -> list[dict]:
    needle = (q or "").strip().lower()
    seen: set[str] = set()
    hits = []
    for g, ad in await _cached_ads(db, user_id, ad_account_id):
        tier = rank(ad.get("name"), needle)
        if tier is None or str(ad.get("id")) in seen:
            continue
        seen.add(str(ad.get("id")))
        hit = _hit(g, ad)
        hits.append((tier, -hit["metrics"]["spend"], hit))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [h[2] for h in hits[:limit]]


async def find_ad(db, user_id: str, ad_id: str) -> Optional[dict]:
    """One of the user's cached ads by Meta ad id, with its owning client group id."""
    for g, ad in await _cached_ads(db, user_id, None):
        if str(ad.get("id")) == str(ad_id):
            return {**_hit(g, ad), "client_group_id": g.get("id")}
    return None


def prepare_patch(test: dict, fields: dict) -> dict:
    """Apply the draft-only rules to a patch. Returns the fields to store or raises ReadError(409)."""
    fields = dict(fields)
    draft = test.get("status") == store.DRAFT
    if "targets" in fields:
        fields["targets"] = {**(test.get("targets") or {}), **fields["targets"]}
    if "creative" in fields and not draft:
        raise ReadError("The creative can only be changed while the test is a draft", 409)
    for locked in ("source", "origin"):
        if locked in fields and not draft:
            raise ReadError(f"{locked.capitalize()} can only be changed while the test is a draft", 409)
    if "slug" in fields:
        if not draft:
            raise ReadError("Slug can only be changed while the test is a draft", 409)
        slug = slugify(fields["slug"]) or "untitled"
        fields["slug"] = slug
        fields["meta_ad_name"] = meta_ad_name(test["code"], slug)
    return fields


async def enqueue_accounts(db, user_id: str, test_id: str, accounts: list[dict], *,
                           add_accounts: bool) -> dict:
    """Queue paused ad creation: first post (draft tests) or add accounts (posted tests).
    Raises posting.PostingError, which owns the validation and the 404/409 codes."""
    return await posting.enqueue_post(
        db, user_id, test_id, accounts=accounts, add_accounts=add_accounts)
