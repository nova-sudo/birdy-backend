"""Attribution MCP tools (ai/mcp/attribution_mcp.py): status, leads_by_ad, funnel, attribution_overview."""

from datetime import datetime

import pytest
from fastmcp.exceptions import ToolError

from ai.mcp import attribution_mcp as t

ALICE, BOB = "alice@example.com", "bob@example.com"

pytestmark = pytest.mark.asyncio


@pytest.fixture
def alice(set_current_user):
    set_current_user(ALICE)


@pytest.fixture
def bob(set_current_user):
    set_current_user(BOB)


async def _group(db, gid="g1", user=ALICE, name="Acme Dental", method="landing_page", **extra):
    await db["client_groups"].insert_one({
        "id": gid, "user_id": user, "name": name, "attribution_site_id": f"site-{gid}",
        "lead_collection": {"method": method}, **extra,
    })


async def _seed_tracking(db, gid="g1", user=ALICE):
    await db["attribution_visitors"].insert_one({
        "client_group_id": gid, "visitor_id": f"v-{gid}", "last_seen_at": datetime(2026, 11, 10),
        "last_paid_touch": {"ad_id": f"ad-{gid}"},
    })
    await db["attribution_matches"].insert_one({
        "user_id": user, "client_group_id": gid, "ad_id": f"ad-{gid}", "adset_id": "as1", "campaign_id": "c1",
        "contact_created_at": datetime(2026, 11, 5), "confidence": 100,
    })
    await db["landing_page_daily"].insert_one({
        "client_group_id": gid, "day": "2026-11-05", "pageviews": 10, "visitors": 8, "views": 8,
        "ad_views": 6, "form_starts": 4, "opt_ins": 2,
    })


async def test_tool_tags_and_docstrings():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    for name in ("attribution_status", "leads_by_ad", "funnel", "attribution_overview"):
        assert tools[name].tags == {"read"}
        assert len(tools[name].description) > 60
    assert "get_closes_by_ad" in tools["leads_by_ad"].description


async def test_attribution_status_merges_status_and_diagnostics(mock_db, alice):
    await _group(mock_db)
    await _seed_tracking(mock_db)
    out = await t.attribution_status.fn(client="g1")
    assert out["group_id"] == "g1"
    assert out["status"]["installed"] is True
    assert out["status"]["attributed_leads"] == 1
    assert out["status"]["first_ad_click_seen"] is True
    assert "stages" in out["diagnostics"] and out["diagnostics"]["method"] == "landing_page"


async def test_leads_by_ad_happy_path(mock_db, alice):
    await _group(mock_db)
    await _seed_tracking(mock_db)
    out = await t.leads_by_ad.fn(client="Acme Dental", start_date="2026-11-01", end_date="2026-11-30")
    assert out["group_id"] == "g1"
    assert out["attributed_leads"] == 1
    assert out["ads"][0]["ad_id"] == "ad-g1"
    # outside the window: nothing
    out = await t.leads_by_ad.fn(client="g1", start_date="2026-12-01", end_date="2026-12-31")
    assert out["ads"] == [] and out["attributed_leads"] == 0


async def test_funnel_happy_path(mock_db, alice):
    await _group(mock_db)
    await _seed_tracking(mock_db)
    out = await t.funnel.fn(client="g1", start_date="2026-11-01", end_date="2026-11-30")
    assert out["applicable"] is True
    assert out["totals"]["opt_ins"] == 2 and out["totals"]["form_starts"] == 4
    assert out["start"] == "2026-11-01" and out["end"] == "2026-11-30"


async def test_funnel_not_applicable_for_instant_form(mock_db, alice):
    await _group(mock_db, method="instant_form")
    out = await t.funnel.fn(client="g1", start_date="2026-11-01", end_date="2026-11-30")
    assert out["applicable"] is False and out["reason"]


async def test_funnel_defaults_to_last_30_days(mock_db, alice):
    await _group(mock_db)
    out = await t.funnel.fn(client="g1")
    assert out["applicable"] is True
    s, e = datetime.fromisoformat(out["start"]), datetime.fromisoformat(out["end"])
    assert (e - s).days == 30


async def test_attribution_overview_only_callers_groups(mock_db, alice):
    await _group(mock_db, "g1", ALICE, "Acme Dental")
    await _group(mock_db, "g2", ALICE, "Beta Law", method="unknown")
    await _group(mock_db, "gb", BOB, "Bobs Gym")
    await _seed_tracking(mock_db, "g1", ALICE)
    await _seed_tracking(mock_db, "gb", BOB)
    out = await t.attribution_overview.fn()
    ids = {r["group_id"] for r in out["clients"]}
    assert ids == {"g1", "g2"}
    by_id = {r["group_id"]: r for r in out["clients"]}
    assert by_id["g1"]["attributed_leads"] == 1 and by_id["g1"]["installed"] is True


async def test_attribution_scoped_to_user(mock_db, bob):
    """Alice has real data; bob can see none of it, by id or by name."""
    await _group(mock_db, "g1", ALICE, "Acme Dental")
    await _seed_tracking(mock_db, "g1", ALICE)
    for call in (
        t.attribution_status.fn(client="g1"),
        t.leads_by_ad.fn(client="g1"),
        t.funnel.fn(client="g1", start_date="2026-11-01", end_date="2026-11-30"),
        t.leads_by_ad.fn(client="Acme Dental"),
    ):
        with pytest.raises(ToolError, match="No client matches|Your clients|no clients"):
            await call
    assert (await t.attribution_overview.fn()) == {"clients": []}


async def test_foreign_name_collision_does_not_leak(mock_db, bob):
    """Bob has a same-named client; he sees only his own (empty) numbers, never alice's."""
    await _group(mock_db, "g1", ALICE, "Acme Dental")
    await _seed_tracking(mock_db, "g1", ALICE)
    await _group(mock_db, "gb", BOB, "Acme Dental")
    out = await t.leads_by_ad.fn(client="Acme Dental")
    assert out["group_id"] == "gb" and out["ads"] == []
    status = await t.attribution_status.fn(client="Acme Dental")
    assert status["status"]["installed"] is False and status["status"]["attributed_leads"] == 0


async def test_ambiguous_client_returns_candidates(mock_db, alice):
    await _group(mock_db, "g1", ALICE, "Acme Dental")
    await _group(mock_db, "g2", ALICE, "Acme Law")
    for call in (t.attribution_status.fn(client="Acme"), t.leads_by_ad.fn(client="Acme"), t.funnel.fn(client="Acme")):
        with pytest.raises(ToolError, match="Acme Dental.*Acme Law|Acme Law.*Acme Dental"):
            await call


async def test_bad_dates_are_clear_errors(mock_db, alice):
    await _group(mock_db)
    with pytest.raises(ToolError, match="Invalid date: nope"):
        await t.leads_by_ad.fn(client="g1", start_date="nope")
    with pytest.raises(ToolError, match="Invalid end date: 31/11"):
        await t.funnel.fn(client="g1", start_date="2026-11-01", end_date="31/11")
    with pytest.raises(ToolError, match="start must not be after end"):
        await t.funnel.fn(client="g1", start_date="2026-11-30", end_date="2026-11-01")
