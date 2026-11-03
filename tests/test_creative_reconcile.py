import pytest

from services.creatives import reconcile
from services.creatives.store import TEST_ADS, add_test_ads, create_test, list_test_ads


async def mk(db, uid="u1"):
    return await create_test(db, uid, name="x", origin="new", creative={"format": "video"}, targets={})


async def group(db, gid, acct, ads, uid="u1"):
    await db["client_groups"].insert_one({
        "id": gid, "user_id": uid, "meta_ad_account_id": acct,
        "facebook_cache": {"entities": {"ads": ads}}})


def ad(i, name, adset="as1"):
    return {"id": i, "name": name, "campaign_id": "c1", "adset_id": adset, "status": "ACTIVE"}


async def test_adoptable_found_across_two_groups(mock_db):
    t = await mk(mock_db)
    await group(mock_db, "g1", "act_1", [ad("a1", f"{t['code']} | hook"), ad("a2", "other ad")])
    await group(mock_db, "g2", "act_2", [ad("a3", f"{t['code']} | hook b", "as9")])
    found = await reconcile.find_adoptable(mock_db, "u1")
    assert {(f["ad_id"], f["ad_account_id"], f["client_group_id"], f["adset_id"]) for f in found} == {
        ("a1", "act_1", "g1", "as1"), ("a3", "act_2", "g2", "as9")}
    assert all(f["test_id"] == t["id"] and f["code"] == t["code"] for f in found)


async def test_already_tracked_ad_excluded(mock_db):
    t = await mk(mock_db)
    await group(mock_db, "g1", "act_1", [ad("a1", f"{t['code']} | hook")])
    await add_test_ads(mock_db, "u1", t["id"], [{"ad_account_id": "act_1", "meta": {"ad_id": "a1"}}])
    assert await reconcile.find_adoptable(mock_db, "u1") == []


async def test_other_users_code_excluded(mock_db):
    mine = await mk(mock_db, "u1")
    other = await mk(mock_db, "u2")
    assert mine["code"] == other["code"] == "CT-0001"
    await group(mock_db, "g1", "act_1", [ad("a1", f"{other['code']} | hook")])
    await add_test_ads(mock_db, "u2", other["id"], [{"ad_account_id": "act_1", "meta": {"ad_id": "a1"}}])
    # u1's own CT-0001 matches by name, but u2 already tracks a1 (shared ad account)
    assert await reconcile.find_adoptable(mock_db, "u1") == []


async def test_adopt_inserts_active_adopted_row(mock_db):
    t = await mk(mock_db)
    await group(mock_db, "g1", "act_1", [ad("a1", f"{t['code']} | hook")])
    row = await reconcile.adopt(mock_db, "u1", t["id"], "a1")
    assert row["post_state"] == "active" and row["origin"] == "adopted"
    assert (await list_test_ads(mock_db, t["id"]))[0]["meta"]["ad_id"] == "a1"
    assert await reconcile.find_adoptable(mock_db, "u1") == []


async def test_adopt_mismatched_code_raises(mock_db):
    t1, t2 = await mk(mock_db), await mk(mock_db)
    await group(mock_db, "g1", "act_1", [ad("a1", f"{t1['code']} | hook")])
    with pytest.raises(ValueError):
        await reconcile.adopt(mock_db, "u1", t2["id"], "a1")
    assert await mock_db[TEST_ADS].count_documents({}) == 0


async def test_reconcile_all_counts(mock_db):
    t = await mk(mock_db)
    await group(mock_db, "g1", "act_1", [ad("a1", f"{t['code']} | hook")])
    out = await reconcile.reconcile_all(mock_db)
    assert out["adoptable"] == 1 and out["by_user"] == {"u1": 1}
