"""
GET /api/leads/unified — lead stage tabs for one client.

With exactly one group: `meta.lead_stages` counts all / meta / crm / called /
closed / lost over the table's filters (incl. stage_id, not lead_stage), and
`lead_stage` narrows the table and its paging. crm + called + closed + lost
partition all; meta overlaps. `meta` and `called` use the same Meta / call-centre
match the enrichment uses. Anything going wrong leaves the response as before.
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
PIPELINES = [{"id": "p-sales", "name": "Sales", "stages": [
    {"id": "s-opt", "name": "Opt-in", "position": 0},
    {"id": "s-app", "name": "Application", "position": 1},
]}]


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def boom(*a, **k):
        raise AssertionError("GoHighLevel must not be called")

    monkeypatch.setattr(cg_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(ghl_pipelines.ghl_integration, "fetch_pipelines", boom)
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _opp(status, stage="s-opt"):
    return {"id": f"o-{status}-{stage}", "pipelineId": "p-sales", "pipelineStageId": stage, "status": status}


def _key(cid):
    return f"email:{cid}@x.com"


# (contact id, group, day, opportunities, has match keys) -> expected stage
ROWS = [
    ("c1", "g1", "2026-11-01", [_opp("open")], True),                       # called + meta
    ("c2", "g1", "2026-11-02", [_opp("Won", "s-app"), _opp("lost")], True),  # closed + meta
    ("c3", "g1", "2026-11-03", [_opp("LOST"), _opp("abandoned")], True),     # lost
    ("c4", "g1", "2026-11-04", [_opp("open")], True),                       # crm (HP lead, no calls)
    ("c5", "g1", "2026-11-05", [], False),                                  # crm (nothing, no keys)
    ("c6", "g1", "2026-11-06", [_opp("open", "s-app")], True),              # called
    ("c7", "g1", "2026-11-07", [], True),                                   # called, no opportunity
    ("c8", "g1", "2026-11-08", [_opp("open"), _opp("lost")], True),         # crm (not all lost)
    ("c9", "g2", "2026-11-09", [_opp("won")], True),                        # other client
]
EXPECTED = {
    "closed": ["c2"], "lost": ["c3"], "called": ["c7", "c6", "c1"],
    "crm": ["c8", "c5", "c4"], "meta": ["c2", "c1"],
}


async def _seed(mock_mongo_client):
    db = mock_mongo_client[DB_NAME]
    await db["client_groups"].insert_many([
        {"id": "g1", "user_id": USER, "name": "Alpha", "ghl_location_id": LOC},
        {"id": "g2", "user_id": USER, "name": "Beta", "ghl_location_id": "loc-b"},
    ])
    await db["ghl_pipelines"].insert_one({
        "user_id": USER, "location_id": LOC, "pipelines": PIPELINES, "fetched_at": datetime.now(timezone.utc),
    })
    await db["ghl_contacts"].insert_many([{
        "user_id": USER, "client_group_id": g, "client_group_name": g, "lead_type": "lead",
        "location_id": LOC if g == "g1" else "loc-b",
        **({"match_keys": [_key(cid), f"phone:{cid}"]} if keyed else {}),
        "contact_data": {"id": cid, "firstName": cid, "email": f"{cid}@x.com",
                         "dateAdded": f"{day}T10:00:00.000Z", "opportunities": opps},
    } for cid, g, day, opps, keyed in ROWS])
    await db["facebook_leads"].insert_many([
        {"user_id": USER, "client_group_id": "g1", "match_keys": [_key("c1")], "lead_data": {"ad_name": "A1"}},
        # matched on the phone key only, carrying a key no contact has
        {"user_id": USER, "client_group_id": "g1", "match_keys": ["phone:c2", "email:stranger@x.com"],
         "lead_data": {}},
        {"user_id": USER, "client_group_id": "g2", "match_keys": [_key("c9")], "lead_data": {}},
        {"user_id": "someone-else", "match_keys": [_key("c4")], "lead_data": {}},
    ])
    await db["hotprospector_leads"].insert_many([
        {"user_id": USER, "ghl_location_id": LOC, "match_keys": [_key("c1")],
         "lead_data": {"call_logs_count": 2, "call_logs": [{"call_time": "2026-11-02"}]}},
        {"user_id": USER, "ghl_location_id": LOC, "match_keys": [_key("c4")],
         "lead_data": {"call_logs_count": 0, "call_logs": []}},
        {"user_id": USER, "ghl_location_id": LOC, "match_keys": ["phone:c6"], "lead_data": {"call_logs_count": 1}},
        {"user_id": USER, "ghl_location_id": LOC, "match_keys": [_key("c7")], "lead_data": {"call_logs_count": 3}},
        # won / lost contacts that were called stay closed / lost
        {"user_id": USER, "ghl_location_id": LOC, "match_keys": [_key("c2"), _key("c3")],
         "lead_data": {"call_logs_count": 1}},
        {"user_id": "someone-else", "match_keys": [_key("c8")], "lead_data": {"call_logs_count": 5}},
    ])


def _ids(body):
    return [c["contactId"] for c in body["contacts"]]


async def test_each_stage_lists_its_contacts_and_counts_partition(api, mock_mongo_client):
    await _seed(mock_mongo_client)

    full = api.get("/api/leads/unified?groups=g1&limit=50").json()
    counts = full["meta"]["lead_stages"]
    assert list(counts) == ["all", "meta", "crm", "called", "closed", "lost"]
    assert counts == {"all": 8, "meta": 2, "crm": 3, "called": 3, "closed": 1, "lost": 1}
    assert counts["crm"] + counts["called"] + counts["closed"] + counts["lost"] == counts["all"]

    for stage, ids in EXPECTED.items():
        body = api.get(f"/api/leads/unified?groups=g1&limit=50&lead_stage={stage}").json()
        assert _ids(body) == ids, stage
        assert body["meta"]["total_contacts"] == counts[stage] == len(ids)
        assert body["meta"]["lead_stages"] == counts  # counts ignore lead_stage
        assert body["meta"]["stats"] == full["meta"]["stats"]  # stats unchanged


async def test_meta_and_called_agree_with_the_enrichment(api, mock_mongo_client):
    await _seed(mock_mongo_client)

    body = api.get("/api/leads/unified?groups=g1&limit=50").json()
    meta_ids = set(EXPECTED["meta"])
    called_ids = set(EXPECTED["called"])
    for c in body["contacts"]:
        assert c["sources"]["meta"] is (c["contactId"] in meta_ids), c["contactId"]
        if c["contactId"] in called_ids:
            assert c["hp_enrichment"]["call_logs_count"] > 0
    # meta overlaps the partition: c1 is both Meta and called, c2 Meta and closed
    assert {"c1", "c2"} <= meta_ids and "c1" in called_ids


async def test_counts_follow_date_status_and_stage(api, mock_mongo_client):
    await _seed(mock_mongo_client)

    dated = api.get("/api/leads/unified?groups=g1&start_date=2026-11-03&end_date=2026-11-06").json()
    assert dated["meta"]["lead_stages"] == {"all": 4, "meta": 0, "crm": 2, "called": 1, "closed": 0, "lost": 1}

    lost_opps = api.get("/api/leads/unified?groups=g1&opportunity_status=lost&lead_stage=crm").json()
    assert lost_opps["meta"]["lead_stages"] == {"all": 3, "meta": 1, "crm": 1, "called": 0, "closed": 1, "lost": 1}
    assert _ids(lost_opps) == ["c8"]

    staged = api.get("/api/leads/unified?groups=g1&stage_id=s-app").json()
    assert staged["meta"]["lead_stages"] == {"all": 2, "meta": 1, "crm": 0, "called": 1, "closed": 1, "lost": 0}
    both = api.get("/api/leads/unified?groups=g1&stage_id=s-app&lead_stage=called").json()
    assert _ids(both) == ["c6"] and both["meta"]["total_contacts"] == 1


async def test_pipeline_counts_follow_lead_stage_and_ignore_stage_id(api, mock_mongo_client):
    await _seed(mock_mongo_client)

    def stages(body):
        return {s["id"]: s["count"] for p in body["meta"]["pipelines"] for s in p["stages"]}

    assert stages(api.get("/api/leads/unified?groups=g1").json()) == {"s-opt": 5, "s-app": 2}
    closed = api.get("/api/leads/unified?groups=g1&lead_stage=closed&stage_id=s-opt").json()
    assert stages(closed) == {"s-opt": 1, "s-app": 1}
    assert _ids(closed) == ["c2"]


async def test_paging_under_lead_stage(api, mock_mongo_client):
    await _seed(mock_mongo_client)

    p1 = api.get("/api/leads/unified?groups=g1&lead_stage=called&limit=2&page=1").json()
    p2 = api.get("/api/leads/unified?groups=g1&lead_stage=called&limit=2&page=2").json()

    assert p1["meta"]["total_contacts"] == 3
    assert p1["meta"]["total_pages"] == 2 and p1["meta"]["has_next"] is True
    assert _ids(p1) == ["c7", "c6"]
    assert _ids(p2) == ["c1"] and p2["meta"]["has_next"] is False


async def test_multi_group_and_no_group_unchanged(api, mock_mongo_client):
    await _seed(mock_mongo_client)

    body = api.get("/api/leads/unified?groups=g1,g2&lead_stage=closed&limit=50").json()
    assert "lead_stages" not in body["meta"]
    assert body["meta"]["total_contacts"] == 9  # lead_stage ignored

    none = api.get("/api/leads/unified?lead_stage=closed").json()
    assert "lead_stages" not in none["meta"] and none["meta"]["total_contacts"] == 9


async def test_unknown_lead_stage_is_ignored(api, mock_mongo_client):
    await _seed(mock_mongo_client)

    for value in ("won", "all", ""):
        body = api.get(f"/api/leads/unified?groups=g1&lead_stage={value}").json()
        assert body["meta"]["total_contacts"] == 8, value
        assert body["meta"]["lead_stages"]["all"] == 8


@pytest.mark.parametrize("broken", ["_lead_stage_conditions", "_lead_stage_counts"])
async def test_failure_leaves_the_response_as_before(api, mock_mongo_client, monkeypatch, broken):
    await _seed(mock_mongo_client)
    before = api.get("/api/leads/unified?groups=g1&limit=50").json()

    async def fail(*a, **k):
        raise RuntimeError("mongo hiccup")

    monkeypatch.setattr(cg_router, broken, fail)
    resp = api.get("/api/leads/unified?groups=g1&limit=50&lead_stage=closed")

    assert resp.status_code == 200
    body = resp.json()
    assert "lead_stages" not in body["meta"]
    assert body["meta"]["total_contacts"] == 8  # lead_stage ignored
    assert _ids(body) == _ids(before)
    assert body["meta"]["pipelines"] == before["meta"]["pipelines"]


async def test_key_lookup_failure_inside_distinct(api, mock_mongo_client, monkeypatch):
    await _seed(mock_mongo_client)
    hp = mock_mongo_client[DB_NAME]["hotprospector_leads"]
    real_distinct = type(hp).distinct

    async def flaky(self, key, filter=None, *a, **k):
        if self.name == "hotprospector_leads" and key == "match_keys":
            raise RuntimeError("timeout")
        return await real_distinct(self, key, filter, *a, **k)

    monkeypatch.setattr(type(hp), "distinct", flaky)
    body = api.get("/api/leads/unified?groups=g1&lead_stage=called").json()
    assert "lead_stages" not in body["meta"] and body["meta"]["total_contacts"] == 8


async def test_too_many_keys_skips_lead_stages(api, mock_mongo_client, monkeypatch):
    await _seed(mock_mongo_client)
    monkeypatch.setattr(cg_router, "LEAD_STAGE_MAX_KEYS", 3)

    body = api.get("/api/leads/unified?groups=g1&lead_stage=closed").json()
    assert "lead_stages" not in body["meta"] and body["meta"]["total_contacts"] == 8
