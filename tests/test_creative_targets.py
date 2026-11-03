from services.creatives.targets import owning_group, prefill_targets, resolve_account_targets


async def _seed(mock_db, cpls, default=None):
    await mock_db["users"].insert_one({"user_id": "u1", **({"default_targets": {"cpl": default}} if default else {})})
    for gid, cpl in cpls.items():
        doc = {"id": gid, "user_id": "u1", "name": gid}
        if cpl is not None:
            doc["targets"] = {"cpl": cpl}
        await mock_db["client_groups"].insert_one(doc)
    # another owner's group must never leak in
    await mock_db["client_groups"].insert_one({"id": "x1", "user_id": "u2", "targets": {"cpl": 99}})


async def test_prefill_uses_client_then_agency(mock_db):
    await _seed(mock_db, {"g1": 35, "g2": 35, "g3": 42}, default=30)
    r = await prefill_targets(mock_db, "u1", ["g1", "g2", "g3", "x1"])
    assert r["metrics"]["cpl"] == {"op": "lte", "value": 35, "source": "client"}
    assert r["per_account"] == {"g3": {"cpl": 42}, "g1": {"cpl": 35}, "g2": {"cpl": 35}}


async def test_prefill_falls_back_to_agency_then_none(mock_db):
    await _seed(mock_db, {"g1": None, "g2": None}, default=30)
    r = await prefill_targets(mock_db, "u1", ["g1", "g2"])
    assert r["metrics"]["cpl"] == {"op": "lte", "value": 30, "source": "agency"}
    assert r["per_account"] == {}


async def test_prefill_no_targets_anywhere(mock_db):
    await _seed(mock_db, {"g1": None})
    r = await prefill_targets(mock_db, "u1", ["g1"])
    assert r["metrics"]["cpl"] == {"op": "lte", "value": None, "source": None}


def test_owning_group_for_shared_account():  # Review Focus 4
    gs = [{"id": "a", "meta_campaign_ids": ["c1"]}, {"id": "b", "meta_campaign_ids": ["c2"]}]
    assert owning_group(gs, "c2")["id"] == "b"
    assert owning_group(gs, "zzz")["id"] == "a"


def test_owning_group_prefers_unscoped_group_as_fallback():
    gs = [{"id": "a", "meta_campaign_ids": ["c1"]}, {"id": "b", "meta_campaign_ids": None}, {"id": "c"}]
    assert owning_group(gs, "zzz")["id"] == "b"
    assert owning_group(gs, None)["id"] == "b"


def test_resolve_precedence():
    t = {"mode": "thresholds", "metrics": {"cpl": {"op": "lte", "value": 35}}}
    assert resolve_account_targets(t, {}, 42)["metrics"]["cpl"]["value"] == 42
    assert resolve_account_targets(t, {"targets_override": {"cpl": 50}}, 42)["metrics"]["cpl"]["value"] == 50
    assert resolve_account_targets(t, {}, None)["metrics"]["cpl"]["value"] == 35
    assert t["metrics"]["cpl"]["value"] == 35  # input not mutated
