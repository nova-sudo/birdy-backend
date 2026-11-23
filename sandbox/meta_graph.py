"""
sandbox/meta_graph.py
---------------------
The engine behind the fake Graph API (sandbox/meta_app.py): Graph's object model over the
account's world, field expansion, edges with paging, insights, leads and writes.

    ctx = await load_ctx(cred, "v25.0")
    body = await handle(ctx, "GET", "/act_123/campaigns", {"fields": "name,insights.date_preset(today){spend}"})

Nodes (``Node.type``): user (``me``), business, account (``act_<n>``), campaign, adset, ad,
adcreative, lead, page, leadgen_form, video, adimage, pixel, ig_user. Objects created through
the fake (ads, creatives, images, videos, campaigns, ad sets) live in ``sandbox_state`` under kind
``meta_object`` and are merged into every listing; they never deliver (no insights, no leads).

Shapes follow Graph: numbers in insights are strings, edges are ``{"data": [...], "paging":
{"cursors": {"before", "after"}, "next"}}`` (``next`` only while there is more; it carries the
access token and cursor, as Graph's does), a nested edge or insights with nothing in it is left
out of its parent, a field that has no value is left out. Unknown fields are left out too (and
logged ``sandbox_unhandled_field``) so a product request for a field this fake does not know
still works. Dates are the account's (Europe/London); lead ``created_time`` is UTC, as Graph's.

Numbers are the world's: an account's insights for a range are the sum of its campaigns', which
are the sum of their ads', which are what the daily rows add up to, and the ``lead`` action count
equals the leads the ``leads`` edge lists for those ads and days.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode

from sandbox import meta_media as media
from sandbox import state
from sandbox.credentials import make_credential
from sandbox.fakes import ProviderError
from sandbox.meta_fields import FieldSpec, FieldsError, parse_fields
from sandbox.world import (ACTIVE, EPOCH, PAUSED, TZ, Ad, AdSet, Campaign, Client, Creative, DayMetrics, Lead,
                           World, world_for)

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
DEFAULT_VERSION = "v25.0"
DEFAULT_LIMIT = 25
MAX_LIMIT = 5000
OBJECT_KIND = "meta_object"        # sandbox_state kind for objects created through the fake
ATTRIBUTION = "7d_click_1d_view"
LEVELS = ("account", "campaign", "adset", "ad")
STATUSES = ("ACTIVE", "PAUSED", "ARCHIVED", "DELETED")
HIDDEN_BY_DEFAULT = ("ARCHIVED", "DELETED")   # Graph leaves these out of listings unless asked
CATEGORY = {"aesthetics": "Medical & health", "dental": "Dentist & dental office", "gym": "Gym/Physical fitness centre"}


# ---------------------------------------------------------------------------
# Errors (raised as ProviderError; meta_app turns them into Graph's error body)
# ---------------------------------------------------------------------------
def not_found(object_id: str, method: str = "get") -> ProviderError:
    return ProviderError(
        400, f"Unsupported {method} request. Object with ID '{object_id}' does not exist, cannot be loaded due "
             "to missing permissions, or does not support this operation. Please read the Graph API "
             "documentation at https://developers.facebook.com/docs/graph-api",
        code=100, error_subcode=33, type="GraphMethodException")


def bad_param(message: str, code: int = 100, **extra) -> ProviderError:
    return ProviderError(400, f"(#{code}) {message}", code=code, type="OAuthException", **extra)


def unhandled(what: str) -> ProviderError:
    logger.warning("sandbox_unhandled provider=meta %s", what)
    return ProviderError(400, f"sandbox: {what} is not implemented", code=100, type="GraphMethodException",
                         sandbox_unhandled=True)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
@dataclass
class _Index:
    """Ids the world does not index itself (fixed per user and day)."""
    creative_ad: dict
    video_ad: dict
    hash_ad: dict
    page: dict
    form: dict
    ig: dict
    pixel: dict


_indexes: dict = {}


def _digits(n: int, *parts) -> str:
    h = int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")
    return str(h % (10 ** n)).rjust(n, "0")


def image_hash(user_id: str, creative_id: str) -> str:
    """The image hash of a world image creative (an md5-style hex string, as Graph's)."""
    return hashlib.md5(f"sandbox|{user_id}|{creative_id}".encode()).hexdigest()


def ig_id(client: Client) -> str:
    return "17841" + _digits(10, "ig", client.meta_page_id)


def pixel_id(client: Client) -> str:
    return "7" + _digits(14, "pixel", client.meta_ad_account_id)


def _index(w: World) -> _Index:
    key = (w.user_id, w.today)
    hit = _indexes.get(key)
    if hit is not None:
        return hit
    idx = _Index({}, {}, {}, {}, {}, {}, {})
    for c in w.clients:
        idx.page[c.meta_page_id] = c.index
        idx.form[c.lead_form_id] = c.index
        idx.ig[ig_id(c)] = c.index
        idx.pixel[pixel_id(c)] = c.index
        for ad in w.ads(c):
            cr = ad.creative
            idx.creative_ad[cr.id] = ad.id
            if cr.video_id:
                idx.video_ad[cr.video_id] = ad.id
            else:
                idx.hash_ad[image_hash(w.user_id, cr.id)] = ad.id
    if len(_indexes) > 32:
        _indexes.clear()
    _indexes[key] = idx
    return idx


class Ctx:
    """One request's view: the credential, the world with writes applied, created objects."""

    def __init__(self, cred, world: World, created: dict, version: Optional[str] = None):
        self.cred = cred
        self.user_id = cred.user_id
        self.world = world
        self.created = created            # state key -> doc (objects made through the fake)
        self.version = version or DEFAULT_VERSION
        self.token = cred.raw
        self.index = _index(world)

    def created_of(self, type_: str, **match) -> list:
        out = [d for d in self.created.values() if d.get("type") == type_
               and all(str(d.get(k)) == str(v) for k, v in match.items())]
        return sorted(out, key=lambda d: d.get("created_time") or "", reverse=True)


async def load_ctx(cred, version: Optional[str] = None) -> Ctx:
    w = world_for(cred.user_id, overrides=await state.overrides(cred.user_id))
    docs = await state.list(cred.user_id, OBJECT_KIND)
    return Ctx(cred, w, {d["key"]: d["value"] for d in docs}, version)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _ts(dt: datetime) -> str:
    """Graph's timestamp format: 2026-11-20T14:03:11+0000."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _utc(dt: datetime) -> str:
    return _ts(dt.astimezone(timezone.utc))


def _made(d: date, object_id: str) -> datetime:
    """A stable time of day on ``d`` for an object made that day (09:00-11:59 London)."""
    return datetime.combine(d, time(9), TZ) + timedelta(minutes=int(_digits(4, "made", object_id)) % 180)


def _updated(ctx: Ctx, object_id: str, made: datetime) -> datetime:
    ov = ctx.world.overrides
    times = [at for _v, at in ov.status.get(object_id, ())] + [at for _v, at in ov.budget.get(object_id, ())]
    if object_id in ov.name:
        times.append(ov.name[object_id][1])
    return max([made, *times])


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        raise bad_param(f"Invalid date: {text!r}")


def preset_range(preset: str, w: World) -> tuple:
    """(since, until) of a Graph date_preset in the account's calendar."""
    today, p = w.today, (preset or "").strip().lower()
    wd = today.weekday()
    if p in ("maximum", "data_maximum", "lifetime"):
        return w.history_start, today
    if p == "today":
        return today, today
    if p == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    m = re.fullmatch(r"last_(\d+)d", p)
    if m and 0 < int(m.group(1)) <= 1000:
        return today - timedelta(days=int(m.group(1))), today - timedelta(days=1)
    if p == "this_week_mon_today":
        return today - timedelta(days=wd), today
    if p == "this_week_sun_today":
        return today - timedelta(days=(wd + 1) % 7), today
    if p == "last_week_mon_sun":
        s = today - timedelta(days=wd + 7)
        return s, s + timedelta(days=6)
    if p == "last_week_sun_sat":
        s = today - timedelta(days=(wd + 1) % 7 + 7)
        return s, s + timedelta(days=6)
    if p == "this_month":
        return today.replace(day=1), today
    if p == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if p in ("this_quarter", "last_quarter"):
        first = today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1)
        if p == "this_quarter":
            return first, today
        end = first - timedelta(days=1)
        return end.replace(month=((end.month - 1) // 3) * 3 + 1, day=1), end
    if p == "this_year":
        return today.replace(month=1, day=1), today
    if p == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    raise bad_param(f"date_preset must be one of the following values: today, yesterday, this_month, last_month, "
                    f"this_quarter, maximum, data_maximum, last_3d, last_7d, last_14d, last_28d, last_30d, last_90d, "
                    f"last_week_mon_sun, last_week_sun_sat, last_quarter, last_year, this_week_mon_today, "
                    f"this_week_sun_today, this_year (got {preset!r})")


def _json_param(value, what: str):
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise bad_param(f"Param {what} must be JSON (got {str(value)[:80]!r})")


def date_window(params: dict, w: World) -> tuple:
    """(since, until) asked for: time_range, else date_preset, else Graph's default last_30d.
    ``until`` is clamped to today (no future day has data)."""
    tr = params.get("time_range")
    if tr:
        tr = _json_param(tr, "time_range")
        if not isinstance(tr, dict) or "since" not in tr or "until" not in tr:
            raise bad_param("time_range must be a JSON object with since and until")
        since, until = _parse_day(tr["since"]), _parse_day(tr["until"])
    else:
        since, until = preset_range(params.get("date_preset") or "last_30d", w)
    if since > until:
        raise bad_param("time_range: since must be on or before until")
    return since, min(until, max(w.today, since))


def buckets(params: dict, w: World) -> list:
    """[(since, until)] per the time_increment (or time_ranges)."""
    trs = params.get("time_ranges")
    if trs:
        out = []
        for tr in _json_param(trs, "time_ranges") or []:
            out.append(date_window({"time_range": tr}, w))
        return out
    since, until = date_window(params, w)
    inc = str(params.get("time_increment") or "all_days").strip().lower()
    if inc in ("all_days", ""):
        return [(since, until)]
    if inc == "monthly":
        out, cur = [], since
        while cur <= until:
            nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
            out.append((cur, min(until, nxt - timedelta(days=1))))
            cur = nxt
        return out
    if not inc.isdigit() or not 1 <= int(inc) <= 90:
        raise bad_param("time_increment must be all_days, monthly or a number of days between 1 and 90")
    n, out, cur = int(inc), [], since
    while cur <= until:
        out.append((cur, min(until, cur + timedelta(days=n - 1))))
        cur += timedelta(days=n)
    return out


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
@dataclass
class Node:
    type: str
    obj: Any                  # a world object, a Client, a Lead, or a created doc (dict)
    ref: Any = None           # the ad behind a creative / video; the client behind an image

    @property
    def created(self) -> bool:
        return isinstance(self.obj, dict) and "type" in self.obj


def node_id(n: Node) -> str:
    o = n.obj
    if n.created:
        return o["id"]
    t = n.type
    if t == "user":
        return o.meta_user_id
    if t == "business":
        return o.meta_business_id
    if t == "account":
        return o.meta_ad_account_id
    if t in ("campaign", "adset", "ad", "adcreative", "lead"):
        return o.id
    if t == "video":
        return o.video_id
    if t == "page":
        return o.meta_page_id
    if t == "leadgen_form":
        return o.lead_form_id
    if t == "ig_user":
        return ig_id(o)
    if t == "pixel":
        return pixel_id(o)
    if t == "adimage":
        return f"{o['account'].meta_account_number}:{o['hash']}"
    raise KeyError(t)


def _account_of_id(w: World, act: str) -> Optional[Client]:
    act = str(act or "")
    if not act.startswith("act_"):
        act = "act_" + act
    c = w.client(act)
    return c if c is not None and c.meta_ad_account_id == act else None


def lookup(ctx: Ctx, oid: str) -> Optional[Node]:
    """The node an id names in this account's world (or among its created objects), or None."""
    w, idx, oid = ctx.world, ctx.index, str(oid or "").strip()
    if not oid:
        return None
    if oid in ("me", w.agency.meta_user_id):
        return Node("user", w.agency)
    if oid == w.agency.meta_business_id:
        return Node("business", w.agency)
    if oid.startswith("act_") or (oid.isdigit() and len(oid) == 15):
        c = _account_of_id(w, oid)
        if c is not None:
            return Node("account", c)
    obj = w.find(oid)
    if isinstance(obj, Campaign):
        return Node("campaign", obj)
    if isinstance(obj, AdSet):
        return Node("adset", obj)
    if isinstance(obj, Ad):
        return Node("ad", obj)
    if oid in idx.creative_ad:
        ad = w.find(idx.creative_ad[oid])
        return Node("adcreative", ad.creative, ad)
    if oid in idx.video_ad:
        ad = w.find(idx.video_ad[oid])
        return Node("video", ad.creative, ad)
    if oid in idx.page:
        return Node("page", w.clients[idx.page[oid]])
    if oid in idx.form:
        return Node("leadgen_form", w.clients[idx.form[oid]])
    if oid in idx.ig:
        return Node("ig_user", w.clients[idx.ig[oid]])
    if oid in idx.pixel:
        return Node("pixel", w.clients[idx.pixel[oid]])
    if len(oid) == 17 and oid.startswith("9"):
        lead = w.lead(oid)
        if lead is not None:
            return Node("lead", lead)
    doc = ctx.created.get(oid)
    if doc is not None:
        return Node(doc["type"], doc)
    if ":" in oid:   # an ad image id, "<account number>:<hash>"
        num, _, h = oid.partition(":")
        img = _image(ctx, _account_of_id(w, num), h)
        if img is not None:
            return img
    return None


def _image(ctx: Ctx, client: Optional[Client], h: str) -> Optional[Node]:
    if client is None:
        return None
    ad_id = ctx.index.hash_ad.get(h)
    if ad_id:
        ad = ctx.world.find(ad_id)
        if ad is not None and ad.client_index == client.index:
            return Node("adimage", {"hash": h, "account": client, "title": ad.creative.title,
                                    "created": _made(ad.created_on, ad.creative.id), "name": f"{ad.creative.id}.jpg"},
                        ad)
    doc = ctx.created.get(f"adimage:{h}")
    if doc and doc.get("account_id") == client.meta_ad_account_id:
        return Node("adimage", {"hash": h, "account": client, "title": doc.get("name") or h,
                                "created": datetime.fromisoformat(doc["created_time"]), "name": doc.get("name")})
    return None


def _client_of_node(ctx: Ctx, n: Node) -> Optional[Client]:
    w = ctx.world
    if n.created:
        return _account_of_id(w, n.obj.get("account_id"))
    if n.type in ("account", "page", "leadgen_form", "ig_user", "pixel"):
        return n.obj
    if n.type in ("campaign", "adset", "ad", "lead"):
        return w.clients[n.obj.client_index]
    if n.type in ("adcreative", "video"):
        return w.clients[n.ref.client_index]
    if n.type == "adimage":
        return n.obj["account"]
    return None


# ---------------------------------------------------------------------------
# Properties (scalar fields), per node type
# ---------------------------------------------------------------------------
def _creative_spec(ctx: Ctx, cr: Creative, client: Client) -> dict:
    cta = {"type": cr.call_to_action, "value": {"lead_gen_form_id": client.lead_form_id, "link": "http://fb.me/"}}
    if cr.kind == "video":
        return {"page_id": client.meta_page_id, "instagram_user_id": ig_id(client),
                "video_data": {"video_id": cr.video_id, "image_url": media.public_url(ctx.user_id, "thumb", cr.id),
                               "message": cr.body, "title": cr.title, "call_to_action": cta}}
    return {"page_id": client.meta_page_id, "instagram_user_id": ig_id(client),
            "link_data": {"image_hash": image_hash(ctx.user_id, cr.id), "link": "http://fb.me/", "message": cr.body,
                          "name": cr.title, "picture": media.public_url(ctx.user_id, "image", cr.id),
                          "call_to_action": cta}}


def _status_props(ctx: Ctx, n: Node, status: str) -> dict:
    return {"status": status, "configured_status": status, "effective_status": _effective(ctx, n)}


def _effective(ctx: Ctx, n: Node) -> str:
    """Meta's effective_status: the object's own unless a parent is not active."""
    if not n.created:
        if n.type in ("campaign", "adset", "ad"):
            return ctx.world.effective_status(n.obj)
        return "ACTIVE"
    own = n.obj.get("status") or ACTIVE
    if own != ACTIVE:
        return own
    if n.type == "ad":
        adset = lookup(ctx, n.obj.get("adset_id"))
        if adset is not None:
            eff = _effective(ctx, adset)
            if eff != ACTIVE:
                return eff if eff.endswith("_PAUSED") else "ADSET_PAUSED"
    if n.type == "adset":
        camp = lookup(ctx, n.obj.get("campaign_id"))
        if camp is not None and _effective(ctx, camp) != ACTIVE:
            return "CAMPAIGN_PAUSED"
    return own


def props(ctx: Ctx, n: Node) -> dict:
    """Every scalar field of the node (values may be callables, computed only when asked for)."""
    if n.created:
        return _created_props(ctx, n)
    w, o, t, uid = ctx.world, n.obj, n.type, ctx.user_id
    if t == "user":
        return {"id": o.meta_user_id, "name": o.meta_user_name, "first_name": "Sandbox", "last_name": "Tester",
                "short_name": "Sandbox"}
    if t == "business":
        return {"id": o.meta_business_id, "name": o.name, "verification_status": "verified",
                "created_time": _ts(_made(EPOCH - timedelta(days=400), o.meta_business_id))}
    if t == "account":
        offset = w.now.utcoffset().total_seconds() / 3600
        return {"id": o.meta_ad_account_id, "account_id": o.meta_account_number, "name": o.name,
                "currency": o.currency, "timezone_name": o.timezone, "timezone_offset_hours_utc": offset,
                "timezone_id": 274, "account_status": 1, "disable_reason": 0, "is_prepay_account": False,
                "funding_source": "8" + _digits(14, "funding", o.meta_ad_account_id),
                "funding_source_details": {"id": "8" + _digits(14, "funding", o.meta_ad_account_id),
                                           "display_string": "Visa *4242", "type": 1},
                "amount_spent": lambda: str(int(round(w.metrics(o, w.history_start, w.today).spend * 100))),
                "balance": "0", "spend_cap": "0", "min_daily_budget": 100,
                "created_time": _ts(_made(EPOCH - timedelta(days=30), o.meta_ad_account_id)),
                "owner": w.agency.meta_business_id, "business_name": w.agency.name,
                "business_city": o.city, "business_country_code": "GB",
                "age": float((w.today - (EPOCH - timedelta(days=30))).days), "capabilities": [],
                "user_tasks": ["DRAFT", "ANALYZE", "ADVERTISE", "MANAGE"], "is_personal": 0,
                "tos_accepted": {"web_custom_audience_tos": 1}}
    if t == "campaign":
        made = _made(o.created_on, o.id)
        return {"id": o.id, "name": o.name, **_status_props(ctx, n, o.status), "objective": o.objective,
                "account_id": o.account_id[4:], "created_time": _ts(made), "start_time": _ts(made),
                "updated_time": _ts(_updated(ctx, o.id, made)), "buying_type": "AUCTION",
                "special_ad_categories": [], "special_ad_category": "NONE",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "budget_rebalance_flag": False,
                "can_use_spend_cap": True, "smart_promotion_type": "GUIDED_CREATION",
                "source_campaign_id": "0", "pacing_type": ["standard"]}
    if t == "adset":
        c, made = w.clients[o.client_index], _made(o.created_on, o.id)
        return {"id": o.id, "name": o.name, **_status_props(ctx, n, o.status), "campaign_id": o.campaign_id,
                "account_id": o.account_id[4:], "daily_budget": str(o.daily_budget), "lifetime_budget": "0",
                "budget_remaining": str(o.daily_budget), "optimization_goal": o.optimization_goal,
                "billing_event": o.billing_event, "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "destination_type": "ON_AD", "promoted_object": {"page_id": c.meta_page_id},
                "targeting": {"geo_locations": {"countries": ["GB"], "location_types": ["home", "recent"]},
                              "age_min": 25, "age_max": 55, "targeting_automation": {"advantage_audience": 0}},
                "created_time": _ts(made), "start_time": _ts(made), "updated_time": _ts(_updated(ctx, o.id, made)),
                "is_dynamic_creative": False, "pacing_type": ["standard"], "attribution_spec": [
                    {"event_type": "CLICK_THROUGH", "window_days": 7}, {"event_type": "VIEW_THROUGH", "window_days": 1}]}
    if t == "ad":
        made = _made(o.created_on, o.id)
        return {"id": o.id, "name": o.name, **_status_props(ctx, n, o.status), "adset_id": o.adset_id,
                "campaign_id": o.campaign_id, "account_id": o.account_id[4:], "created_time": _ts(made),
                "updated_time": _ts(_updated(ctx, o.id, made)), "bid_type": "ABSOLUTE_OCPM",
                "preview_shareable_link": lambda: media.public_url(uid, "preview", o.id),
                "source_ad_id": "0", "demolink_hash": None}
    if t == "adcreative":
        ad, c = n.ref, w.clients[n.ref.client_index]
        spec = _creative_spec(ctx, o, c)
        out = {"id": o.id, "name": f"{o.title} {_made(ad.created_on, o.id).strftime('%Y-%m-%d-%H%M')}",
               "title": o.title, "body": o.body, "status": "ACTIVE", "account_id": ad.account_id[4:],
               "object_story_spec": spec, "call_to_action_type": o.call_to_action,
               "object_type": "VIDEO" if o.kind == "video" else "SHARE", "actor_id": c.meta_page_id,
               "effective_object_story_id": f"{c.meta_page_id}_{_digits(15, 'post', o.id)}",
               "thumbnail_url": media.public_url(uid, "thumb", o.id), "instagram_permalink_url": None}
        if o.kind == "video":
            out["video_id"] = o.video_id
        else:
            out["image_hash"] = image_hash(uid, o.id)
            out["image_url"] = media.public_url(uid, "image", o.id)
        return out
    if t == "lead":
        return _lead_props(ctx, o)
    if t == "page":
        return {"id": o.meta_page_id, "name": o.name, "category": CATEGORY.get(o.vertical, "Local business"),
                "access_token": make_credential(uid, f"meta_page:{o.meta_page_id}", nonce=""),
                "tasks": ["ADVERTISE", "ANALYZE", "CREATE_CONTENT", "MESSAGING", "MODERATE", "MANAGE"],
                "link": o.website, "website": o.website, "phone": o.phone,
                "about": f"{o.name}: {o.vertical} in {o.city}.", "fan_count": 800 + int(_digits(4, "fans", o.key)),
                "followers_count": 900 + int(_digits(4, "fans", o.key)), "is_published": True,
                "picture": {"data": {"url": media.public_url(uid, "image", o.meta_page_id), "is_silhouette": False,
                                     "width": 50, "height": 50}},
                "location": {"city": o.city, "country": "United Kingdom", "street": o.address, "zip": o.postcode}}
    if t == "leadgen_form":
        from sandbox.world import _QUESTIONS   # the form's own questions
        questions = [{"key": "full_name", "label": "Full name", "type": "FULL_NAME"},
                     {"key": "email", "label": "Email", "type": "EMAIL"},
                     {"key": "phone_number", "label": "Phone number", "type": "PHONE"}]
        questions += [{"key": q, "label": q.replace("_", " ").rstrip("?").capitalize() + "?", "type": "CUSTOM",
                       "options": [{"key": a.lower().replace(" ", "_"), "value": a} for a in opts]}
                      for q, opts in _QUESTIONS[o.vertical]]
        return {"id": o.lead_form_id, "name": f"{o.name} Enquiry Form", "status": "ACTIVE", "locale": "en_GB",
                "questions": questions, "created_time": _ts(_made(EPOCH, o.lead_form_id)),
                "leads_count": lambda: len(w.leads(o, w.history_start, w.today)),
                "privacy_policy_url": f"{o.website}/privacy", "follow_up_action_url": o.website,
                "is_optimized_for_quality": False, "block_display_for_non_targeted_viewer": False}
    if t == "video":
        ad = n.ref
        made = _made(ad.created_on, o.video_id)
        return {"id": o.video_id, "title": o.title, "description": o.body,
                "picture": media.public_url(uid, "video", o.id), "status": {"video_status": "ready",
                                                                           "processing_progress": 100},
                "length": 15.0 + int(_digits(2, "len", o.video_id)) % 31, "created_time": _ts(made),
                "updated_time": _ts(made), "published": True,
                "permalink_url": f"/{w.clients[ad.client_index].meta_page_id}/videos/{o.video_id}/",
                "embeddable": True, "format": [{"filter": "native", "width": 1080, "height": 1080,
                                                "picture": media.public_url(uid, "video", o.id)}]}
    if t == "adimage":
        c, h = o["account"], o["hash"]
        return {"id": f"{c.meta_account_number}:{h}", "hash": h, "account_id": c.meta_account_number,
                "name": o.get("name") or f"{h}.png", "url": media.cdn_url(uid, h),
                "url_128": media.public_url(uid, "image", h), "permalink_url": media.public_url(uid, "image", h),
                "width": 1080, "height": 1080, "original_width": 1080, "original_height": 1080,
                "status": "ACTIVE", "created_time": _ts(o["created"]), "updated_time": _ts(o["created"])}
    if t == "pixel":
        return {"id": pixel_id(o), "name": f"{o.name} Pixel", "is_unavailable": False,
                "last_fired_time": _ts(w.now.replace(microsecond=0) - timedelta(minutes=7)),
                "creation_time": _ts(_made(EPOCH, pixel_id(o)))}
    if t == "ig_user":
        return {"id": ig_id(o), "username": o.key.replace("-", "")[:30], "name": o.name,
                "followers_count": 500 + int(_digits(4, "igf", o.key)), "media_count": 120}
    return {"id": node_id(n)}


def _lead_props(ctx: Ctx, lead: Lead) -> dict:
    w = ctx.world
    ad, adset, camp = w.find(lead.ad_id), w.find(lead.adset_id), w.find(lead.campaign_id)
    field_data = [{"name": "full_name", "values": [lead.full_name]},
                  {"name": "email", "values": [lead.email]},
                  {"name": "phone_number", "values": [lead.phone]}]
    field_data += [{"name": q, "values": [a]} for q, a in lead.answers.items()]
    return {"id": lead.id, "created_time": _utc(lead.created_time), "ad_id": lead.ad_id,
            "ad_name": ad.name if ad else None, "adset_id": lead.adset_id, "adset_name": adset.name if adset else None,
            "campaign_id": lead.campaign_id, "campaign_name": camp.name if camp else None,
            "form_id": lead.form_id, "field_data": field_data, "is_organic": False, "platform": lead.platform,
            "custom_disclaimer_responses": [], "home_listing": None, "partner_name": None, "retailer_item_id": None}


def _created_props(ctx: Ctx, n: Node) -> dict:
    d, t, uid = n.obj, n.type, ctx.user_id
    made = d.get("created_time")
    made_ts = _ts(datetime.fromisoformat(made)) if made else None
    act = d.get("account_id") or ""
    base = {"id": d["id"], "name": d.get("name"), "account_id": act[4:] if act.startswith("act_") else act,
            "created_time": made_ts, "updated_time": _ts(datetime.fromisoformat(d.get("updated_time") or made))
            if made else None}
    if t in ("campaign", "adset", "ad"):
        base.update(_status_props(ctx, n, d.get("status") or ACTIVE))
    if t == "campaign":
        base.update({"objective": d.get("objective") or "OUTCOME_LEADS", "buying_type": "AUCTION",
                     "special_ad_categories": d.get("special_ad_categories") or [], "start_time": made_ts,
                     "bid_strategy": "LOWEST_COST_WITHOUT_CAP"})
    elif t == "adset":
        budget = d.get("daily_budget")
        base.update({"campaign_id": d.get("campaign_id"), "daily_budget": str(budget) if budget else "0",
                     "lifetime_budget": "0", "optimization_goal": d.get("optimization_goal") or "LEAD_GENERATION",
                     "billing_event": d.get("billing_event") or "IMPRESSIONS", "destination_type": "ON_AD",
                     "targeting": d.get("targeting") or {"geo_locations": {"countries": ["GB"]}},
                     "start_time": made_ts})
    elif t == "ad":
        base.update({"adset_id": d.get("adset_id"), "campaign_id": d.get("campaign_id"),
                     "preview_shareable_link": media.public_url(uid, "preview", d["id"])})
    elif t == "adcreative":
        spec = d.get("object_story_spec") or {}
        data = spec.get("link_data") or spec.get("video_data") or {}
        cta = data.get("call_to_action") or {}
        base.update({"object_story_spec": spec, "status": "ACTIVE", "url_tags": d.get("url_tags"),
                     "title": data.get("name") or data.get("title"), "body": data.get("message"),
                     "call_to_action_type": cta.get("type"),
                     "object_type": "VIDEO" if "video_data" in spec else "SHARE", "actor_id": spec.get("page_id"),
                     "thumbnail_url": media.public_url(uid, "thumb", d["id"])})
        if data.get("image_hash"):
            base.update({"image_hash": data["image_hash"], "image_url": media.public_url(uid, "image", d["id"])})
        if data.get("video_id"):
            base["video_id"] = data["video_id"]
    elif t == "advideo":
        base.update({"title": d.get("name"), "picture": media.public_url(uid, "video", d["id"]),
                     "status": {"video_status": "ready", "processing_progress": 100}, "length": 20.0,
                     "published": False})
    return base


# ---------------------------------------------------------------------------
# Object-valued fields (``creative{...}``, ``adset{id,name}``, ``campaign{effective_status}``)
# ---------------------------------------------------------------------------
def objects(ctx: Ctx, n: Node) -> dict:
    w = ctx.world
    if n.created:
        d = n.obj
        out = {}
        if d.get("creative_id"):
            out["creative"] = lambda: lookup(ctx, d["creative_id"])
        if d.get("adset_id"):
            out["adset"] = lambda: lookup(ctx, d["adset_id"])
        if d.get("campaign_id"):
            out["campaign"] = lambda: lookup(ctx, d["campaign_id"])
        out["account"] = lambda: lookup(ctx, d.get("account_id"))
        return out
    t, o = n.type, n.obj
    if t == "account":
        return {"business": lambda: Node("business", w.agency)}
    if t == "adset":
        return {"campaign": lambda: Node("campaign", w.campaign_of(o)),
                "account": lambda: Node("account", w.clients[o.client_index])}
    if t == "ad":
        return {"creative": lambda: Node("adcreative", o.creative, o),
                "adset": lambda: Node("adset", w.adset_of(o)),
                "campaign": lambda: Node("campaign", w.campaign_of(o)),
                "account": lambda: Node("account", w.clients[o.client_index])}
    if t == "campaign":
        return {"account": lambda: Node("account", w.clients[o.client_index])}
    if t == "page":
        return {"instagram_business_account": lambda: Node("ig_user", o),
                "business": lambda: Node("business", w.agency)}
    if t == "leadgen_form":
        return {"page": lambda: Node("page", o)}
    if t == "video":
        return {"from": lambda: Node("page", w.clients[n.ref.client_index])}
    return {}


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------
def _newest(nodes: list, ctx: Ctx) -> list:
    def made(n: Node):
        p = props(ctx, n)
        return p.get("created_time") or ""
    # created_time strings sort chronologically within one offset; parse to be safe across BST/GMT
    return sorted(nodes, key=lambda n: _sort_key(made(n)), reverse=True)


def _sort_key(ts: str):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _campaigns(ctx: Ctx, client: Client) -> list:
    nodes = [Node("campaign", c) for c in client.campaigns]
    nodes += [Node("campaign", d) for d in ctx.created_of("campaign", account_id=client.meta_ad_account_id)]
    return _newest(nodes, ctx)


def _adsets(ctx: Ctx, scope: Node) -> list:
    w = ctx.world
    if scope.type == "account":
        nodes = [Node("adset", a) for a in w.adsets(scope.obj)]
        nodes += [Node("adset", d) for d in ctx.created_of("adset", account_id=scope.obj.meta_ad_account_id)]
    else:
        nodes = [] if scope.created else [Node("adset", a) for a in w.adsets(scope.obj)]
        nodes += [Node("adset", d) for d in ctx.created_of("adset", campaign_id=node_id(scope))]
    return _newest(nodes, ctx)


def _ads(ctx: Ctx, scope: Node) -> list:
    w = ctx.world
    if scope.type == "account":
        nodes = [Node("ad", a) for a in w.ads(scope.obj)]
        nodes += [Node("ad", d) for d in ctx.created_of("ad", account_id=scope.obj.meta_ad_account_id)]
    elif scope.type == "campaign":
        nodes = [] if scope.created else [Node("ad", a) for a in w.ads(scope.obj)]
        nodes += [Node("ad", d) for d in ctx.created_of("ad", campaign_id=node_id(scope))]
    else:
        nodes = [] if scope.created else [Node("ad", a) for a in w.ads(scope.obj)]
        nodes += [Node("ad", d) for d in ctx.created_of("ad", adset_id=node_id(scope))]
    return _newest(nodes, ctx)


def _creatives(ctx: Ctx, client: Client) -> list:
    nodes = [Node("adcreative", ad.creative, ad) for ad in ctx.world.ads(client)]
    nodes += [Node("adcreative", d) for d in ctx.created_of("adcreative", account_id=client.meta_ad_account_id)]
    return _newest(nodes, ctx)


def _images(ctx: Ctx, client: Client, params: dict) -> list:
    wanted = _json_param(params.get("hashes"), "hashes") if params.get("hashes") else None
    hashes = []
    if wanted is None:
        hashes = [image_hash(ctx.user_id, ad.creative.id) for ad in ctx.world.ads(client) if not ad.creative.video_id]
        hashes += [d["hash"] for d in ctx.created_of("adimage", account_id=client.meta_ad_account_id)]
    else:
        hashes = [str(h) for h in (wanted if isinstance(wanted, list) else [wanted])]
    out = [_image(ctx, client, h) for h in dict.fromkeys(hashes)]
    return [n for n in out if n is not None]


def _videos(ctx: Ctx, client: Client) -> list:
    nodes = [Node("video", ad.creative, ad) for ad in ctx.world.ads(client) if ad.creative.video_id]
    nodes += [Node("advideo", d) for d in ctx.created_of("advideo", account_id=client.meta_ad_account_id)]
    return nodes


def _ad_leads(ctx: Ctx, ad: Ad) -> list:
    w, out = ctx.world, []
    for day in w.days(ad.created_on, w.today):
        out.extend(w.ad_leads(ad, day))
    return out


def _leads(ctx: Ctx, n: Node, params: dict) -> list:
    w = ctx.world
    if n.created:
        return []
    if n.type == "ad":
        leads = _ad_leads(ctx, n.obj)
    elif n.type in ("adset", "campaign"):
        leads = [p for ad in w.ads(n.obj) for p in _ad_leads(ctx, ad)]
    else:   # a lead form: the client's leads
        leads = w.leads(n.obj, w.history_start, w.today)
    if params.get("date_preset") or params.get("time_range"):
        since, until = date_window(params, w)
        leads = [p for p in leads if since <= p.created_time.date() <= until]
    for f in _filters(params):
        if f["field"] in ("time_created", "created_time"):
            leads = [p for p in leads if _compare(p.created_time.timestamp(), f["operator"], _when(f["value"]))]
        else:
            logger.warning("sandbox_unhandled provider=meta leads filtering field=%s", f["field"])
    leads.sort(key=lambda p: (p.created_time, p.id), reverse=True)    # Graph lists leads newest first
    return [Node("lead", p) for p in leads]


def _when(value) -> Any:
    """A time_created filter value (unix seconds, or a date / datetime in the account's zone) as unix seconds."""
    if isinstance(value, list):
        return [_when(v) for v in value]
    s = str(value).strip()
    if re.fullmatch(r"\d{9,11}", s):
        return float(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise bad_param(f"Invalid time_created value {s!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.timestamp()


def _previews(ctx: Ctx, n: Node, params: dict) -> list:
    target = node_id(n) if n.type == "ad" else (node_id(n.ref) if n.ref is not None else node_id(n))
    url = media.public_url(ctx.user_id, "preview", target).replace("&", "&amp;")
    return [{"body": f'<iframe src="{url}" width="540" height="690" scrolling="yes" style="border: none;">'
                     "</iframe>"}]


def _thumbnails(ctx: Ctx, n: Node, params: dict) -> list:
    vid = node_id(n)
    key = n.obj.id if not n.created and n.type == "video" else vid
    return [{"id": f"{vid}{k}", "uri": media.public_url(ctx.user_id, "video", key), "is_preferred": k == 0,
             "width": 1080, "height": 1080, "scale": 1, "name": f"thumbnail {k}"} for k in range(3)]


def _permissions(ctx: Ctx, n: Node, params: dict) -> list:
    from sandbox.meta_app import SCOPES

    return [{"permission": p, "status": "granted"} for p in SCOPES]


def edges(ctx: Ctx, n: Node) -> dict:
    """name -> fn(params) -> [Node | row]."""
    w, t, o = ctx.world, n.type, n.obj
    ins = {"insights": None}   # special: rows built from the requested fields
    if t == "user":
        return {"adaccounts": lambda p: [Node("account", c) for c in w.clients],
                "accounts": lambda p: [Node("page", c) for c in w.clients],
                "businesses": lambda p: [Node("business", w.agency)],
                "permissions": lambda p: _permissions(ctx, n, p)}
    if t == "business":
        return {"owned_ad_accounts": lambda p: [Node("account", c) for c in w.clients],
                "client_ad_accounts": lambda p: [], "owned_pages": lambda p: [Node("page", c) for c in w.clients],
                "client_pages": lambda p: [], "adspixels": lambda p: [Node("pixel", c) for c in w.clients],
                "owned_instagram_accounts": lambda p: [Node("ig_user", c) for c in w.clients]}
    if n.created:
        if t == "campaign":
            return {**ins, "adsets": lambda p: _adsets(ctx, n), "ads": lambda p: _ads(ctx, n)}
        if t == "adset":
            return {**ins, "ads": lambda p: _ads(ctx, n)}
        if t == "ad":
            return {**ins, "leads": lambda p: [], "previews": lambda p: _previews(ctx, n, p)}
        if t == "adcreative":
            return {"previews": lambda p: _previews(ctx, n, p)}
        if t == "advideo":
            return {"thumbnails": lambda p: _thumbnails(ctx, n, p)}
        return {}
    if t == "account":
        return {**ins, "campaigns": lambda p: _campaigns(ctx, o), "adsets": lambda p: _adsets(ctx, n),
                "ads": lambda p: _ads(ctx, n), "adcreatives": lambda p: _creatives(ctx, o),
                "adimages": lambda p: _images(ctx, o, p), "advideos": lambda p: _videos(ctx, o),
                "promote_pages": lambda p: [Node("page", o)], "adspixels": lambda p: [Node("pixel", o)],
                "customaudiences": lambda p: [], "saved_audiences": lambda p: [],
                "instagram_accounts": lambda p: [Node("ig_user", o)],
                "users": lambda p: [{"id": w.agency.meta_user_id, "name": w.agency.meta_user_name,
                                     "tasks": ["DRAFT", "ANALYZE", "ADVERTISE", "MANAGE"]}]}
    if t == "campaign":
        return {**ins, "adsets": lambda p: _adsets(ctx, n), "ads": lambda p: _ads(ctx, n),
                "leads": lambda p: _leads(ctx, n, p)}
    if t == "adset":
        return {**ins, "ads": lambda p: _ads(ctx, n), "leads": lambda p: _leads(ctx, n, p)}
    if t == "ad":
        return {**ins, "leads": lambda p: _leads(ctx, n, p), "previews": lambda p: _previews(ctx, n, p),
                "adcreatives": lambda p: [Node("adcreative", o.creative, o)]}
    if t == "adcreative":
        return {"previews": lambda p: _previews(ctx, n, p)}
    if t == "page":
        return {"leadgen_forms": lambda p: [Node("leadgen_form", o)],
                "instagram_accounts": lambda p: [Node("ig_user", o)],
                "promotable_posts": lambda p: [], "videos": lambda p: _videos(ctx, o)}
    if t == "leadgen_form":
        return {"leads": lambda p: _leads(ctx, n, p)}
    if t == "video":
        return {"thumbnails": lambda p: _thumbnails(ctx, n, p)}
    return {}


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def _filters(params: dict) -> list:
    raw = params.get("filtering")
    if not raw:
        return []
    parsed = _json_param(raw, "filtering")
    if not isinstance(parsed, list):
        raise bad_param("filtering must be a JSON array of {field, operator, value}")
    out = []
    for f in parsed:
        if not isinstance(f, dict) or "field" not in f or "operator" not in f:
            raise bad_param("Each filtering item needs a field and an operator")
        out.append({"field": str(f["field"]), "operator": str(f["operator"]).upper(), "value": f.get("value")})
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _compare(actual, op: str, target) -> bool:
    if op in ("IN", "NOT_IN"):
        vals = target if isinstance(target, list) else [target]
        hit = any(str(actual) == str(v) or _num(actual) == _num(v) for v in vals)
        return hit if op == "IN" else not hit
    if op in ("CONTAIN", "NOT_CONTAIN"):
        hit = str(target).lower() in str(actual or "").lower()
        return hit if op == "CONTAIN" else not hit
    if op in ("IN_RANGE", "NOT_IN_RANGE"):
        lo, hi = (target + [None, None])[:2] if isinstance(target, list) else (None, None)
        hit = lo is not None and hi is not None and _num(lo) <= _num(actual) <= _num(hi)
        return hit if op == "IN_RANGE" else not hit
    a, b = _num(actual), _num(target)
    if op == "EQUAL":
        return str(actual) == str(target) or a == b
    if op == "NOT_EQUAL":
        return not (str(actual) == str(target) or a == b)
    try:
        if op == "GREATER_THAN":
            return a > b
        if op == "GREATER_THAN_OR_EQUAL":
            return a >= b
        if op == "LESS_THAN":
            return a < b
        if op == "LESS_THAN_OR_EQUAL":
            return a <= b
    except TypeError:
        return False
    raise bad_param(f"Unsupported filtering operator {op}")


_ENTITY_FIELDS = {"id", "name", "effective_status", "status", "configured_status", "objective"}


def _entity_value(ctx: Ctx, n: Node, fld: str):
    """The value of a filtering field (``campaign.id``, ``ad.effective_status``, ``name``) on a node."""
    prefix, _, attr = fld.rpartition(".")
    p = props(ctx, n)
    if not prefix or prefix == n.type:
        v = p.get(attr)
    elif attr == "id" and f"{prefix}_id" in p:
        v = p.get(f"{prefix}_id")
    else:
        obj = objects(ctx, n).get(prefix)
        sub = obj() if obj else None
        v = props(ctx, sub).get(attr) if sub is not None else None
    return v() if callable(v) else v


def _filter_nodes(ctx: Ctx, nodes: list, params: dict) -> list:
    statuses = params.get("effective_status")
    if statuses:
        wanted = _json_param(statuses, "effective_status")
        wanted = set(wanted if isinstance(wanted, list) else [wanted])
        nodes = [n for n in nodes if props(ctx, n).get("effective_status") in wanted]
    elif nodes and nodes[0].type in ("campaign", "adset", "ad"):
        nodes = [n for n in nodes if props(ctx, n).get("effective_status") not in HIDDEN_BY_DEFAULT]
    for f in _filters(params):
        if f["field"].rpartition(".")[2] not in _ENTITY_FIELDS and f["field"] not in ("created_time", "updated_time"):
            logger.warning("sandbox_unhandled provider=meta filtering field=%s", f["field"])
            continue
        if f["field"] in ("created_time", "updated_time"):
            nodes = [n for n in nodes if _compare(_sort_key(props(ctx, n).get(f["field"])).timestamp(),
                                                  f["operator"], _when(f["value"]))]
        else:
            nodes = [n for n in nodes if _compare(_entity_value(ctx, n, f["field"]), f["operator"], f["value"])]
    return nodes


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------
def _total(w: World, ads: list, days: list) -> DayMetrics:
    sp = im = re_ = cl = li = le = 0
    for ad in ads:
        for d in days:
            m = w.ad_day(ad, d)
            if m is None or not (m.spend or m.impressions or m.leads):
                continue
            sp += m.spend; im += m.impressions; re_ += m.reach; cl += m.clicks; li += m.link_clicks; le += m.leads
    return DayMetrics(days[0] if days else date.min, round(sp, 2), im, re_, cl, li, le, len(days))


def _f(x: float, nd: int = 6) -> str:
    return f"{x:.{nd}f}".rstrip("0").rstrip(".") if nd else str(int(x))


def _ranking(quality: float) -> str:
    if quality >= 1.12:
        return "ABOVE_AVERAGE"
    if quality < 0.85:
        return "BELOW_AVERAGE_35"
    return "AVERAGE"


def _row(ctx: Ctx, level: str, ad: Ad, m: DayMetrics, wanted: list, since: date, until: date) -> dict:
    w = ctx.world
    c, camp, adset = w.clients[ad.client_index], w.campaign_of(ad), w.adset_of(ad)
    leads = m.leads
    lead_indicator = "actions:onsite_conversion.lead_grouped"
    vals: dict = {
        "account_id": c.meta_account_number, "account_name": c.name, "account_currency": c.currency,
        "spend": f"{m.spend:.2f}", "impressions": str(m.impressions), "reach": str(m.reach),
        "clicks": str(m.clicks), "inline_link_clicks": str(m.link_clicks),
        "unique_clicks": str(int(m.clicks * 0.93)), "unique_inline_link_clicks": str(int(m.link_clicks * 0.95)),
        "frequency": _f(m.frequency), "cpm": _f(m.cpm), "ctr": _f(m.ctr),
        "social_spend": "0", "objective": camp.objective, "buying_type": "AUCTION",
        "attribution_setting": ATTRIBUTION,
        "inline_link_click_ctr": _f(100.0 * m.link_clicks / m.impressions) if m.impressions else None,
        "cpc": _f(m.cpc) if m.clicks else None,
        "cpp": _f(1000.0 * m.spend / m.reach) if m.reach else None,
        "cost_per_inline_link_click": _f(m.spend / m.link_clicks) if m.link_clicks else None,
        "cost_per_unique_click": _f(m.spend / int(m.clicks * 0.93)) if int(m.clicks * 0.93) else None,
        "actions": m.meta_actions() or None,
        "unique_actions": m.meta_actions() or None,
        "cost_per_action_type": [{"action_type": a["action_type"], "value": _f(m.spend / int(a["value"]))}
                                 for a in m.meta_actions()] or None,
        "results": [{"indicator": lead_indicator, "values": [{"value": str(leads)}]}] if leads else None,
        "cost_per_result": ([{"indicator": lead_indicator, "values": [{"value": _f(m.spend / leads)}]}]
                            if leads else None),
        "date_start": since.isoformat(), "date_stop": until.isoformat(),
    }
    if level in ("campaign", "adset", "ad"):
        vals.update(campaign_id=camp.id, campaign_name=camp.name)
    if level in ("adset", "ad"):
        vals.update(adset_id=adset.id, adset_name=adset.name, optimization_goal=adset.optimization_goal)
    if level == "ad":
        vals.update(ad_id=ad.id, ad_name=ad.name, quality_ranking=_ranking(ad.quality),
                    engagement_rate_ranking=_ranking(ad.quality * 1.04),
                    conversion_rate_ranking=_ranking(ad.quality * 0.97))
    out = {}
    for name in wanted:
        if name in ("date_start", "date_stop"):
            continue
        v = vals.get(name)
        if v is not None:
            out[name] = v
        elif name not in vals:
            logger.debug("sandbox_unhandled_field provider=meta node=insights field=%s", name)
    out["date_start"], out["date_stop"] = vals["date_start"], vals["date_stop"]
    return out


_METRIC_FILTERS = {"spend": "spend", "impressions": "impressions", "clicks": "clicks", "reach": "reach",
                   "inline_link_clicks": "link_clicks", "ctr": "ctr", "cpm": "cpm", "cpc": "cpc",
                   "frequency": "frequency", "actions:lead": "leads", "leads": "leads"}


def insights_rows(ctx: Ctx, n: Node, params: dict, specs: list) -> list:
    """Graph's insights rows for a node over the asked window, at the asked level."""
    w = ctx.world
    if n.created or n.type not in LEVELS:
        return []
    scope_level = n.type
    level = (params.get("level") or scope_level).strip().lower()
    if level not in LEVELS:
        raise bad_param(f"level must be one of {', '.join(LEVELS)}")
    if LEVELS.index(level) < LEVELS.index(scope_level):
        level = scope_level
    if params.get("breakdowns"):
        logger.warning("sandbox_unhandled provider=meta insights breakdowns=%s (rows are not broken down)",
                       params.get("breakdowns"))
    ads = w.ads(n.obj)
    metric_filters = []
    for f in _filters(params):
        prefix, _, attr = f["field"].rpartition(".")
        if prefix in ("campaign", "adset", "ad") and attr in ("id", "name", "effective_status", "objective"):
            ads = [a for a in ads if _compare(_entity_value(ctx, Node("ad", a), f["field"]), f["operator"], f["value"])]
        elif f["field"] in _METRIC_FILTERS:
            metric_filters.append(f)
        else:
            logger.warning("sandbox_unhandled provider=meta insights filtering field=%s", f["field"])
    groups: dict = {}
    for ad in ads:
        key = {"account": "", "campaign": ad.campaign_id, "adset": ad.adset_id, "ad": ad.id}[level]
        groups.setdefault(key, []).append(ad)
    wanted = [s.name for s in specs] if specs else ["spend", "impressions"]
    rows = []
    for since, until in buckets(params, w):
        days = w.days(since, until)
        if not days:
            continue
        for key, group in groups.items():
            m = _total(w, group, days)
            if not (m.spend or m.impressions):
                continue    # Graph has no row for an entity / period without delivery
            if any(not _compare(getattr(m, _METRIC_FILTERS[f["field"]]), f["operator"], f["value"])
                   for f in metric_filters):
                continue
            rows.append(_row(ctx, level, group[0], m, wanted, since, until))
    return rows


# ---------------------------------------------------------------------------
# Serialising
# ---------------------------------------------------------------------------
_DEFAULT_NODE_FIELDS = {"lead": "created_time,id,field_data", "adimage": "hash,url",
                        "user": "id,name", "business": "id,name", "page": "id,name", "account": "id,account_id",
                        "leadgen_form": "id,name,status"}


def default_specs(n: Node, top: bool) -> list:
    if n.type in _DEFAULT_NODE_FIELDS:
        return parse_fields(_DEFAULT_NODE_FIELDS[n.type])
    return parse_fields("id,name" if top and n.type in ("campaign", "adset", "ad", "adcreative") else "id")


def serialize(ctx: Ctx, n: Node, specs: Optional[list], params: Optional[dict] = None, top: bool = False) -> dict:
    specs = specs or default_specs(n, top)
    p = props(ctx, n)
    obj_fields = objects(ctx, n)
    edge_fns = edges(ctx, n)
    out: dict = {}
    for s in specs:
        name = s.name
        if name in edge_fns:
            value = nested_edge(ctx, n, s, edge_fns, params or {})
            if value is not None:
                out[name] = value
        elif name in obj_fields:
            sub = obj_fields[name]()
            if sub is not None:
                out[name] = serialize(ctx, sub, s.children)
        elif name in p:
            v = p[name]
            v = v() if callable(v) else v
            if v is not None:
                out[name] = v
        else:
            logger.debug("sandbox_unhandled_field provider=meta node=%s field=%s", n.type, name)
    out.setdefault("id", node_id(n))
    return out


def _mod_params(spec: FieldSpec) -> dict:
    out = {}
    for k, v in spec.mods.items():
        out[k] = v.strip()
    if spec.children is not None:
        out["fields"] = spec.raw_children
    return out


def nested_edge(ctx: Ctx, n: Node, spec: FieldSpec, edge_fns: dict, parent: dict) -> Optional[dict]:
    params = _mod_params(spec)
    if spec.name == "insights":
        for k in ("date_preset", "time_range", "time_increment"):
            if k not in params and parent.get(k):
                params[k] = parent[k]
    body = edge_page(ctx, n, spec.name, edge_fns, params, spec.children, nested=True)
    return body if body["data"] else None    # Graph leaves an empty nested edge out


def _limit(params: dict) -> int:
    try:
        v = int(params.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise bad_param("limit must be a number")
    return max(1, min(MAX_LIMIT, v))


def _cursor(pos: int) -> str:
    return base64.urlsafe_b64encode(f"sbx:{pos}".encode()).decode().rstrip("=")


def _uncursor(c) -> int:
    if not c:
        return 0
    try:
        raw = base64.urlsafe_b64decode(str(c) + "=" * (-len(str(c)) % 4)).decode()
        if raw.startswith("sbx:"):
            return max(0, int(raw[4:]))
    except (ValueError, UnicodeDecodeError):
        pass
    raise bad_param("Invalid cursor")


def _page_url(ctx: Ctx, path: str, params: dict, pos: int, limit: int) -> str:
    q = {k: v for k, v in params.items() if k not in ("after", "before", "offset", "access_token", "limit")}
    q.update(limit=limit, access_token=ctx.token, after=_cursor(pos))
    return f"{GRAPH_HOST}/{ctx.version}/{path.strip('/')}?{urlencode(q)}"


def edge_page(ctx: Ctx, n: Node, edge: str, edge_fns: dict, params: dict, specs: Optional[list],
              nested: bool = False) -> dict:
    """One page of an edge, in Graph's envelope."""
    if edge == "insights":
        items = insights_rows(ctx, n, params, specs or [])
    else:
        items = edge_fns[edge](params)
        if items and isinstance(items[0], Node):
            items = _filter_nodes(ctx, items, params)
    limit, start = _limit(params), _uncursor(params.get("after"))
    if not params.get("after") and params.get("offset"):
        start = max(0, int(params["offset"]))
    chunk = items[start:start + limit]
    data = [serialize(ctx, x, specs, params) if isinstance(x, Node) else x for x in chunk]
    body: dict = {"data": data}
    path = f"{node_id(n)}/{edge}"
    paging: dict = {}
    if chunk:
        paging["cursors"] = {"before": _cursor(start), "after": _cursor(start + len(chunk))}
    if start + len(chunk) < len(items):
        paging["next"] = _page_url(ctx, path, params, start + len(chunk), limit)
    if start > 0:
        paging["previous"] = _page_url(ctx, path, params, max(0, start - limit), limit).replace("after=", "before=")
    if paging:
        body["paging"] = paging
    if str(params.get("summary") or "").lower() in ("true", "1", "total_count"):
        body["summary"] = {"total_count": len(items)}
    return body


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
_PATH_VERSION = re.compile(r"^/?v\d+\.\d+(?=/|$)")


def split_path(path: str) -> list:
    path = _PATH_VERSION.sub("", path or "")
    return [p for p in path.strip("/").split("/") if p]


def _specs(params: dict) -> Optional[list]:
    try:
        return parse_fields(params.get("fields")) or None
    except FieldsError as e:
        raise ProviderError(400, f"Syntax error \"{e}\"", code=2500, type="OAuthException")


async def handle(ctx: Ctx, method: str, path: str, params: dict) -> Any:
    """Answer one Graph request (also used for each item of a batch). Raises ProviderError."""
    method = method.upper()
    parts = split_path(path)
    if not parts or len(parts) > 2:
        raise ProviderError(400, f"(#2500) Unknown path components: /{'/'.join(parts)}", code=2500,
                            type="OAuthException")
    node = lookup(ctx, parts[0])
    if node is None:
        raise not_found(parts[0], "post" if method == "POST" else "delete" if method == "DELETE" else "get")
    if len(parts) == 1:
        if method == "GET":
            return serialize(ctx, node, _specs(params), params, top=True)
        if method == "POST":
            return await update(ctx, node, params)
        if method == "DELETE":
            return await update(ctx, node, {"status": "DELETED"})
        raise unhandled(f"{method} /{parts[0]}")
    edge = parts[1]
    if method == "POST":
        return await create(ctx, node, edge, params)
    if method == "DELETE":
        raise unhandled(f"DELETE /{node.type}/{edge}")
    edge_fns = edges(ctx, node)
    if edge not in edge_fns:
        logger.warning("sandbox_unhandled provider=meta method=GET node=%s edge=%s", node.type, edge)
        raise ProviderError(400, f"(#100) Tried accessing nonexisting field ({edge}) on node type "
                                 f"({_TYPE_NAMES.get(node.type, node.type)})", code=100, type="OAuthException",
                            sandbox_unhandled=True)
    return edge_page(ctx, node, edge, edge_fns, params, _specs(params))


_TYPE_NAMES = {"user": "User", "business": "Business", "account": "AdAccount", "campaign": "AdCampaignGroup",
               "adset": "AdCampaign", "ad": "AdGroup", "adcreative": "AdCreative", "lead": "LeadgenFormData",
               "page": "Page", "leadgen_form": "LeadgenForm", "video": "AdVideo", "advideo": "AdVideo",
               "adimage": "AdImage", "pixel": "AdsPixel", "ig_user": "IGUser"}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _validate_only(params: dict) -> bool:
    return "validate_only" in str(params.get("execution_options") or "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _new_id() -> str:
    return "1202" + str(secrets.randbelow(10 ** 14)).rjust(14, "0")


async def update(ctx: Ctx, node: Node, params: dict) -> dict:
    """POST /{id}: status, name, daily_budget. Remembered in sandbox_state; later reads reflect them."""
    if node.type not in ("campaign", "adset", "ad", "adcreative", "advideo"):
        raise unhandled(f"POST /{node.type}")
    known = {"status", "name", "daily_budget", "access_token", "execution_options", "fields", "lifetime_budget"}
    for k in params:
        if k not in known:
            logger.warning("sandbox_unhandled provider=meta update node=%s param=%s (ignored)", node.type, k)
    status = params.get("status")
    if status is not None:
        status = str(status).upper()
        if status not in STATUSES or node.type not in ("campaign", "adset", "ad"):
            raise bad_param(f"Param status must be one of {{{', '.join(STATUSES)}}}")
    budget = params.get("daily_budget")
    if budget is not None:
        if node.type != "adset":
            raise ProviderError(400, "Invalid parameter", code=100, error_subcode=1885621, type="OAuthException",
                                error_user_msg="This campaign doesn't use a campaign budget: change the budget on "
                                               "its ad sets.")
        try:
            budget = int(float(budget))
        except (TypeError, ValueError):
            raise bad_param("Param daily_budget must be an integer (minor units)")
        if budget < 100:
            raise ProviderError(400, "Invalid parameter", code=100, error_subcode=1885272, type="OAuthException",
                                error_user_msg="Your budget is too low. The minimum budget for this ad set is £1.00.")
    if params.get("lifetime_budget") not in (None, "", "0", 0):
        raise bad_param("This ad set uses a daily budget; lifetime_budget can't be set on it")
    name = params.get("name")
    if _validate_only(params):
        return {"success": True}
    oid = node_id(node)
    if node.created:
        doc = dict(node.obj)
        if status is not None:
            doc["status"] = status
        if name is not None:
            doc["name"] = str(name)
        if budget is not None:
            doc["daily_budget"] = budget
        doc["updated_time"] = _now_iso()
        await state.set(ctx.user_id, OBJECT_KIND, oid, doc)
        ctx.created[oid] = doc
        return {"success": True}
    if status is not None:
        await state.set(ctx.user_id, state.STATUS, oid, status)
    if name is not None:
        await state.set(ctx.user_id, state.NAME, oid, str(name))
    if budget is not None:
        await state.set(ctx.user_id, state.BUDGET, oid, budget)
    return {"success": True}


async def _remember(ctx: Ctx, key: str, doc: dict) -> None:
    await state.set(ctx.user_id, OBJECT_KIND, key, doc)
    ctx.created[key] = doc


async def create(ctx: Ctx, node: Node, edge: str, params: dict) -> dict:
    """POST /{act}/{edge}: campaigns, adsets, ads, adcreatives, adimages, advideos."""
    if node.type != "account":
        raise unhandled(f"POST /{node.type}/{edge}")
    act = node.obj.meta_ad_account_id
    now = _now_iso()
    if edge == "adimages":
        raw = params.get("bytes")
        if raw:
            try:
                content = base64.b64decode(raw, validate=False)
            except (ValueError, TypeError):
                raise bad_param("Param bytes must be base64")
        else:
            content = (params.get("filename") or params.get("url") or secrets.token_hex(8)).encode()
        h = hashlib.md5(content).hexdigest()
        name = params.get("name") or ("bytes" if raw else str(params.get("filename") or h))
        if not _validate_only(params):
            await _remember(ctx, f"adimage:{h}", {"type": "adimage", "id": f"{node.obj.meta_account_number}:{h}",
                                                  "hash": h, "account_id": act, "name": name, "created_time": now,
                                                  "size": len(content)})
        return {"images": {name: {"hash": h, "url": media.cdn_url(ctx.user_id, h)}}}
    oid = _new_id()
    if edge == "advideos":
        if not (params.get("file_url") or params.get("source") or params.get("upload_phase")):
            raise bad_param("Param file_url or source is required")
        await _remember(ctx, oid, {"type": "advideo", "id": oid, "account_id": act, "created_time": now,
                                   "name": params.get("name") or params.get("title") or "Sandbox video"})
        return {"id": oid}
    if edge == "adcreatives":
        spec = _json_param(params.get("object_story_spec"), "object_story_spec")
        if not isinstance(spec, dict) or not spec.get("page_id"):
            raise bad_param("Param object_story_spec must include page_id")
        if spec["page_id"] not in ctx.index.page:
            raise bad_param(f"Invalid page_id {spec['page_id']}: the page is not available to this ad account")
        await _remember(ctx, oid, {"type": "adcreative", "id": oid, "account_id": act, "created_time": now,
                                   "name": params.get("name") or f"Creative {now}", "object_story_spec": spec,
                                   "url_tags": params.get("url_tags")})
        return {"id": oid}
    if edge == "ads":
        adset = lookup(ctx, params.get("adset_id"))
        if adset is None or adset.type != "adset" or _client_of_node(ctx, adset) is not node.obj:
            raise bad_param(f"Param adset_id {params.get('adset_id')!r} is not an ad set of this account")
        creative = _json_param(params.get("creative"), "creative") or {}
        cid = str(creative.get("creative_id") or "")
        cnode = lookup(ctx, cid) if cid else None
        if cnode is None or cnode.type != "adcreative":
            raise bad_param(f"Param creative: creative {cid!r} does not exist in this account")
        status = str(params.get("status") or ACTIVE).upper()
        if status not in ("ACTIVE", "PAUSED"):
            raise bad_param("Param status must be one of {ACTIVE, PAUSED}")
        await _remember(ctx, oid, {"type": "ad", "id": oid, "account_id": act, "created_time": now,
                                   "name": params.get("name") or f"Ad {now}", "status": status,
                                   "adset_id": node_id(adset), "campaign_id": props(ctx, adset)["campaign_id"],
                                   "creative_id": cid})
        return {"id": oid}
    if edge == "campaigns":
        status = str(params.get("status") or PAUSED).upper()
        await _remember(ctx, oid, {"type": "campaign", "id": oid, "account_id": act, "created_time": now,
                                   "name": params.get("name") or "New campaign", "status": status,
                                   "objective": params.get("objective") or "OUTCOME_LEADS",
                                   "special_ad_categories": _json_param(params.get("special_ad_categories"),
                                                                        "special_ad_categories") or []})
        return {"id": oid}
    if edge == "adsets":
        camp = lookup(ctx, params.get("campaign_id"))
        if camp is None or camp.type != "campaign" or _client_of_node(ctx, camp) is not node.obj:
            raise bad_param(f"Param campaign_id {params.get('campaign_id')!r} is not a campaign of this account")
        budget = params.get("daily_budget")
        await _remember(ctx, oid, {"type": "adset", "id": oid, "account_id": act, "created_time": now,
                                   "name": params.get("name") or "New ad set",
                                   "status": str(params.get("status") or PAUSED).upper(),
                                   "campaign_id": node_id(camp), "daily_budget": int(float(budget)) if budget else None,
                                   "optimization_goal": params.get("optimization_goal"),
                                   "billing_event": params.get("billing_event"),
                                   "targeting": _json_param(params.get("targeting"), "targeting")})
        return {"id": oid}
    raise unhandled(f"POST /act/{edge}")


# ---------------------------------------------------------------------------
# Media lookups (routers/sandbox.py, the fbcdn route)
# ---------------------------------------------------------------------------
async def describe_media(user_id: str, what: str, object_id: str) -> Optional[tuple]:
    """(title, client name, what) for the object behind a media token, or None."""
    from sandbox.credentials import Credential

    if what not in media.WHATS:
        return None
    cred = Credential(user_id=user_id, kind="meta", raw="")
    ctx = await load_ctx(cred)
    n = lookup(ctx, object_id)
    if n is None:
        idx = ctx.index
        if object_id in idx.hash_ad:
            n = Node("ad", ctx.world.find(idx.hash_ad[object_id]))
        elif f"adimage:{object_id}" in ctx.created:
            d = ctx.created[f"adimage:{object_id}"]
            c = _account_of_id(ctx.world, d.get("account_id"))
            return (d.get("name") or "Uploaded image", c.name if c else "", what)
    if n is None:
        return None
    client = _client_of_node(ctx, n)
    p = props(ctx, n)
    if n.type == "ad" and not n.created:
        title = n.obj.creative.title if what != "preview" else n.obj.name
    elif n.type in ("adcreative", "video") and not n.created:
        title = n.obj.title
    else:
        title = p.get("title") or p.get("name") or "Sandbox"
    return (str(title), client.name if client else "", what)
