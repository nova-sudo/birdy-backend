import contextlib

import pytest
from fastapi.testclient import TestClient

import routers.creatives as creatives_router
from dependencies import get_current_user
from integrations.facebook_utils import meta_creative_reader as reader
from main import app

U = "u1@example.com"


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def fake_token(user_id, _client):
        return {"access_token": "tok"}

    monkeypatch.setattr(creatives_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(creatives_router, "get_facebook_token", fake_token)
    app.dependency_overrides[get_current_user] = lambda: U
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


async def grp(db, gid, name, acct, user=U, **extra):
    await db["client_groups"].insert_one(
        {"id": gid, "user_id": user, "name": name, "meta_ad_account_id": acct, **extra})


def test_shared_account_listed_once_with_both_clients(api, mock_db):
    import asyncio
    asyncio.run(grp(mock_db, "g1", "Acme", "act_1", ad_account_currency="GBP"))
    asyncio.run(grp(mock_db, "g2", "Bolt", "act_1"))
    asyncio.run(grp(mock_db, "g3", "Cleo", "act_2", ad_account_currency="USD"))
    asyncio.run(grp(mock_db, "g4", "Other", "act_9", user="u2@example.com"))
    rows = api.get("/api/creatives/ad-accounts").json()
    by = {r["ad_account_id"]: r for r in rows}
    assert set(by) == {"act_1", "act_2"}
    assert by["act_1"]["currency"] == "GBP"
    assert {c["name"] for c in by["act_1"]["clients"]} == {"Acme", "Bolt"}


def test_foreign_account_is_404(api, mock_db):
    import asyncio
    asyncio.run(grp(mock_db, "g4", "Other", "act_9", user="u2@example.com"))
    for path in ("/adsets?destination=website", "/pages"):
        assert api.get(f"/api/creatives/ad-accounts/act_9{path}").status_code == 404


@pytest.mark.parametrize("objective,dest,ok", [
    ("OUTCOME_LEADS", "lead_form", True), ("OUTCOME_TRAFFIC", "lead_form", False),
    ("OUTCOME_TRAFFIC", "website", True), ("OUTCOME_AWARENESS", "website", False),
    ("OUTCOME_ENGAGEMENT", "whatsapp", True), ("OUTCOME_SALES", "whatsapp", True),
    ("OUTCOME_LEADS", "whatsapp", False),
])
def test_adset_compatible(objective, dest, ok):
    good, reason = reader.adset_compatible({"campaign": {"objective": objective}}, dest)
    assert good is ok and (reason is None) == ok


def test_adsets_route_flags_compatibility_and_owner(api, mock_db, monkeypatch):
    import asyncio
    asyncio.run(grp(mock_db, "g1", "Acme", "act_1", meta_campaign_ids=["c1"]))
    asyncio.run(grp(mock_db, "g2", "Bolt", "act_1", meta_campaign_ids=["c2"]))

    async def fake_adsets(act, token):
        assert (act, token) == ("act_1", "tok")
        return [{"id": "as1", "name": "A", "campaign": {"id": "c2", "objective": "OUTCOME_LEADS"}},
                {"id": "as2", "name": "B", "campaign": {"id": "c1", "objective": "OUTCOME_TRAFFIC"}}]

    monkeypatch.setattr(creatives_router, "list_adsets", fake_adsets)
    rows = {r["id"]: r for r in api.get(
        "/api/creatives/ad-accounts/act_1/adsets?destination=lead_form").json()}
    assert rows["as1"]["compatible"] is True and rows["as1"]["owning_client_group_id"] == "g2"
    assert rows["as2"]["compatible"] is False and rows["as2"]["reason"]
    assert rows["as2"]["owning_client_group_id"] == "g1"


def test_pages_route(api, mock_db, monkeypatch):
    import asyncio
    asyncio.run(grp(mock_db, "g1", "Acme", "act_1"))

    async def fake_pages(act, token):
        return [{"id": "p1", "name": "Page"}]

    monkeypatch.setattr(creatives_router, "list_promote_pages", fake_pages)
    assert api.get("/api/creatives/ad-accounts/act_1/pages").json() == [{"id": "p1", "name": "Page"}]


def test_lead_forms_requires_page_of_users_account(api, mock_db, monkeypatch):
    import asyncio
    asyncio.run(grp(mock_db, "g1", "Acme", "act_1"))

    async def fake_pages(act, token):
        return [{"id": "p1", "name": "Page"}]

    async def fake_forms(page_id, token):
        return [{"id": "f1", "name": "Form", "status": "ACTIVE"}]

    monkeypatch.setattr(creatives_router, "list_promote_pages", fake_pages)
    monkeypatch.setattr(creatives_router, "list_lead_forms", fake_forms)
    assert api.get("/api/creatives/pages/p1/lead-forms").json()[0]["id"] == "f1"
    assert api.get("/api/creatives/pages/p_other/lead-forms").status_code == 404


def _ad(i, name, spend, **kw):
    return {"id": i, "name": name, "status": "ACTIVE", "creative_thumbnail": f"t{i}",
            "creative_image": f"img{i}", "creative_video_id": f"v{i}",
            "creative_title": "T", "creative_body": "B", **kw}


def test_search_ranks_exact_code_first(api, mock_db):
    import asyncio
    ads = [_ad("a1", "Summer CT-0042 promo", 0), _ad("a2", "CT-0042 | hook", 0),
           _ad("a3", "CT-0042 extra | hook", 0), _ad("a4", "unrelated", 0)]
    metrics = {"a1": {"spend": 900, "results": 9}, "a2": {"spend": 10, "total_leads": 2},
               "a3": {"spend": 500}, "a4": {"spend": 5}}
    asyncio.run(grp(mock_db, "g1", "Acme", "act_1", facebook_cache={
        "entities": {"ads": ads},
        "presets": {"last_30d": {"ads": metrics}, "last_7d": {"ads": {"a2": {"spend": 1}}}}}))
    asyncio.run(grp(mock_db, "g2", "Foreign", "act_9", user="u2@example.com", facebook_cache={
        "entities": {"ads": [_ad("x", "CT-0042 | leak", 0)]}, "presets": {}}))
    rows = api.get("/api/creatives/ads/search?q=ct-0042").json()
    assert [r["ad_id"] for r in rows] == ["a2", "a3", "a1"]
    top = rows[0]
    assert top["client_name"] == "Acme" and top["ad_account_id"] == "act_1"
    assert top["thumbnail"] == "ta2" and top["image"] == "imga2" and top["video_id"] == "va2"
    assert top["title"] == "T" and top["body"] == "B"
    assert top["metrics"] == {"spend": 10.0, "leads": 2.0, "cpl": 5.0}
    assert rows[2]["metrics"]["cpl"] == 100.0
    assert api.get("/api/creatives/ads/search?q=ct-0042&ad_account_id=act_9").json() == []
    assert len(api.get("/api/creatives/ads/search?q=ct&limit=1").json()) == 1
