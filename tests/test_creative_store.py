from services.creatives.store import (
    ARCHIVED,
    add_test_ads,
    create_creative_indexes,
    create_test,
    get_test,
    list_test_ads,
    list_tests,
    next_code,
    update_test,
)


async def test_next_code_is_sequential_per_user(mock_db):
    assert await next_code(mock_db, "u1") == "CT-0001"
    assert await next_code(mock_db, "u1") == "CT-0002"
    assert await next_code(mock_db, "u2") == "CT-0001"


async def test_create_test_fills_name_and_defaults(mock_db):
    t = await create_test(
        mock_db, "u1", name="Mortgage myth #3", origin="new",
        creative={"format": "video"},
        targets={"mode": "cpl_only", "metrics": {"cpl": {"op": "lte", "value": 35}}},
    )
    assert t["status"] == "draft" and t["code"] == "CT-0001"
    assert t["meta_ad_name"] == "CT-0001 | mortgage-myth-3"
    assert t["targets"]["min_spend"] == 150 and t["targets"]["win_share"] == 0.6
    assert t["targets"]["mode"] == "cpl_only"
    assert "_id" not in t or t["_id"] == t["id"]


async def test_owner_scoping_and_update_guards(mock_db):
    t = await create_test(mock_db, "u1", name="x", origin="new", creative={}, targets={})
    assert await get_test(mock_db, "u2", t["id"]) is None
    assert await update_test(mock_db, "u2", t["id"], {"name": "hax"}) is None
    out = await update_test(mock_db, "u1", t["id"], {"name": "y", "code": "CT-9999"})
    assert out["name"] == "y" and out["code"] == "CT-0001"


async def test_list_excludes_archived_by_default(mock_db):
    a = await create_test(mock_db, "u1", name="a", origin="new", creative={}, targets={})
    await create_test(mock_db, "u1", name="b", origin="new", creative={}, targets={})
    await update_test(mock_db, "u1", a["id"], {"status": ARCHIVED})
    assert len(await list_tests(mock_db, "u1")) == 1
    archived = await list_tests(mock_db, "u1", status="archived")
    assert [t["id"] for t in archived] == [a["id"]]


async def test_list_filters_by_ad_account(mock_db):
    a = await create_test(mock_db, "u1", name="a", origin="new", creative={}, targets={})
    b = await create_test(mock_db, "u1", name="b", origin="new", creative={}, targets={})
    await add_test_ads(mock_db, "u1", a["id"], [{"ad_account_id": "act_1", "client_group_id": "g"}])
    await add_test_ads(mock_db, "u1", b["id"], [{"ad_account_id": "act_2", "client_group_id": "g"}])
    out = await list_tests(mock_db, "u1", ad_account_ids=["act_2"])
    assert [t["id"] for t in out] == [b["id"]]


async def test_add_test_ads_defaults(mock_db):
    t = await create_test(mock_db, "u1", name="a", origin="new", creative={}, targets={})
    rows = await add_test_ads(
        mock_db, "u1", t["id"], [{"ad_account_id": "act_1", "client_group_id": "g1"}]
    )
    assert len(rows) == 1
    assert rows[0]["post_state"] == "pending"
    assert rows[0]["score"] == {"state": "scoring"}
    assert rows[0]["test_id"] == t["id"] and rows[0]["user_id"] == "u1"
    assert len(await list_test_ads(mock_db, t["id"])) == 1


async def test_create_indexes_is_idempotent(mock_mongo_client):
    await create_creative_indexes(mock_mongo_client)
    await create_creative_indexes(mock_mongo_client)
