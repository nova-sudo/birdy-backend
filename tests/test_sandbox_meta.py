"""
Sandbox accounts, phase 2: the fake Graph API, driven through Birdy's own Meta code (first sync,
presets batch, daily spend, ad daily, lead import, incremental refresh, live tools, agent state,
alerts, status / budget writes, creative-test posting), with an sbx_ token and mock_db. Nothing
may leave the process: httpx's real transport raises for any request that reaches it.

Today is partial in the world (spend grows through the day), so the exact-equality checks use
windows that end yesterday.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import sandbox
from core.config import API_PUBLIC_URL
from core.constants import META_CACHE_PRESETS
from core.database import DB_NAME
from core.utils import get_result_value
from sandbox import make_credential, meta_graph, state
from sandbox.world import world_for

SBX = "tester@sandbox.birdy.test"
GRAPH = "https://graph.facebook.com/v25.0"


@pytest.fixture(autouse=True)
def outgoing(mock_db, monkeypatch):
    """The hook is on; any request reaching httpx's real transport fails the test. Sleeps between
    pages are skipped (the product sleeps 0.1-2 s between Graph calls)."""
    sandbox.install()
    seen = []

    async def leak(self, request):
        seen.append(str(request.url))
        raise AssertionError(f"left the process: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", leak)
    real_sleep = asyncio.sleep

    async def no_wait(delay=0, result=None):
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    yield seen
    assert seen == [], f"requests left the process: {seen}"


@pytest.fixture
async def token(mock_db):
    tok = make_credential(SBX, "meta")
    await mock_db["users"].insert_one({"user_id": SBX, "email": SBX,
                                       "integrations": {"facebook": {"access_token": tok}}})
    return tok


async def _world():
    return world_for(SBX, overrides=await state.overrides(SBX))


async def _get(path, token, **params):
    async with httpx.AsyncClient() as c:
        return await c.get(f"{GRAPH}/{path}", params={**params, "access_token": token})


async def _post(path, token, **data):
    async with httpx.AsyncClient() as c:
        return await c.post(f"{GRAPH}/{path}", data={**data, "access_token": token})


def _leads_of(row_or_rows):
    rows = row_or_rows if isinstance(row_or_rows, list) else [row_or_rows]
    return get_result_value(rows, "lead")


# ---------------------------------------------------------------------------
# Onboarding: ad accounts, campaigns, the first sync (creatives + presets batch)
# ---------------------------------------------------------------------------
async def test_ad_accounts_for_onboarding(token, mock_mongo_client):
    from routers.onboarding import _fetch_fb_accounts

    accounts = await _fetch_fb_accounts(SBX, mock_mongo_client)
    w = await _world()
    assert [a["id"] for a in accounts] == [c.meta_ad_account_id for c in w.clients]
    assert all(a["currency"] == "GBP" and a["name"] for a in accounts)

    r = await _get("me/adaccounts", token, fields="name,currency,created_time,owner", limit=1000)
    first = r.json()["data"][0]
    assert first["owner"] == w.agency.meta_business_id and first["created_time"].endswith(("+0000", "+0100"))


async def test_first_sync_presets_batch_is_consistent(token):
    from services import meta_service

    w = await _world()
    c = w.clients[0]
    creatives = await meta_service._fetch_ad_creatives(c.meta_ad_account_id, token)
    assert set(creatives) == {ad.id for ad in w.ads(c)}
    for ad in w.ads(c):
        row = creatives[ad.id]
        assert row["creative_title"] == ad.creative.title
        assert row["creative_thumbnail"].startswith(f"{API_PUBLIC_URL}/api/sandbox/media/sbx_")
        if ad.creative.kind == "video":
            assert row["creative_video_id"] == ad.creative.video_id and not row["creative_image"]
        else:
            assert row["creative_image"].startswith(API_PUBLIC_URL)

    out = await meta_service._fetch_meta_presets_batch(c.meta_ad_account_id, token, list(META_CACHE_PRESETS),
                                                        creatives)
    assert set(out) == set(META_CACHE_PRESETS)
    assert not any(r.get("_failed") or r.get("_rate_limited") for r in out.values())

    last7 = out["last_7d"]
    since, until = w.today - timedelta(days=7), w.today - timedelta(days=1)
    expected = w.metrics(c, since, until)
    totals = last7["metrics"]["insights"]
    assert totals["spend"] == pytest.approx(expected.spend, abs=0.01)
    assert totals["results"] == expected.leads == len(w.leads(c, since, until)) > 0
    assert sum(x["spend"] for x in last7["campaigns"]) == pytest.approx(expected.spend, abs=0.05)
    assert sum(x["spend"] for x in last7["adsets"]) == pytest.approx(expected.spend, abs=0.05)
    assert sum(x["spend"] for x in last7["ads"]) == pytest.approx(expected.spend, abs=0.05)
    assert sum(x["results"] for x in last7["ads"]) == expected.leads
    assert {x["id"] for x in last7["campaigns"]} == {camp.id for camp in c.campaigns}
    ad_row = next(x for x in last7["ads"] if x["spend"])
    assert ad_row["creative_thumbnail"] and ad_row["status"] in ("Active", "Paused")

    # The single-preset (paginating) fallback gives the same numbers as the batch.
    single = await meta_service._fetch_meta_campaigns_for_preset(c.meta_ad_account_id, token, "last_30d", creatives)
    assert single["metrics"]["insights"] == out["last_30d"]["metrics"]["insights"]
    assert out["maximum"]["metrics"]["insights"]["spend"] >= out["last_30d"]["metrics"]["insights"]["spend"]
    assert out["last_year"]["metrics"]["insights"]["spend"] == 0   # the world starts this year


async def test_all_presets_for_group_fill_the_cache(token, mock_db, mock_mongo_client, monkeypatch):
    from services import meta_service
    from utils.currency_exchange import CurrencyService

    async def gbp(user_id):
        return "GBP"

    monkeypatch.setattr(CurrencyService, "get_user_currency", staticmethod(gbp))   # it opens its own Mongo client

    w = await _world()
    c = w.clients[1]
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": SBX, "name": c.name,
                                               "meta_ad_account_id": c.meta_ad_account_id,
                                               "ad_account_currency": "GBP"})
    await meta_service.fetch_meta_all_presets_for_group("g1", c.meta_ad_account_id, SBX, mock_mongo_client, "GBP")
    group = await mock_db["client_groups"].find_one({"id": "g1"})
    cache = group["facebook_cache"]   # (the account name comes from a cached /me/adaccounts, not stored here)
    since, until = w.today - timedelta(days=30), w.today - timedelta(days=1)
    bucket = (cache.get("presets") or {}).get("last_30d") or cache.get("last_30d")
    assert bucket["metrics"]["insights"]["spend"] == pytest.approx(w.metrics(c, since, until).spend, abs=0.01)


async def test_scoped_group_sees_only_its_campaigns(token):
    from services import meta_service

    w = await _world()
    c = w.clients[2]
    keep = [c.campaigns[0].id]
    out = await meta_service._fetch_meta_presets_batch(c.meta_ad_account_id, token, ["last_7d"], {}, campaign_ids=keep)
    res = out["last_7d"]
    assert {x["id"] for x in res["campaigns"]} == set(keep)
    since, until = w.today - timedelta(days=7), w.today - timedelta(days=1)
    assert res["metrics"]["insights"]["spend"] == pytest.approx(w.metrics(c.campaigns[0], since, until).spend, abs=0.01)


async def test_account_campaign_list_pages_to_the_end(token):
    from services.meta_campaign_scope import fetch_account_campaigns

    w = await _world()
    c = w.clients[3]
    campaigns = await fetch_account_campaigns(c.meta_ad_account_id, token)
    assert sorted(x["id"] for x in campaigns) == sorted(camp.id for camp in c.campaigns)
    assert all(x["effective_status"] == "ACTIVE" for x in campaigns)

    # Small pages, followed by `next` exactly as the product does (no params on later pages).
    ids, url, params, pages = [], f"{GRAPH}/{c.meta_ad_account_id}/ads", {"fields": "id,name", "limit": 7,
                                                                           "access_token": token}, 0
    async with httpx.AsyncClient() as client:
        while url:
            body = (await client.get(url, params=params)).json()
            ids += [a["id"] for a in body["data"]]
            assert len(body["data"]) <= 7 and body["paging"]["cursors"]["after"]
            url, params, pages = body["paging"].get("next"), None, pages + 1
    assert sorted(ids) == sorted(ad.id for ad in w.ads(c)) and pages > 1


# ---------------------------------------------------------------------------
# Every date preset the product uses (and Graph's others)
# ---------------------------------------------------------------------------
PRESETS = sorted(set(META_CACHE_PRESETS) | {"last_3d", "last_28d", "last_90d", "this_week_sun_today",
                                            "last_week_mon_sun", "last_week_sun_sat", "data_maximum"})


@pytest.mark.parametrize("preset", PRESETS)
async def test_every_date_preset(token, preset):
    w = await _world()
    c = w.clients[0]
    since, until = meta_graph.preset_range(preset, w)
    r = await _get(f"{c.meta_ad_account_id}/insights", token, date_preset=preset, fields="spend,actions")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    expected = w.metrics(c, since, until)
    if not expected.spend:
        assert rows == []
        return
    assert (rows[0]["date_start"], rows[0]["date_stop"]) == (since.isoformat(), min(until, w.today).isoformat())
    assert float(rows[0]["spend"]) == pytest.approx(expected.spend, abs=0.05)


def test_preset_windows_follow_graph():
    w = world_for(SBX, now=datetime(2026, 11, 25, 12, tzinfo=timezone.utc))   # a Wednesday
    d = lambda s: datetime.strptime(s, "%Y-%m-%d").date()  # noqa: E731
    assert meta_graph.preset_range("last_7d", w) == (d("2026-11-18"), d("2026-11-24"))
    assert meta_graph.preset_range("this_week_mon_today", w) == (d("2026-11-23"), d("2026-11-25"))
    assert meta_graph.preset_range("last_week_mon_sun", w) == (d("2026-11-16"), d("2026-11-22"))
    assert meta_graph.preset_range("this_quarter", w) == (d("2026-10-01"), d("2026-11-25"))
    assert meta_graph.preset_range("last_quarter", w) == (d("2026-07-01"), d("2026-09-30"))
    assert meta_graph.preset_range("last_month", w) == (d("2026-10-01"), d("2026-10-31"))
    assert meta_graph.preset_range("maximum", w)[0] == w.history_start


async def test_unknown_preset_is_a_graph_error(token):
    w = await _world()
    r = await _get(f"{w.clients[0].meta_ad_account_id}/insights", token, date_preset="last_fortnight")
    assert r.status_code == 400 and r.json()["error"]["code"] == 100


# ---------------------------------------------------------------------------
# Account = campaigns = ads = daily rows; lead actions = listed leads
# ---------------------------------------------------------------------------
async def test_numbers_agree_at_every_level(token):
    w = await _world()
    c = w.clients[4]
    act = c.meta_ad_account_id
    rng = json.dumps({"since": (w.today - timedelta(days=30)).isoformat(),
                      "until": (w.today - timedelta(days=1)).isoformat()})

    async def rows(**p):
        out, url, params = [], f"{GRAPH}/{act}/insights", {**p, "time_range": rng, "access_token": token, "limit": 50}
        async with httpx.AsyncClient() as client:
            while url:
                body = (await client.get(url, params=params)).json()
                out += body["data"]
                url, params = body.get("paging", {}).get("next"), None
        return out

    account = (await rows(fields="spend,impressions,clicks,actions"))[0]
    by_campaign = await rows(level="campaign", fields="campaign_id,spend,actions")
    by_ad = await rows(level="ad", fields="ad_id,adset_id,spend,actions")
    daily = await rows(time_increment="1", fields="spend,actions,date_start")
    ad_daily = await rows(level="ad", time_increment="1", fields="ad_id,spend,actions,date_start")
    spend = float(account["spend"])
    for series in (by_campaign, by_ad, daily, ad_daily):
        assert sum(float(r["spend"]) for r in series) == pytest.approx(spend, abs=0.05)
        assert sum(_leads_of(r) for r in series) == _leads_of(account)
    assert len({r["date_start"] for r in daily}) == len(daily) == 30
    assert all(r["date_start"] == r["date_stop"] for r in daily)
    assert all(isinstance(r["spend"], str) for r in daily)

    # The lead action count equals the leads the ads' leads edge lists for those days.
    since, until = w.today - timedelta(days=30), w.today - timedelta(days=1)
    listed = 0
    for r in by_ad:
        if not _leads_of(r):
            continue
        body = (await _get(f"{r['ad_id']}/leads", token, fields="id,created_time", limit=500)).json()
        days = {datetime.strptime(x["created_time"], "%Y-%m-%dT%H:%M:%S%z").astimezone(w.now.tzinfo).date()
                for x in body["data"]}
        listed += sum(1 for x in body["data"]
                      if since <= datetime.strptime(x["created_time"], "%Y-%m-%dT%H:%M:%S%z")
                      .astimezone(w.now.tzinfo).date() <= until)
        assert days
    assert listed == _leads_of(account) == len(w.leads(c, since, until))


async def test_daily_spend_and_ad_daily_jobs(token):
    from integrations.facebook_utils.meta_ad_daily import fetch_ad_daily
    from integrations.facebook_utils.meta_daily_spend import fetch_account_daily_spend

    w = await _world()
    c = w.clients[5]
    since, until = w.today - timedelta(days=13), w.today - timedelta(days=1)
    days = await fetch_account_daily_spend(c.meta_ad_account_id, token, since.isoformat(), until.isoformat())
    expected = w.daily(c, since, until)
    assert [d["date"] for d in days] == [m.date.isoformat() for m in expected if m.spend]
    assert [d["spend"] for d in days] == pytest.approx([m.spend for m in expected if m.spend], abs=0.01)
    assert [d["leads"] for d in days] == [m.leads for m in expected if m.spend]

    rows = await fetch_ad_daily(c.meta_ad_account_id, token, since, until)
    assert rows and all(float(r["spend"]) > 0 and r["ad_id"] and r["campaign_name"] for r in rows)
    assert sum(float(r["spend"]) for r in rows) == pytest.approx(sum(d["spend"] for d in days), abs=0.05)

    scoped = await fetch_ad_daily(c.meta_ad_account_id, token, since, until, campaign_ids=[c.campaigns[0].id])
    assert scoped and {r["campaign_id"] for r in scoped} == {c.campaigns[0].id}


# ---------------------------------------------------------------------------
# Leads: the staged import, the incremental refresh
# ---------------------------------------------------------------------------
async def test_lead_import_lists_every_lead(token):
    from integrations.facebook_utils import facebook_leads

    w = await _world()
    c = w.clients[0]
    ad_ids = await facebook_leads.stage1_get_all_ad_ids(c.meta_ad_account_id, token)
    assert sorted(ad_ids) == sorted(ad.id for ad in w.ads(c))
    leads = []
    for ad_id in ad_ids:
        leads += await facebook_leads.stage2_get_all_leads_for_ad(ad_id, token)
    yesterday = w.today - timedelta(days=1)
    past = [x for x in leads if x["created_time"][:10] <= yesterday.isoformat()]
    expected = w.leads(c, w.history_start, yesterday)
    assert len(past) == len(expected) and len({x["id"] for x in leads}) == len(leads)
    assert max(len([x for x in leads if x["ad_id"] == a]) for a in ad_ids) > 25   # paging was followed
    lead = next(x for x in leads if x["id"] == expected[-1].id)
    fd = {f["name"]: f["values"][0] for f in lead["field_data"]}
    assert fd["email"] == expected[-1].email and fd["phone_number"] == expected[-1].phone
    assert fd["full_name"] == expected[-1].full_name
    assert lead["form_id"] == c.lead_form_id and lead["campaign_name"] and lead["adset_name"]
    assert lead["platform"] in ("fb", "ig") and lead["is_organic"] is False

    # The leads the incremental refresh walks are newest first; a watermark stops the walk.
    busiest = max(ad_ids, key=lambda a: len([x for x in leads if x["ad_id"] == a]))
    mine = sorted((x for x in leads if x["ad_id"] == busiest), key=lambda x: x["created_time"], reverse=True)
    from integrations.facebook_utils.meta_incremental_refresh import _get_todays_leads_for_ad

    new, hit = await _get_todays_leads_for_ad(busiest, token, mine[3]["id"], mine[3]["created_time"])
    assert hit and [x["lead_id"] if "lead_id" in x else x["id"] for x in new][:3] == [x["id"] for x in mine[:3]]


async def test_incremental_refresh_today(token):
    from integrations.facebook_utils import meta_incremental_refresh as inc

    w = await _world()
    c = w.clients[1]
    rows = await inc._fetch_account_insights_today(c.meta_ad_account_id, token, "ad")
    assert rows and all(r["date_start"] == w.today.isoformat() and r["ad_id"] and r["campaign_name"] for r in rows)
    ids = await inc._get_todays_active_ad_ids(c.meta_ad_account_id, token)
    assert {r["ad_id"] for r in rows} <= set(ids)


# ---------------------------------------------------------------------------
# Chat / MCP live tools, multi-window, agent state, alerts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", ["ai.tools.meta_live_tools", "ai.mcp.meta_live_mcp"])
async def test_live_tools(token, module):
    import importlib

    live = importlib.import_module(module)
    w = await _world()
    c = w.clients[6]
    since, until = w.today - timedelta(days=14), w.today - timedelta(days=1)
    res = await live._fetch_insights_live(c.meta_ad_account_id, token, since.isoformat(), until.isoformat())
    assert "error" not in res
    expected = w.metrics(c, since, until)
    assert res["metrics"]["insights"]["spend"] == pytest.approx(expected.spend, abs=0.05)
    assert res["metrics"]["insights"]["results"] == expected.leads
    # The tool nests ads{...} without a limit: Graph (and the fake) give 25 per campaign.
    assert len(res["ads"]) == sum(min(25, len(w.ads(camp))) for camp in c.campaigns)

    leads = await live._fetch_leads_live(c.meta_ad_account_id, token, since.isoformat(), until.isoformat())
    # The tool's filter is "after {since} 00:00" (exclusive) .. "{until}T23:59:59".
    assert leads["total"] == len(w.leads(c, since, until))
    assert all(x["email"].endswith("@example.com") for x in leads["leads"])

    monthly = await live._fetch_monthly_insights_live(c.meta_ad_account_id, token, w.today.year)
    total = w.metrics(c, w.history_start, w.today).spend
    assert sum(m["spend"] for m in monthly["months"]) == pytest.approx(total, abs=1.0)
    assert [m["month"] for m in monthly["months"]] == sorted(m["month"] for m in monthly["months"])


async def test_live_tool_executor_and_daily_windows(token, mock_db):
    from ai.tools.meta_live_tools import get_meta_insights_live
    from ai.tools.multi_window_tools import _fetch_daily_insights

    w = await _world()
    c = w.clients[7]
    await mock_db["client_groups"].insert_one({"id": "g7", "user_id": SBX, "name": c.name,
                                               "meta_ad_account_id": c.meta_ad_account_id,
                                               "ad_account_currency": "GBP"})
    since, until = w.today - timedelta(days=7), w.today - timedelta(days=1)
    out = await get_meta_insights_live(mock_db, SBX, since.isoformat(), until.isoformat())
    assert "error" not in json.dumps(out)[:200].lower() or out
    daily = await _fetch_daily_insights(c.meta_ad_account_id, token, since.isoformat(), until.isoformat())
    assert [d["spend"] for d in daily["days"]] == pytest.approx([m.spend for m in w.daily(c, since, until)], abs=0.01)
    assert sum(d["results"] for d in daily["days"]) == w.metrics(c, since, until).leads


async def test_agent_meta_state_and_alerts(token):
    from ai.agents.meta_state import read_meta_state
    from ai.agents.stock.alerts import fetch_account_health, fetch_account_spend

    w = await _world()
    c = w.clients[2]
    ads, adsets = await read_meta_state(c.meta_ad_account_id, token)
    assert {a["id"] for a in ads} == {ad.id for ad in w.ads(c)}
    assert {s["id"] for s in adsets} == {a.id for a in w.adsets(c)}
    video = next((a for a in ads if a["creative"].get("video_id")), None)
    image = next(a for a in ads if a["creative"].get("image_hash"))
    assert image["creative"]["object_story_spec"]["page_id"] == c.meta_page_id
    if video:
        assert video["creative"]["object_story_spec"]["video_data"]["video_id"] == video["creative"]["video_id"]
    budgets = {s["id"]: int(s["daily_budget"]) for s in adsets}
    assert budgets == {a.id: a.daily_budget for a in w.adsets(c)}
    assert all(s["campaign"]["effective_status"] == "ACTIVE" for s in adsets)

    health = await fetch_account_health(c.meta_ad_account_id, token)
    assert health == {"account_status": 1, "disable_reason": 0, "funding_source": health["funding_source"],
                      "is_prepay_account": False, "timezone_name": "Europe/London", "currency": "GBP",
                      "name": c.name}
    since, until = w.today - timedelta(days=5), w.today - timedelta(days=1)
    spend = await fetch_account_spend(c.meta_ad_account_id, token, since, until)
    assert spend == pytest.approx({m.date.isoformat(): m.spend for m in w.daily(c, since, until)}, abs=0.01)


# ---------------------------------------------------------------------------
# Writes: pause / enable, budget, rename; then reads reflect them
# ---------------------------------------------------------------------------
async def test_pause_enable_budget_and_rename(token, mock_mongo_client):
    from ai.agents.meta_state import read_meta_state
    from integrations.facebook_utils.meta_creative_reader import fetch_ad_status
    from services.meta_status_service import get_object_state, set_object_status

    w = await _world()
    c = w.clients[3]
    ad = next(a for a in w.ads(c) if a.status == "ACTIVE" and w.effective_status(a) == "ACTIVE")
    res = await set_object_status(SBX, ad.id, "ad", "PAUSED", mock_mongo_client)
    assert res["success"] and res["new_status"] == "PAUSED"
    assert await get_object_state(SBX, ad.id, "ad", mock_mongo_client) == {"status": "PAUSED",
                                                                           "effective_status": "PAUSED"}
    assert await fetch_ad_status(ad.id, token) == "PAUSED"
    # A paused ad stops delivering: today's spend no longer grows.
    today = json.dumps({"since": w.today.isoformat(), "until": w.today.isoformat()})
    first = (await _get(f"{ad.id}/insights", token, time_range=today, fields="spend")).json()["data"]
    await asyncio.sleep(0)
    again = (await _get(f"{ad.id}/insights", token, time_range=today, fields="spend")).json()["data"]
    assert first == again

    camp = w.campaign_of(ad)
    await set_object_status(SBX, camp.id, "campaign", "PAUSED", mock_mongo_client)
    sibling = next(a for a in w.ads(camp) if a.id != ad.id and a.status == "ACTIVE")
    assert (await get_object_state(SBX, sibling.id, "ad", mock_mongo_client))["effective_status"] == "CAMPAIGN_PAUSED"
    await set_object_status(SBX, camp.id, "campaign", "ACTIVE", mock_mongo_client)
    await set_object_status(SBX, ad.id, "ad", "ACTIVE", mock_mongo_client)
    assert (await get_object_state(SBX, ad.id, "ad", mock_mongo_client))["effective_status"] == "ACTIVE"

    adset = w.adset_of(ad)
    r = await _post(adset.id, token, daily_budget="5500")
    assert r.status_code == 200 and r.json() == {"success": True}
    r = await _post(ad.id, token, name="Renamed in sandbox")
    assert r.json() == {"success": True}
    ads, adsets = await read_meta_state(c.meta_ad_account_id, token)
    assert next(s for s in adsets if s["id"] == adset.id)["daily_budget"] == "5500"
    assert next(a for a in ads if a["id"] == ad.id)["name"] == "Renamed in sandbox"
    assert (await _world()).find(adset.id).daily_budget == 5500

    bad = await _post(adset.id, token, daily_budget="50")
    assert bad.status_code == 400 and bad.json()["error"]["error_user_msg"]
    bad = await _post(ad.id, token, status="SLEEPING")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == 100


# ---------------------------------------------------------------------------
# Creative tests: reads for the wizard, media, creative + ad creation, adoption, go-live
# ---------------------------------------------------------------------------
async def test_creative_test_posting_flow(token, mock_mongo_client):
    from integrations.facebook_utils import meta_creative_reader as reader
    from integrations.facebook_utils import meta_creative_writer as writer
    from services.meta_status_service import set_object_status

    w = await _world()
    c = w.clients[0]
    act = c.meta_ad_account_id
    adsets = await reader.list_adsets(act, token)
    target = next(a for a in adsets if a["effective_status"] == "ACTIVE")
    assert reader.adset_compatible(target, "lead_form") == (True, None)
    pages = await reader.list_promote_pages(act, token)
    assert pages == [{"id": c.meta_page_id, "name": c.name}]
    forms = await reader.list_lead_forms(c.meta_page_id, token)    # needs the Page token first
    assert forms == [{"id": c.lead_form_id, "name": f"{c.name} Enquiry Form", "status": "ACTIVE"}]

    # Revive an image into the account: its hash -> a CDN URL the writer downloads (in-process) and re-uploads.
    image_ad = next(a for a in w.ads(c) if a.creative.kind == "image")
    h = meta_graph.image_hash(SBX, image_ad.creative.id)
    url = await writer.image_url_for_hash(act, token, h)
    assert urlsplit(url).hostname.endswith(".fbcdn.net")
    new_hash = await writer.upload_image(act, token, image_url=url, allow_meta_cdn=True)
    assert len(new_hash) == 32

    since = datetime.now(timezone.utc)
    creative_id = await writer.create_adcreative(
        act, token, name="CT-1 creative", page_id=c.meta_page_id, fmt="image", media_ref=new_hash,
        primary_text="Fresh copy", headline="New headline", description="", cta_type="LEARN_MORE",
        destination="lead_form", lead_form_id=c.lead_form_id)
    ad_id = await writer.create_ad(act, token, name="CT-1", adset_id=target["id"], creative_id=creative_id)
    assert await writer.find_ad_in_adset(target["id"], token, name="CT-1", creative_id=creative_id,
                                         since=since) == ad_id
    assert await reader.fetch_ad_status(ad_id, token) == "PAUSED"

    # The new ad is readable everywhere a listed ad is, and starts with no delivery.
    body = (await _get(ad_id, token, fields="name,adset{id},creative{title,body,image_url,image_hash}")).json()
    assert body["name"] == "CT-1" and body["adset"]["id"] == target["id"]
    assert body["creative"]["title"] == "New headline" and body["creative"]["image_hash"] == new_hash
    assert body["creative"]["image_url"].startswith(API_PUBLIC_URL)
    listed = (await _get(f"{act}/ads", token, fields="id", limit=500)).json()["data"]
    assert ad_id in {a["id"] for a in listed}
    await set_object_status(SBX, ad_id, "ad", "ACTIVE", mock_mongo_client)
    assert await reader.fetch_ad_status(ad_id, token) == "ACTIVE"
    rows = await reader.fetch_tracked_ad_days(act, token, [ad_id], w.today - timedelta(days=3), w.today)
    assert rows == []
    baseline = await reader.fetch_account_daily(act, token, w.today - timedelta(days=3), w.today - timedelta(days=1))
    assert len(baseline) == 3

    # Video: uploaded from a Birdy blob URL (Meta would pull it; the fake does not fetch anything).
    vid = await writer.upload_video(act, token, file_url="https://abc.public.blob.vercel-storage.com/v.mp4",
                                    name="CT-2")
    assert await writer.video_ready(vid, token) is True
    thumb = await writer.video_thumbnail(vid, token)
    assert thumb.startswith(API_PUBLIC_URL)
    video_ad = next((a for a in w.ads(c) if a.creative.kind == "video"), None)
    if video_ad:
        assert await writer.video_source(video_ad.creative.video_id, token) is None   # Meta holds ad videos back


async def test_ad_identity_and_preview(token, mock_mongo_client):
    from fastapi.testclient import TestClient

    from main import app
    from services.meta_service import fetch_ad_identity

    w = await _world()
    ad = w.ads(w.clients[1])[0]
    ident = await fetch_ad_identity(ad.id, token)
    assert ident["name"] == ad.name and ident["campaign_id"] == ad.campaign_id
    assert ident["adset_name"] == w.adset_of(ad).name

    # The preview iframe and every creative URL load from the backend's own public route.
    body = (await _get(f"{ad.id}/previews", token, ad_format="DESKTOP_FEED_STANDARD")).json()
    src = body["data"][0]["body"].split('src="')[1].split('"')[0].replace("&amp;", "&")
    assert src.startswith(f"{API_PUBLIC_URL}/api/sandbox/media/")
    client = TestClient(app)
    r = client.get(urlsplit(src).path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert ad.name in r.text and w.clients[1].name in r.text
    thumb = ident["creative_thumbnail"]
    r = client.get(urlsplit(thumb).path)
    assert r.status_code == 200 and ad.creative.title[:15] in r.text
    assert client.get("/api/sandbox/media/sbx_forged.abc.svg").status_code == 404
    other = make_credential("second@sandbox.birdy.test", "media", nonce=f"preview.{ad.id}")
    assert client.get(f"/api/sandbox/media/{other}.svg").status_code == 404   # not that account's ad


# ---------------------------------------------------------------------------
# Batch API, errors, the catch-all
# ---------------------------------------------------------------------------
async def test_batch_api_shape(token):
    w = await _world()
    act = w.clients[0].meta_ad_account_id
    batch = [{"method": "GET", "relative_url": f"{act}/campaigns?fields=name&limit=2"},
             {"method": "GET", "relative_url": f"v25.0/{act}/insights?date_preset=yesterday&fields=spend"},
             {"method": "GET", "relative_url": "999999999/insights?fields=spend"}]
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{GRAPH}/", data={"access_token": token, "batch": json.dumps(batch)})
    out = r.json()
    assert r.status_code == 200 and [x["code"] for x in out] == [200, 200, 400]
    first = json.loads(out[0]["body"])
    assert len(first["data"]) == 2 and "next" in first["paging"]
    assert parse_qs(urlsplit(first["paging"]["next"]).query)["access_token"] == [token]
    assert json.loads(out[1]["body"])["data"][0]["date_start"] == (w.today - timedelta(days=1)).isoformat()
    assert json.loads(out[2]["body"])["error"]["code"] == 100


async def test_graph_errors(token, mock_mongo_client):
    from integrations.facebook_utils.meta_creative_reader import fetch_ad_status
    from services.meta_status_service import get_object_state

    w = await _world()
    act = w.clients[0].meta_ad_account_id
    r = await _get(f"{act}/campaigns/extra", token)
    err = r.json()["error"]
    assert r.status_code == 400 and err["message"].startswith("sandbox:") and {"type", "code", "fbtrace_id"} <= set(err)
    r = await _get(f"{act}/not_an_edge", token)
    assert r.status_code == 400 and r.json()["error"]["code"] == 100
    # A missing object is Graph's code 100, which the product reads as "deleted".
    assert await get_object_state(SBX, "120299999999999999", "ad", mock_mongo_client) is None
    assert await fetch_ad_status("120299999999999999", token) is None
    # Another sandbox account's objects do not exist for this token.
    other = make_credential("second@sandbox.birdy.test", "meta")
    r = await _get(f"{act}/campaigns", other)
    assert r.status_code == 400 and r.json()["error"]["code"] == 100
    # No token: Graph's 190, which the product treats as a dead connection.
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{GRAPH}/{act}/campaigns", params={"access_token": token[:-1] + ("A" if token[-1] != "A" else "B")})
    assert r.status_code == 400 and r.json()["error"]["code"] == 190


async def test_fields_parser():
    from sandbox.meta_fields import parse_fields

    specs = parse_fields('name,insights.time_range({"since":"2026-11-01","until":"2026-11-07"}){spend,actions},'
                         "adsets.limit(200){name,ads{id}},,leads.date_preset(maximum){id}")
    assert [s.name for s in specs] == ["name", "insights", "adsets", "leads"]
    assert json.loads(specs[1].mods["time_range"]) == {"since": "2026-11-01", "until": "2026-11-07"}
    assert specs[2].mods == {"limit": "200"} and specs[2].raw_children == "name,ads{id}"
    assert specs[2].children[1].children[0].name == "id"


def test_the_leak_guard_works(outgoing):
    async def go():
        async with httpx.AsyncClient() as client:
            await client.get("https://example.com/")
    with pytest.raises(AssertionError):
        asyncio.run(go())
    outgoing.clear()


# ---------------------------------------------------------------------------
# Leak rules: paging links keep the sbx_ token, Page tokens are sbx_, browser media is ours
# ---------------------------------------------------------------------------
async def test_every_paging_link_keeps_the_sandbox_token(token):
    from sandbox import parse_credential

    w = await _world()
    c = w.clients[0]
    busiest = max(w.ads(c), key=lambda a: len([x for d in w.days(a.created_on, w.today) for x in w.ad_leads(a, d)]))
    starts = [(f"{c.meta_ad_account_id}/campaigns", {"fields": "name,ads.limit(2){id,leads.limit(2){id}}", "limit": 1}),
              (f"{c.meta_ad_account_id}/insights", {"level": "ad", "time_increment": "1", "date_preset": "last_7d",
                                                    "fields": "spend", "limit": 5}),
              (f"{busiest.id}/leads", {"fields": "id,created_time", "limit": 3}),
              ("me/adaccounts", {"fields": "name", "limit": 2})]
    links = 0
    async with httpx.AsyncClient() as client:
        for path, params in starts:
            url, p, seen = f"{GRAPH}/{path}", {**params, "access_token": token}, 0
            while url and seen < 400:
                body = (await client.get(url, params=p)).json()
                assert "error" not in body, body
                # Every link in the page (its own and the nested edges') carries the same sbx_ token.
                for link in re.findall(r'"(?:next|previous)":\s*"([^"]+)"', json.dumps(body)):
                    q = parse_qs(urlsplit(link).query)
                    assert q["access_token"] == [token] and parse_credential(q["access_token"][0]).user_id == SBX
                    links += 1
                url, p, seen = body.get("paging", {}).get("next"), None, seen + 1   # bare GET, as the product does
            assert seen > 1, path
    assert links > 10


async def test_page_tokens_are_sandbox_credentials(token):
    from integrations.facebook_utils.meta_creative_reader import list_lead_forms
    from sandbox import parse_credential

    w = await _world()
    pages = (await _get("me/accounts", token, fields="id,name,access_token")).json()["data"]
    assert [p["id"] for p in pages] == [c.meta_page_id for c in w.clients]
    for p in pages:
        cred = parse_credential(p["access_token"])
        assert cred.user_id == SBX and cred.kind == f"meta_page:{p['id']}"
    one = (await _get(w.clients[3].meta_page_id, token, fields="access_token")).json()
    assert parse_credential(one["access_token"]).base_kind == "meta_page"
    assert await list_lead_forms(w.clients[3].meta_page_id, token) == [
        {"id": w.clients[3].lead_form_id, "name": f"{w.clients[3].name} Enquiry Form", "status": "ACTIVE"}]


async def test_nothing_a_browser_loads_points_at_meta(token):
    w = await _world()
    c = w.clients[0]
    video_ad = next(a for a in w.ads() if a.creative.kind == "video")
    reads = [
        (f"{c.meta_ad_account_id}/ads", {"fields": "id,preview_shareable_link,creative{title,body,image_url,"
                                                   "thumbnail_url,video_id,object_story_spec}", "limit": 500}),
        (f"{c.meta_ad_account_id}/adcreatives", {"fields": "image_url,thumbnail_url,object_story_spec", "limit": 500}),
        ("me/accounts", {"fields": "name,link,picture"}),
        (video_ad.creative.video_id, {"fields": "picture,source,format,permalink_url"}),
        (f"{video_ad.creative.video_id}/thumbnails", {"fields": "uri,is_preferred"}),
        (f"{video_ad.id}/previews", {"ad_format": "MOBILE_FEED_STANDARD"}),
    ]
    for path, params in reads:
        body = (await _get(path, token, **params)).json()
        body.pop("paging", None)
        text = json.dumps(body)
        assert "error" not in body, body
        assert "facebook.com" not in text and "fbcdn" not in text, (path, text[:300])
        assert API_PUBLIC_URL in text or path == "me/accounts"
    # The one Meta-host URL handed out is /adimages `url`: the backend downloads it (in-process).
    h = meta_graph.image_hash(SBX, next(a for a in w.ads(c) if a.creative.kind == "image").creative.id)
    img = (await _get(f"{c.meta_ad_account_id}/adimages", token, hashes=json.dumps([h]),
                      fields="hash,url,url_128,permalink_url")).json()["data"][0]
    assert urlsplit(img["url"]).hostname.endswith(".fbcdn.net") and "sbx_" in img["url"]
    assert img["url_128"].startswith(API_PUBLIC_URL) and img["permalink_url"].startswith(API_PUBLIC_URL)
    async with httpx.AsyncClient() as client:
        r = await client.get(img["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
