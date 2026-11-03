import pytest

from ai.orchestrator import _PAGE_TOOLS
from ai.tools import creative_tools as t
from ai.tools.registry import ToolRegistry
from services.creatives import reads, store

U = "u1@example.com"
OTHER = "u2@example.com"


async def group(db, gid, name, act, user=U, ads=None, **extra):
    cache = {"last_30d": {"ads": ads or []}}
    await db["client_groups"].insert_one({
        "id": gid, "user_id": user, "name": name, "meta_ad_account_id": act,
        "ad_account_currency": "USD", "facebook_cache": cache, **extra})


def test_registers_five_read_tools():
    reg = ToolRegistry()
    t.register_creative_tools(reg)
    for name in t.CREATIVE_READ_TOOLS:
        assert name in reg.get_tool_names()


@pytest.mark.parametrize("page", ["campaigns", "dashboard"])
def test_read_tools_on_pages(page):
    assert set(t.CREATIVE_READ_TOOLS) <= set(_PAGE_TOOLS[page])


async def test_find_ads_owner_scoped_and_compact(mock_db):
    ad = {"id": "a1", "name": "Summer Hook", "spend": 100, "total_leads": 4,
          "creative_thumbnail": "thumb", "creative_image": "big"}
    await group(mock_db, "g1", "Acme", "act_1", ads=[ad])
    await group(mock_db, "g2", "Foreign", "act_9", user=OTHER, ads=[{**ad, "id": "a9"}])
    out = await t.find_ads(mock_db, U, q="summer")
    assert [a["ad_id"] for a in out["ads"]] == ["a1"]
    assert out["ads"][0]["thumbnail"] == "thumb" and "image" not in out["ads"][0]
    assert out["ads"][0]["metrics"]["cpl"] == 25
    assert (await t.find_ads(mock_db, U, q="", ad_account_id="act_9"))["error"]


async def test_list_ad_accounts_owner_scoped(mock_db):
    await group(mock_db, "g1", "Acme", "act_1")
    await group(mock_db, "g2", "Foreign", "act_9", user=OTHER)
    out = await t.list_ad_accounts(mock_db, U)
    assert [a["ad_account_id"] for a in out["ad_accounts"]] == ["act_1"]


async def test_list_adsets_flags_compat_and_rejects_foreign(mock_db, monkeypatch):
    await group(mock_db, "g1", "Acme", "act_1")
    await group(mock_db, "g2", "Foreign", "act_9", user=OTHER)

    async def token(user_id, _c):
        return {"access_token": "tok"}

    async def adsets(act, tok):
        return [{"id": "s1", "name": "A", "destination_type": "ON_AD",
                 "campaign": {"id": "c", "name": "C", "objective": "OUTCOME_LEADS"}},
                {"id": "s2", "name": "B", "campaign": {"id": "c", "objective": "OUTCOME_TRAFFIC"}}]

    monkeypatch.setattr(reads, "get_facebook_token", token)
    monkeypatch.setattr(reads, "list_adsets", adsets)
    out = await t.list_adsets(mock_db, U, None, ad_account_id="act_1", destination="lead_form")
    assert [(a["id"], a["compatible"]) for a in out["adsets"]] == [("s1", True), ("s2", False)]
    assert "error" in await t.list_adsets(mock_db, U, None, ad_account_id="act_9", destination="lead_form")
    assert "error" in await t.list_adsets(mock_db, U, None, ad_account_id="act_1", destination="x")


async def make_test(db, user, **kw):
    return await store.create_test(db, user, name="Hook", origin="new",
                                   creative={"format": "image", "media": {"image_url": "u"}},
                                   targets={}, **kw)


async def test_creative_tests_owner_scoped_and_resolve_code(mock_db):
    mine = await make_test(mock_db, U)
    theirs = await make_test(mock_db, OTHER)
    assert mine["code"] == theirs["code"] == "CT-0001"
    rows = (await t.list_creative_tests(mock_db, U))["tests"]
    assert [r["id"] for r in rows] == [mine["id"]]
    by_code = await t.get_creative_test(mock_db, U, code_or_id="CT-0001")
    assert by_code["id"] == mine["id"] and by_code["thumbnail"] == "u"
    assert (await t.get_creative_test(mock_db, U, code_or_id="ct-0001"))["id"] == mine["id"]
    assert (await t.get_creative_test(mock_db, U, code_or_id=mine["id"]))["code"] == "CT-0001"
    assert "error" in await t.get_creative_test(mock_db, U, code_or_id=theirs["id"])
    assert "error" in await t.get_creative_test(mock_db, U, code_or_id="CT-0099")


async def test_get_creative_test_lists_ads(mock_db):
    mine = await make_test(mock_db, U)
    await store.add_test_ads(mock_db, U, mine["id"], [{"ad_account_id": "act_1", "adset_name": "A",
                                                        "meta": {"ad_id": "m1"}}])
    out = await t.get_creative_test(mock_db, U, code_or_id="CT-0001")
    assert out["ads"][0]["ad_id"] == "m1" and "'_id'" not in str(out)
