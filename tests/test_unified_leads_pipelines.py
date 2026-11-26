"""
GET /api/leads/unified — client lead pipeline additions.

With exactly one group: opportunities carry `pipelineStageName` / `pipelineName`,
`meta.pipelines` lists the client's CRM pipelines (CRM order) with per-stage
contact counts over the table's filters, and `stage_id` narrows the table and
its paging. With several groups nothing changes. Unreadable pipelines give [].
"""

import contextlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import routers.client_groups as cg_router
from core.database import DB_NAME
from dependencies import get_current_user
from main import app
from services import ghl_pipelines

USER = "u1"
LOC = "loc-a"

PIPELINES = [
    {"id": "p-sales", "name": "Sales", "stages": [
        # Deliberately out of position order: `position` decides.
        {"id": "s-app", "name": "Application", "position": 1},
        {"id": "s-opt", "name": "Opt-in", "position": 0},
        {"id": "s-book", "name": "Booking", "position": 2},
    ]},
    {"id": "p-empty", "name": "Empty", "stages": []},
    {"id": "p-retain", "name": "Retention", "stages": [{"id": "s-renew", "name": "Renewal"}]},
]


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(cg_router, "get_mongo_client", fake_client)
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def no_ghl(monkeypatch):
    """Fail loudly if anything reaches GoHighLevel: the cache must serve."""
    async def boom(*a, **k):
        raise AssertionError("GoHighLevel must not be called")

    monkeypatch.setattr(ghl_pipelines.ghl_integration, "fetch_pipelines", boom)

    async def no_tokens(user_id, mongo_client):
        return {}

    monkeypatch.setattr(ghl_pipelines, "get_subaccount_tokens", no_tokens)


def _opp(oid, stage, status="open"):
    return {"id": oid, "pipelineId": "p-sales", "pipelineStageId": stage, "status": status, "monetaryValue": 0}


async def _seed(mock_mongo_client, *, cache=True, group_location=LOC):
    db = mock_mongo_client[DB_NAME]
    await db["client_groups"].insert_many([
        {"id": "g1", "user_id": USER, "name": "Alpha", "ghl_location_id": group_location},
        {"id": "g2", "user_id": USER, "name": "Beta", "ghl_location_id": "loc-b"},
    ])
    if cache:
        await db["ghl_pipelines"].insert_one({
            "user_id": USER, "location_id": LOC, "pipelines": PIPELINES,
            "fetched_at": datetime.now(timezone.utc),
        })
    rows = [
        # (contact id, group, day, opportunities)
        ("c1", "g1", "2026-11-01", [_opp("o1", "s-opt")]),
        ("c2", "g1", "2026-11-02", [_opp("o2", "s-app"), _opp("o2b", "s-app", "lost")]),
        ("c3", "g1", "2026-11-03", [_opp("o3", "s-app", "won")]),
        ("c4", "g1", "2026-11-04", [_opp("o4", "s-book"), _opp("o4b", "s-unknown")]),
        ("c5", "g1", "2026-11-05", []),
        ("c6", "g1", "2026-11-06", [_opp("o6", "s-app")]),
        ("c7", "g2", "2026-11-07", [_opp("o7", "s-app")]),
    ]
    await db["ghl_contacts"].insert_many([{
        "user_id": USER, "client_group_id": g, "client_group_name": g, "location_id": LOC if g == "g1" else "loc-b",
        "lead_type": "lead",
        "contact_data": {"id": cid, "firstName": cid, "email": f"{cid}@x.com",
                         "dateAdded": f"{day}T10:00:00.000Z", "opportunities": opps},
    } for cid, g, day, opps in rows])


def _stages(body):
    return {s["id"]: s["count"] for p in body["meta"]["pipelines"] for s in p["stages"]}


async def test_single_group_names_opportunities_and_lists_pipelines(api, mock_mongo_client, no_ghl):
    await _seed(mock_mongo_client)

    body = api.get("/api/leads/unified?groups=g1&limit=50").json()

    pipelines = body["meta"]["pipelines"]
    assert [p["id"] for p in pipelines] == ["p-sales", "p-retain"]  # CRM order, empty pipeline dropped
    assert [s["name"] for s in pipelines[0]["stages"]] == ["Opt-in", "Application", "Booking"]
    # c2 has two opportunities in Application but counts once.
    assert _stages(body) == {"s-opt": 1, "s-app": 3, "s-book": 1, "s-renew": 0}

    by_id = {c["contactId"]: c for c in body["contacts"]}
    o2 = by_id["c2"]["opportunities"][0]
    assert o2["pipelineStageName"] == "Application" and o2["pipelineName"] == "Sales"
    assert o2["status"] == "open" and o2["id"] == "o2"  # existing fields untouched
    unknown = by_id["c4"]["opportunities"][1]
    assert unknown["pipelineStageName"] is None and unknown["pipelineName"] is None


async def test_stage_id_filters_and_pages(api, mock_mongo_client, no_ghl):
    await _seed(mock_mongo_client)

    p1 = api.get("/api/leads/unified?groups=g1&stage_id=s-app&limit=2&page=1").json()
    p2 = api.get("/api/leads/unified?groups=g1&stage_id=s-app&limit=2&page=2").json()

    assert p1["meta"]["total_contacts"] == 3
    assert p1["meta"]["total_pages"] == 2 and p1["meta"]["has_next"] is True
    assert [c["contactId"] for c in p1["contacts"]] == ["c6", "c3"]
    assert [c["contactId"] for c in p2["contacts"]] == ["c2"] and p2["meta"]["has_next"] is False
    # Counts ignore stage_id; stats stay on the unfiltered set.
    assert _stages(p1) == {"s-opt": 1, "s-app": 3, "s-book": 1, "s-renew": 0}
    assert p1["meta"]["stats"]["lead_count"] == 6


async def test_counts_follow_other_filters(api, mock_mongo_client, no_ghl):
    await _seed(mock_mongo_client)

    won = api.get("/api/leads/unified?groups=g1&opportunity_status=won").json()
    assert _stages(won) == {"s-opt": 0, "s-app": 1, "s-book": 0, "s-renew": 0}

    dated = api.get("/api/leads/unified?groups=g1&start_date=2026-11-03&end_date=2026-11-06").json()
    assert _stages(dated) == {"s-opt": 0, "s-app": 2, "s-book": 1, "s-renew": 0}

    both = api.get("/api/leads/unified?groups=g1&opportunity_status=won&stage_id=s-app").json()
    assert [c["contactId"] for c in both["contacts"]] == ["c3"]


async def test_multi_group_unchanged(api, mock_mongo_client, no_ghl):
    await _seed(mock_mongo_client)

    body = api.get("/api/leads/unified?groups=g1,g2&stage_id=s-opt&limit=50").json()

    assert "pipelines" not in body["meta"]
    assert body["meta"]["total_contacts"] == 7  # stage_id ignored
    opp = next(c for c in body["contacts"] if c["contactId"] == "c1")["opportunities"][0]
    assert "pipelineStageName" not in opp and "pipelineName" not in opp

    none = api.get("/api/leads/unified").json()
    assert "pipelines" not in none["meta"]


async def test_unreadable_pipelines_give_empty_list(api, mock_mongo_client, no_ghl):
    await _seed(mock_mongo_client, cache=False)  # no cache, no token

    body = api.get("/api/leads/unified?groups=g1&limit=50").json()

    assert body["meta"]["pipelines"] == []
    assert body["meta"]["total_contacts"] == 6
    opp = next(c for c in body["contacts"] if c["contactId"] == "c1")["opportunities"][0]
    assert opp["pipelineStageName"] is None and opp["pipelineName"] is None


async def test_group_without_location_or_unknown_group(api, mock_mongo_client, no_ghl):
    await _seed(mock_mongo_client, group_location=None)

    assert api.get("/api/leads/unified?groups=g1").json()["meta"]["pipelines"] == []
    other = api.get("/api/leads/unified?groups=not-mine").json()
    assert other["meta"]["pipelines"] == [] and other["meta"]["total_contacts"] == 0


async def test_ghl_failure_gives_empty_list(api, mock_mongo_client, monkeypatch):
    await _seed(mock_mongo_client, cache=False)

    async def tokens(user_id, mongo_client):
        return {LOC: {"access_token": "tok"}}

    async def fail(location_id, access_token):
        return False, []

    monkeypatch.setattr(ghl_pipelines, "get_subaccount_tokens", tokens)
    monkeypatch.setattr(ghl_pipelines.ghl_integration, "fetch_pipelines", fail)

    resp = api.get("/api/leads/unified?groups=g1")
    assert resp.status_code == 200 and resp.json()["meta"]["pipelines"] == []


async def test_ordered_pipelines_refreshes_once_then_caches(mock_db, monkeypatch):
    calls = {"n": 0}

    async def tokens(user_id, mongo_client):
        return {LOC: {"access_token": "tok"}}

    async def fetch(location_id, access_token):
        calls["n"] += 1
        return True, PIPELINES

    monkeypatch.setattr(ghl_pipelines, "get_subaccount_tokens", tokens)
    monkeypatch.setattr(ghl_pipelines.ghl_integration, "fetch_pipelines", fetch)

    first = await ghl_pipelines.ordered_pipelines(mock_db, USER, LOC)
    second = await ghl_pipelines.ordered_pipelines(mock_db, USER, LOC)
    lookup = await ghl_pipelines.stage_lookup(mock_db, USER, LOC)

    assert calls["n"] == 1
    assert first == second
    assert first[0] == {"id": "p-sales", "name": "Sales", "stages": [
        {"id": "s-opt", "name": "Opt-in"}, {"id": "s-app", "name": "Application"}, {"id": "s-book", "name": "Booking"},
    ]}
    assert first[1] == {"id": "p-empty", "name": "Empty", "stages": []}
    assert lookup["s-book"] == {"stage_name": "Booking", "pipeline_id": "p-sales", "pipeline_name": "Sales"}
