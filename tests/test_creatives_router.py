import contextlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import routers.creatives as creatives_router
from core.database import DB_NAME
from dependencies import get_current_user
from main import app
from services.creatives import store

U = "u1@example.com"
OTHER = "u2@example.com"


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(creatives_router, "get_mongo_client", fake_client)
    state = {"user": U}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


BODY = {"name": "Summer Hook!", "origin": "new", "creative": {"kind": "image"}}


def make(api, **over):
    r = api.post("/api/creatives", json={**BODY, **over})
    assert r.status_code == 200, r.text
    return r.json()


def test_create_list_get_round_trip(api):
    t = make(api)
    assert t["status"] == "draft" and t["code"] == "CT-0001"
    assert t["meta_ad_name"] == "CT-0001 | summer-hook" and "_id" not in t
    items = api.get("/api/creatives").json()["items"]
    assert [i["id"] for i in items] == [t["id"]] and items[0]["ads"] == []
    got = api.get(f"/api/creatives/{t['id']}").json()
    assert got["id"] == t["id"] and got["ads"] == [] and got["days"] == {}


def test_other_user_gets_404(api):
    t = make(api)
    api.state["user"] = OTHER
    for r in (
        api.get(f"/api/creatives/{t['id']}"),
        api.patch(f"/api/creatives/{t['id']}", json={"name": "x"}),
        api.post(f"/api/creatives/{t['id']}/archive"),
        api.post(f"/api/creatives/{t['id']}/refresh"),
        api.post(f"/api/creatives/{t['id']}/adopt", json={"ad_id": "a"}),
    ):
        assert r.status_code == 404
    assert api.get("/api/creatives").json()["items"] == []


def test_patch_slug_draft_recomputes_then_409(api, mock_db):
    t = make(api)
    r = api.patch(f"/api/creatives/{t['id']}", json={"slug": "New Slug!!"})
    assert r.status_code == 200
    assert r.json()["slug"] == "new-slug" and r.json()["meta_ad_name"] == "CT-0001 | new-slug"
    r = api.patch(f"/api/creatives/{t['id']}", json={"slug": "!!!"})
    assert r.json()["slug"] == "untitled"
    import asyncio
    t2 = make(api)
    asyncio.run(mock_db[store.TESTS].update_one({"id": t2["id"]}, {"$set": {"status": "testing"}}))
    assert api.patch(f"/api/creatives/{t2['id']}", json={"slug": "other"}).status_code == 409
    assert api.patch(f"/api/creatives/{t2['id']}", json={"name": "renamed"}).json()["name"] == "renamed"


def test_patch_creative_is_read_only_once_posted(api, mock_db):
    import asyncio
    t = make(api)
    assert api.patch(f"/api/creatives/{t['id']}", json={"creative": {"primary_text": "x"}}).status_code == 200
    asyncio.run(mock_db[store.TESTS].update_one({"id": t["id"]}, {"$set": {"status": "testing"}}))
    r = api.patch(f"/api/creatives/{t['id']}", json={"creative": {"primary_text": "y"}})
    assert r.status_code == 409
    assert api.patch(f"/api/creatives/{t['id']}", json={"name": "ok"}).status_code == 200


def test_patch_source_and_origin_draft_only(api, mock_db):
    import asyncio
    t = make(api)
    src = {"ad_id": "a1", "ad_account_id": "act_1", "name": "Ad one"}
    r = api.patch(f"/api/creatives/{t['id']}", json={"origin": "revive", "source": src})
    assert r.status_code == 200 and r.json()["origin"] == "revive" and r.json()["source"] == src
    asyncio.run(mock_db[store.TESTS].update_one({"id": t["id"]}, {"$set": {"status": "testing"}}))
    assert api.patch(f"/api/creatives/{t['id']}", json={"source": src}).status_code == 409
    assert api.patch(f"/api/creatives/{t['id']}", json={"origin": "new"}).status_code == 409
    assert api.patch(f"/api/creatives/{t['id']}", json={"name": "ok"}).status_code == 200


def test_archive_hides_from_default_list(api):
    t = make(api)
    r = api.post(f"/api/creatives/{t['id']}/archive")
    assert r.json()["status"] == "archived" and r.json()["archived_at"]
    assert api.get("/api/creatives").json()["items"] == []
    assert len(api.get("/api/creatives?status=archived").json()["items"]) == 1


def test_invalid_mode_and_origin_422(api):
    r = api.post("/api/creatives", json={**BODY, "targets": {"mode": "bogus"}})
    assert r.status_code == 422
    assert api.post("/api/creatives", json={**BODY, "origin": "other"}).status_code == 422
    t = make(api)
    assert api.patch(f"/api/creatives/{t['id']}", json={"targets": {"mode": "bogus"}}).status_code == 422


def _seed_group(db, gid="g1", acct="act_1", user=U, ad_name="CT-0001 | x", ad_id="ad1"):
    import asyncio
    asyncio.run(db["client_groups"].insert_one({
        "id": gid, "user_id": user, "name": gid, "meta_ad_account_id": acct,
        "facebook_cache": {"entities": {"ads": [
            {"id": ad_id, "name": ad_name, "adset_id": "as1", "campaign_id": "c1"}]}},
    }))


def test_adopt_sets_testing_and_conflicts(api, mock_db):
    t = make(api)
    _seed_group(mock_db)
    assert api.get("/api/creatives/adoptable").json()[0]["ad_id"] == "ad1"
    r = api.post(f"/api/creatives/{t['id']}/adopt", json={"ad_id": "ad1"})
    assert r.status_code == 200 and r.json()["meta"]["ad_id"] == "ad1"
    got = api.get(f"/api/creatives/{t['id']}").json()
    assert got["status"] == "testing" and got["started_at"]
    assert len(got["ads"]) == 1
    r = api.post(f"/api/creatives/{t['id']}/adopt", json={"ad_id": "ad1"})
    assert r.status_code == 409 and "already tracked" in r.json()["detail"]


def test_client_group_filter(api, mock_db):
    import asyncio
    a, b = make(api), make(api, name="two")
    _seed_group(mock_db)
    asyncio.run(store.add_test_ads(mock_db, U, a["id"], [{"ad_account_id": "act_1"}]))
    asyncio.run(store.add_test_ads(mock_db, U, b["id"], [{"ad_account_id": "act_2"}]))
    ids = [i["id"] for i in api.get("/api/creatives?client_group_id=g1").json()["items"]]
    assert ids == [a["id"]]
    assert api.get("/api/creatives?client_group_id=nope").json()["items"] == []


def test_static_paths_not_captured_and_prefill(api, mock_db):
    import asyncio
    asyncio.run(mock_db["client_groups"].insert_one(
        {"id": "g1", "user_id": U, "name": "g", "targets": {"cpl": 12}}))
    r = api.get("/api/creatives/targets/prefill?client_group_ids=g1")
    assert r.status_code == 200
    assert r.json()["per_account"] == {"g1": {"cpl": 12}}
    assert api.get("/api/creatives/adoptable").json() == []


def test_refresh_calls_track_test(api, mock_db, monkeypatch):
    import asyncio
    t = make(api)
    asyncio.run(mock_db[store.TESTS].update_one({"id": t["id"]}, {"$set": {"status": "testing"}}))
    seen = {}

    async def fake_track(db, mongo_client, test, *, today):
        seen["id"] = test["id"]
        return {"ok": True, "when": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    monkeypatch.setattr(creatives_router, "track_test", fake_track)
    r = api.post(f"/api/creatives/{t['id']}/refresh")
    assert r.status_code == 200 and r.json()["ok"] is True and seen["id"] == t["id"]


def test_upload_grant_endpoint(api, monkeypatch):
    monkeypatch.setenv("CREATIVE_UPLOAD_SECRET", "s")
    r = api.post("/api/creatives/upload-grant", json={"mime": "image/png", "bytes": 1000})
    assert r.status_code == 200 and "." in r.json()["grant"]
    bad = api.post("/api/creatives/upload-grant", json={"mime": "image/gif", "bytes": 1000})
    assert bad.status_code == 400


def test_archive_closes_open_creative_suggestions(api, mock_db):
    import asyncio
    from ai.suggestions import store as sug_store

    t = make(api)
    for sid, owner, test_id, rule in (("s1", U, t["id"], "creative_loser"),
                                      ("s2", U, t["id"], "creative_adopt"),
                                      ("s3", U, "other", "creative_loser"),
                                      ("s4", "u2", t["id"], "creative_loser")):
        asyncio.run(mock_db[sug_store.SUGGESTIONS].insert_one({
            "_id": sid, "id": sid, "user_id": owner, "agent": "creative_test_judge",
            "origin_window": "weekly", "status": "open", "rule_id": rule,
            "evidence": {"creative_test_id": test_id}}))
    assert api.post(f"/api/creatives/{t['id']}/archive").status_code == 200
    status = {d["_id"]: d["status"] for d in asyncio.run(
        mock_db[sug_store.SUGGESTIONS].find({}).to_list(None))}
    assert status == {"s1": "resolved", "s2": "resolved", "s3": "open", "s4": "open"}
