import json

import pytest

from services.meta_campaign_scope import (
    ScopeConflict,
    campaign_filter,
    scope_summary,
    siblings,
    validate_selection,
)


def test_filter_none_means_no_param():
    assert campaign_filter(None) is None


def test_filter_builds_in_clause():
    assert json.loads(campaign_filter(["1", "2"], field="id")) == [
        {"field": "id", "operator": "IN", "value": ["1", "2"]}]


def test_filter_default_field():
    assert json.loads(campaign_filter(["1"]))[0]["field"] == "campaign.id"


def test_shared_account_requires_list():
    others = [{"id": "g2", "name": "Plush Face", "meta_campaign_ids": ["c2"]}]
    with pytest.raises(ScopeConflict):
        validate_selection("g1", None, others, {})


def test_null_sibling_is_not_a_violation():
    others = [{"id": "g2", "name": "Plush Body", "meta_campaign_ids": None}]
    validate_selection("g1", ["c1"], others, {})


def test_double_claim_rejected_and_named():
    others = [{"id": "g2", "name": "Plush Face", "meta_campaign_ids": ["c2"]}]
    with pytest.raises(ScopeConflict) as e:
        validate_selection("g1", ["c1", "c2"], others, {"c2": "Face Sculpt"})
    assert "Plush Face" in e.value.message and "Face Sculpt" in e.value.message
    assert str(e.value) == e.value.message


def test_double_claim_falls_back_to_id():
    others = [{"id": "g2", "meta_campaign_ids": ["c2"]}]
    with pytest.raises(ScopeConflict) as e:
        validate_selection("g1", ["c2"], others, {})
    assert "c2" in e.value.message and "g2" in e.value.message


def test_unshared_null_is_valid():
    validate_selection("g1", None, [], {})


def test_summary_counts_unassigned_active_and_paused_only():
    campaigns = [{"id": "c1", "name": "Body", "effective_status": "ACTIVE"},
                 {"id": "c2", "name": "Face", "effective_status": "ACTIVE"},
                 {"id": "c3", "name": "New", "effective_status": "PAUSED"},
                 {"id": "c4", "name": "Old", "effective_status": "ARCHIVED"}]
    g1 = {"meta_ad_account_id": "act_1", "meta_campaign_ids": ["c1"], "meta_account_campaigns": campaigns}
    g2 = {"meta_ad_account_id": "act_1", "meta_campaign_ids": ["c2"]}
    s = scope_summary(g1, [g2])
    assert s == {"mode": "selected", "selected": 1, "total": 4, "shared": True,
                 "unassigned": [{"id": "c3", "name": "New"}]}


def test_summary_all_mode_has_no_unassigned():
    g = {"meta_ad_account_id": "act_1", "meta_campaign_ids": None,
         "meta_account_campaigns": [{"id": "c1", "name": "x", "effective_status": "ACTIVE"}]}
    s = scope_summary(g, [])
    assert s["unassigned"] == []
    assert s["mode"] == "all" and s["selected"] == 1 and s["shared"] is False


def test_summary_none_without_meta_account():
    assert scope_summary({"meta_campaign_ids": None}, []) is None


def test_summary_missing_campaigns_treated_empty():
    s = scope_summary({"meta_ad_account_id": "act_1", "meta_campaign_ids": ["c1"]}, [])
    assert s["total"] == 0 and s["unassigned"] == []


async def test_siblings_excludes_self_and_other_users(mock_db):
    await mock_db.client_groups.insert_many([
        {"id": "g1", "user_id": "A", "meta_ad_account_id": "act_1", "name": "One"},
        {"id": "g2", "user_id": "A", "meta_ad_account_id": "act_1", "name": "Two",
         "meta_campaign_ids": ["c2"], "created_at": "t", "secret": "x"},
        {"id": "g3", "user_id": "B", "meta_ad_account_id": "act_1", "name": "Three"},
        {"id": "g4", "user_id": "A", "meta_ad_account_id": "act_2", "name": "Four"},
    ])
    out = await siblings(mock_db, "A", "act_1", exclude_group_id="g1")
    assert [g["id"] for g in out] == ["g2"]
    assert "_id" not in out[0] and "secret" not in out[0]
    assert out[0]["created_at"] == "t"
    both = await siblings(mock_db, "A", "act_1")
    assert sorted(g["id"] for g in both) == ["g1", "g2"]
