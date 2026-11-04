"""
ai/tools/creative_tools.py
--------------------------
Birdy chat tools for creative tests. Executors take (db, user_id, mongo_client=None, **args);
user_id always comes from the orchestrator, never from model arguments, and every query is
owner-scoped. Search, accounts, ad sets and the draft-only rules live in
services/creatives/reads.py, shared with routers/creatives.py.
"""

import json
import logging
import re
from datetime import date, datetime

from ai.suggestions.agents.useless_ad_purger import _symbol
from ai.tools.registry import registry
from integrations.facebook_utils.meta_creative_reader import (
    MetaReadError,
    list_lead_forms,
    list_promote_pages,
)
from services.creatives import posting, reads, store
from services.creatives.targets import owning_group, prefill_targets
from services.creatives.verdict import user_currency

logger = logging.getLogger(__name__)
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
    test = await (store.get_test_by_code if CODE_RE.match(ref) else store.get_test)(db, user_id, ref)
    return None if test and test.get("managed_by") else test


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


# ── write tools ──────────────────────────────────────────────────────────────

SESSIONS = "ai_chat_sessions"
MODES = ("thresholds", "baseline", "cpl_only")
DESTINATIONS = ("lead_form", "website", "whatsapp")
NO_ATTACHMENT = "no attachment"


def _confirmation_required(action: str) -> dict:
    # Hard gate, independent of the system prompt: these act on real Meta ad accounts.
    return {
        "error": "confirmation_required",
        "message": (
            f"Do NOT {action} yet. Call draft_creative_test (or get_creative_test) so the user sees "
            "the confirm card, and only after the user explicitly confirms in their latest message "
            "(they click the card's Post button or tell you to post) call this tool again with "
            "confirmed=true."
        ),
    }


def _targets(raw) -> tuple[dict | None, str | None]:
    """Normalize chat targets. `{"cpl": 25}` is shorthand for metrics.cpl lte 25."""
    if raw in (None, {}):
        return {}, None
    if not isinstance(raw, dict):
        return None, "targets must be an object"
    out = dict(raw)
    cpl = out.pop("cpl", None)
    if cpl is not None:
        if isinstance(cpl, bool) or not isinstance(cpl, (int, float)) or cpl <= 0:
            return None, "cpl must be a positive number"
        out["metrics"] = {**(out.get("metrics") or {}), "cpl": {"op": "lte", "value": cpl}}
    if out.get("mode") not in (None, *MODES):
        return None, f"mode must be one of {', '.join(MODES)}"
    return out, None


MONEY_METRICS = ("cpl", "cpc")


def _opt(d: dict, key: str, default):
    v = d.get(key)
    return default if v is None else v


def _n(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def _targets_text(targets: dict, sym: str = "") -> str:
    """Targets are in the user's currency: money metrics and the spend floor carry its symbol."""
    parts = []
    for key, m in (targets.get("metrics") or {}).items():
        if isinstance(m, dict) and m.get("value") is not None:
            money = sym if key in MONEY_METRICS else ""
            parts.append(f"{key.upper()} {'<=' if m.get('op') == 'lte' else '>='} {money}{_n(m['value'])}")
    if not parts:
        parts.append("no metric targets")
    parts.append(f"min spend {sym}{_n(_opt(targets, 'min_spend', 150))} / {_n(_opt(targets, 'min_days', 7))} days")
    if targets.get("mode") == "cpl_only":
        parts.append("CPL only")
    elif targets.get("mode") == "baseline":
        parts.append(f"vs account baseline (+{targets.get('baseline_pct', 15)}%)")
    return " · ".join(parts)


def _destination_text(creative: dict) -> str:
    dest = creative.get("destination")
    if dest == "website":
        return f"Website: {creative.get('website_url') or 'URL missing'}"
    return {"lead_form": "Lead form", "whatsapp": "WhatsApp"}.get(dest, "Destination not set")


async def _attachment(db, user_id: str, session_id, index) -> dict | None:
    """Attachment `index` of this chat session (Task 29 stores them on the session doc)."""
    if not session_id or isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    doc = await db[SESSIONS].find_one(
        {"_id": session_id, "user_id": user_id}, {"session_attachments": 1})
    items = (doc or {}).get("session_attachments") or []
    return items[index] if index < len(items) else None


def _attachment_creative(att: dict) -> dict:
    media = {k: att[k] for k in ("blob_url", "mime", "bytes", "width", "height", "duration_s",
                                  "thumbnail_url") if att.get(k) is not None}
    fmt = "video" if str(att.get("mime", "")).startswith("video/") else "image"
    return {"format": fmt, "media": media}


def _ad_creative(ad: dict) -> dict:
    media = {"image_url": ad.get("image") or ad.get("thumbnail"), "thumbnail_url": ad.get("thumbnail"),
             "video_id": ad.get("video_id")}
    return {"format": "video" if ad.get("video_id") else "image",
            "media": {k: v for k, v in media.items() if v}}


async def _resolve_accounts(db, mongo_client, user_id, acts, destination, adset_ids=None,
                            page_ids=None, lead_form_id=None):
    """(accounts, needs). Auto-picks an ad set / page / lead form when exactly one fits; every
    id the model passes is checked against what Meta lists for that (owned) account."""
    adset_ids, page_ids = adset_ids or {}, page_ids or {}
    accounts, needs = [], []
    token = None
    for act in acts:
        groups = await reads.account_groups(db, user_id, act)  # ReadError if not owned
        token = token or await reads.meta_token(mongo_client, user_id)
        need = {"ad_account_id": act}
        adsets = await reads.account_adsets(db, mongo_client, user_id, act, destination)
        compatible = [a for a in adsets if a["compatible"]]
        adset = None
        if adset_ids.get(act):
            adset = next((a for a in compatible if a.get("id") == adset_ids[act]), None)
            if adset is None:
                raise reads.ReadError(f"Ad set {adset_ids[act]} is not a compatible ad set of {act}")
        elif len(compatible) == 1:
            adset = compatible[0]
        else:
            need["adsets"] = [{"id": a.get("id"), "name": a.get("name"),
                               "campaign": (a.get("campaign") or {}).get("name")} for a in compatible]
        try:
            pages = await list_promote_pages(act, token)
            page = None
            if page_ids.get(act):
                page = next((p for p in pages if p.get("id") == page_ids[act]), None)
                if page is None:
                    raise reads.ReadError(f"Page {page_ids[act]} is not a promote page of {act}")
            elif len(pages) == 1:
                page = pages[0]
            else:
                need["pages"] = [{"id": p.get("id"), "name": p.get("name")} for p in pages]
            form = None
            if page and destination == "lead_form":
                forms = [f for f in await list_lead_forms(page["id"], token)
                         if f.get("status") in (None, "ACTIVE")]
                if lead_form_id:
                    form = next((f for f in forms if f.get("id") == lead_form_id), None)
                    if form is None:
                        raise reads.ReadError(f"Lead form {lead_form_id} is not an active form of that page")
                elif len(forms) == 1:
                    form = forms[0]
                else:
                    need["lead_forms"] = [{"id": f.get("id"), "name": f.get("name")} for f in forms]
        except MetaReadError as e:
            raise reads.meta_error(e)
        if len(need) > 1:
            needs.append(need)
            continue
        campaign_id = (adset.get("campaign") or {}).get("id")
        row = {"ad_account_id": act, "adset_id": adset["id"], "adset_name": adset.get("name"),
               "page_id": page["id"], "campaign_id": campaign_id,
               "client_group_id": owning_group(groups, campaign_id).get("id")}
        if form:
            row["lead_form_id"] = form["id"]
        accounts.append({k: v for k, v in row.items() if v is not None})
    return accounts, needs


def _winner_text(targets: dict, sym: str) -> str:
    share = round(float(_opt(targets, "win_share", 0.6)) * 100)
    return (f"{share}% of scored accounts pass, after {sym}{_n(_opt(targets, 'min_spend', 150))} "
            f"spend or {_n(_opt(targets, 'min_days', 7))} days")


def _card(test: dict, accounts: list[dict], names: dict, n_accounts: int, warnings: list[str],
          ready: bool = True, currency: str | None = None) -> dict:
    creative = test.get("creative") or {}
    targets = test.get("targets") or {}
    sym = _symbol(currency) if currency else ""
    return {
        "test_id": test["id"], "code": test["code"], "name": test.get("name"),
        "thumbnail": _thumbnail(test), "origin": test.get("origin"),
        "accounts": [{"name": names.get(a["ad_account_id"], a["ad_account_id"]),
                      "adset_name": a.get("adset_name")} for a in accounts],
        "targets_text": _targets_text(targets, sym),
        "winner_text": _winner_text(targets, sym),
        "destination_text": _destination_text(creative),
        "warnings": warnings,
        "n_accounts": n_accounts, "ready": ready,
        "posts_as": f"Paused, then live together once all {n_accounts} accept",
    }


async def draft_creative_test(db, user_id, mongo_client=None, name=None, origin=None, destination=None,
                              ad_account_ids=None, source_ad_id=None, attachment_index=None,
                              primary_text=None, headline=None, cta_type=None, website_url=None,
                              adset_ids=None, page_ids=None, lead_form_id=None, targets=None,
                              test_id=None, session_id=None, **_):
    existing = None
    if test_id:
        existing = await _resolve_test(db, user_id, test_id)
        if existing is None:
            return _fail("Creative test not found")
        if existing.get("status") != store.DRAFT:
            return _fail("Only a draft can be changed; this test is already posted")
        origin = origin or existing.get("origin")
        destination = destination or (existing.get("creative") or {}).get("destination")
    if origin not in ("new", "revive"):
        return _fail("origin must be 'new' or 'revive'")
    if destination not in DESTINATIONS:
        return _fail("destination must be lead_form, website or whatsapp")
    if not existing and not (name or "").strip():
        return _fail("name is required")
    norm_targets, err = _targets(targets)
    if err:
        return _fail(err)

    acts = list(ad_account_ids or [])
    if not acts and existing:
        acts = list(existing.get("requested_accounts")
                    or [a["ad_account_id"] for a in existing.get("draft_accounts") or []])
    if not acts:
        return _fail("ad_account_ids is required (use list_ad_accounts)")
    acts = list(dict.fromkeys(acts))

    # media
    creative_base = dict((existing or {}).get("creative") or {})
    source = (existing or {}).get("source")
    attachment_creative = None
    if attachment_index is not None:
        att = await _attachment(db, user_id, session_id, attachment_index)
        if att is None:
            return _fail(NO_ATTACHMENT, message="No attachment at that index in this chat. "
                         "Ask the user to attach the image or video.")
        attachment_creative = _attachment_creative(att)
    if origin == "revive" and (source_ad_id or not source):
        ad = await reads.find_ad(db, user_id, str(source_ad_id or ""))
        if ad is None:
            return _fail("Source ad not found (use find_ads)")
        source = {"ad_id": ad["ad_id"], "ad_account_id": ad["ad_account_id"],
                  "client_group_id": ad.get("client_group_id")}
        base = _ad_creative(ad)
        creative_base = {**creative_base, **base, "primary_text": creative_base.get("primary_text") or ad.get("body"),
                         "headline": creative_base.get("headline") or ad.get("title")}
    if attachment_creative:
        creative_base.update(attachment_creative)
    if origin == "new" and not creative_base.get("media"):
        return _fail(NO_ATTACHMENT, message="A brand-new ad needs media. "
                     "Ask the user to attach the image or video.")

    creative = {**creative_base, "destination": destination}
    for key, val in (("primary_text", primary_text), ("headline", headline), ("cta_type", cta_type),
                     ("website_url", website_url)):
        if val is not None:
            creative[key] = val

    try:
        accounts, needs = await _resolve_accounts(
            db, mongo_client, user_id, acts, destination, adset_ids, page_ids, lead_form_id)
    except reads.ReadError as e:
        return _fail(e.message)

    if not existing:
        if not norm_targets.get("metrics"):
            gids = [a["client_group_id"] for a in accounts if a.get("client_group_id")]
            pre = await prefill_targets(db, user_id, gids)
            cpl = ((pre.get("metrics") or {}).get("cpl") or {})
            if cpl.get("value") is not None:
                norm_targets["metrics"] = pre["metrics"]
        test = await store.create_test(
            db, user_id, name=name.strip(), origin=origin, creative=creative, targets=norm_targets,
            source=source, created_via="chat")
        test = await store.update_test(
            db, user_id, test["id"],
            {"draft_accounts": accounts, "requested_accounts": acts, "draft_needs": needs})
    else:
        fields = {"creative": creative, "draft_accounts": accounts, "requested_accounts": acts,
                  "draft_needs": needs}
        if name:
            fields["name"] = name.strip()
        if norm_targets:
            fields["targets"] = norm_targets
        if source != existing.get("source"):
            fields["source"] = source
        if origin != existing.get("origin"):
            fields["origin"] = origin
        try:
            fields = reads.prepare_patch(existing, fields)
        except reads.ReadError as e:
            return _fail(e.message)
        test = await store.update_test(db, user_id, existing["id"], fields)

    warnings = []
    if not (test.get("targets") or {}).get("metrics"):
        warnings.append("No metric targets set; ask the user for a CPL target.")
    if destination == "website" and not creative.get("website_url"):
        warnings.append("Website URL is missing.")
    if not _thumbnail(test):
        warnings.append("No preview image for this creative.")
    for need in needs:
        warnings.append(f"{need['ad_account_id']} still needs an ad set, page or lead form.")
    if creative.get("format") == "video":
        for a in accounts:
            if posting.media_plan(test, a["ad_account_id"])[0] == "unavailable":
                warnings.append(f"{a['ad_account_id']}: {posting.VIDEO_NEEDS_UPLOAD}.")
    names = {r["ad_account_id"]: r["name"] for r in await reads.ad_accounts(db, user_id)}
    ready = not needs and bool(accounts)
    card = _card(test, accounts, names, len(acts), warnings, ready,
                 currency=await user_currency(user_id, db))
    out = {
        "draft": {"test_id": test["id"], "code": test["code"], "name": test.get("name"),
                  "status": test.get("status"), "ready": ready},
        "card": card,
        "creative_block": ":::creative " + json.dumps(card) + ":::",
    }
    if needs:
        out["needs"] = needs
    return _json(out)


async def post_creative_test(db, user_id, mongo_client=None, test_id=None, confirmed=False, **_):
    if confirmed is not True:
        return _confirmation_required("post this creative test")
    test = await _resolve_test(db, user_id, test_id)
    if test is None:
        return _fail("Creative test not found")
    accounts = test.get("draft_accounts") or []
    if not accounts:
        return _fail("This draft has no accounts yet; call draft_creative_test first")
    requested = test.get("requested_accounts") or [a["ad_account_id"] for a in accounts]
    resolved = {a["ad_account_id"] for a in accounts}
    open_needs = test.get("draft_needs") or [{"ad_account_id": a} for a in requested if a not in resolved]
    if open_needs or not set(requested) <= resolved:
        return {"error": "draft_not_ready", "needs": open_needs,
                "message": "Some requested accounts still need an ad set, page or lead form. "
                           "Ask the user, then call draft_creative_test again. Nothing was posted."}
    try:
        res = await reads.enqueue_accounts(db, user_id, test["id"], accounts, add_accounts=False)
    except posting.PostingError as e:
        return _fail(e.message)
    return _json(res)


async def scale_creative_test(db, user_id, mongo_client=None, test_id=None, ad_account_ids=None,
                              confirmed=False, adset_ids=None, page_ids=None, lead_form_id=None, **_):
    if confirmed is not True:
        return _confirmation_required("add accounts to this creative test")
    test = await _resolve_test(db, user_id, test_id)
    if test is None:
        return _fail("Creative test not found")
    acts = list(dict.fromkeys(ad_account_ids or []))
    if not acts:
        return _fail("ad_account_ids is required")
    destination = (test.get("creative") or {}).get("destination")
    if destination not in DESTINATIONS:
        return _fail("This test has no destination, so ad sets cannot be matched")
    try:
        accounts, needs = await _resolve_accounts(
            db, mongo_client, user_id, acts, destination, adset_ids, page_ids, lead_form_id)
        if needs:
            return {"needs": needs, "message": "Nothing was posted. Ask the user to pick these, "
                    "then call scale_creative_test again."}
        res = await reads.enqueue_accounts(db, user_id, test["id"], accounts, add_accounts=True)
    except reads.ReadError as e:
        return _fail(e.message)
    except posting.PostingError as e:
        return _fail(e.message)
    return _json(res)


async def pause_creative_ads(db, user_id, mongo_client=None, test_id=None, ad_account_ids=None,
                             confirmed=False, **_):
    if confirmed is not True:
        return _confirmation_required("pause these ads")
    test = await _resolve_test(db, user_id, test_id)
    if test is None:
        return _fail("Creative test not found")
    try:
        res = await posting.pause_ads(db, mongo_client, user_id, test["id"], ad_account_ids)
    except posting.PostingError as e:
        return _fail(e.message)
    return _json(res)


async def update_creative_targets(db, user_id, mongo_client=None, test_id=None, targets=None, **_):
    test = await _resolve_test(db, user_id, test_id)
    if test is None:
        return _fail("Creative test not found")
    if test.get("status") == store.ARCHIVED:
        return _fail("This test is archived")
    norm, err = _targets(targets)
    if err or not norm:
        return _fail(err or "targets is required")
    fields = reads.prepare_patch(test, {"targets": norm})
    updated = await store.update_test(db, user_id, test["id"], fields)
    return _json({"test_id": updated["id"], "code": updated["code"], "targets": updated["targets"]})


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
    "draft_creative_test": (
        draft_creative_test,
        "Create or update a DRAFT creative test (never posts to Meta). Pass test_id to change an existing "
        "draft. Picks the ad set, page and lead form automatically when exactly one fits, otherwise returns "
        "`needs` with the options to ask the user about (use :::ui choice blocks) and you call it again with "
        "adset_ids / page_ids / lead_form_id. The result carries `creative_block`: include it verbatim in "
        "your reply so the user sees the confirm card.",
        {"type": "object", "properties": {
            "name": {"type": "string"},
            "origin": {"type": "string", "enum": ["new", "revive"],
                       "description": "new = fresh media attached to the chat; revive = copy an existing ad."},
            "source_ad_id": {"type": "string", "description": "Revive only: ad_id from find_ads."},
            "attachment_index": {"type": "integer",
                                 "description": "Index of an image/video the user attached in this chat."},
            "primary_text": {"type": "string"}, "headline": {"type": "string"},
            "cta_type": {"type": "string", "description": "Meta CTA, e.g. LEARN_MORE, SIGN_UP, WHATSAPP_MESSAGE."},
            "destination": {"type": "string", "enum": list(DESTINATIONS)},
            "website_url": {"type": "string"},
            "ad_account_ids": {"type": "array", "items": {"type": "string"}},
            "adset_ids": {"type": "object", "additionalProperties": {"type": "string"},
                          "description": "{ad_account_id: adset_id} when the user picked one."},
            "page_ids": {"type": "object", "additionalProperties": {"type": "string"},
                         "description": "{ad_account_id: page_id} when the user picked one."},
            "lead_form_id": {"type": "string"},
            "targets": {"type": "object", "description":
                "{cpl: 25} shorthand, or {mode, metrics:{cpl:{op:'lte',value}}, min_spend, min_days, win_share}."},
            "test_id": {"type": "string", "description": "Existing draft id or code to update."}},
         "required": ["name", "origin", "destination", "ad_account_ids", "targets"]},
    ),
    "post_creative_test": (
        post_creative_test,
        "Post a drafted creative test to Meta (ads are created PAUSED, then go live together). Acts on real "
        "ad accounts: NEVER set confirmed=true unless the user's latest message is the confirm card's "
        "confirmation or tells you to post. Without it the tool refuses.",
        {"type": "object", "properties": {
            "test_id": {"type": "string", "description": "Test id or code (CT-0042)."},
            "confirmed": {"type": "boolean"}},
         "required": ["test_id", "confirmed"]},
    ),
    "scale_creative_test": (
        scale_creative_test,
        "Add ad accounts to a posted creative test. Acts on real ad accounts: only with confirmed=true after "
        "the user explicitly confirmed which accounts. Without it the tool refuses.",
        {"type": "object", "properties": {
            "test_id": {"type": "string"},
            "ad_account_ids": {"type": "array", "items": {"type": "string"}},
            "adset_ids": {"type": "object", "additionalProperties": {"type": "string"}},
            "page_ids": {"type": "object", "additionalProperties": {"type": "string"}},
            "lead_form_id": {"type": "string"},
            "confirmed": {"type": "boolean"}},
         "required": ["test_id", "ad_account_ids", "confirmed"]},
    ),
    "pause_creative_ads": (
        pause_creative_ads,
        "Pause a creative test's live ads in some or all accounts. Acts on real ad accounts: only with "
        "confirmed=true after the user explicitly asked. Without it the tool refuses.",
        {"type": "object", "properties": {
            "test_id": {"type": "string"},
            "ad_account_ids": {"type": "array", "items": {"type": "string"},
                               "description": "Omit to pause every account."},
            "confirmed": {"type": "boolean"}},
         "required": ["test_id", "confirmed"]},
    ),
    "update_creative_targets": (
        update_creative_targets,
        "Change a creative test's targets or scoring floor. Birdy-side only (no Meta write); rescoring "
        "happens on the next tracking run.",
        {"type": "object", "properties": {
            "test_id": {"type": "string"},
            "targets": {"type": "object", "description":
                "{cpl: 25} shorthand, or {mode, metrics, min_spend, min_days, win_share, baseline_pct}."}},
         "required": ["test_id", "targets"]},
    ),
}

CREATIVE_WRITE_TOOLS = ("draft_creative_test", "post_creative_test", "scale_creative_test",
                        "pause_creative_ads", "update_creative_targets")
# Tools that need the chat session id (injected by the orchestrator, never from the model).
CREATIVE_SESSION_TOOLS = ("draft_creative_test",)

CREATIVE_READ_TOOLS = ("find_ads", "list_ad_accounts", "list_adsets",
                       "list_creative_tests", "get_creative_test")


def register_creative_tools(tool_registry=None):
    target = tool_registry or registry
    for name, (executor, description, parameters) in _TOOL_DEFS.items():
        target.register(name=name, description=description, parameters=parameters, executor=executor)
