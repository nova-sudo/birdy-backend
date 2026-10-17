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


# ── Task 6: apply_selection, auto-split, lead reassignment ─────────────────

from services.meta_campaign_scope import (  # noqa: E402
    apply_selection,
    on_group_deleted,
    reassign_account_leads,
)

ACCOUNT = [
    {"id": "c1", "name": "Body Spring", "effective_status": "ACTIVE"},
    {"id": "c2", "name": "Face Promo", "effective_status": "PAUSED"},
    {"id": "c3", "name": "Old Body", "effective_status": "ARCHIVED"},
]


def _group(gid, name, ids="__missing__", created_at="2026-01-01T00:00:00", **extra):
    doc = {"id": gid, "user_id": "A", "name": name, "meta_ad_account_id": "act_1",
           "created_at": created_at, **extra}
    if ids != "__missing__":
        doc["meta_campaign_ids"] = ids
    return doc


def _lead(lid, campaign, owner="g1", owner_name="Plush Body", account="act_1", user="A"):
    return {"user_id": user, "ad_account_id": account, "client_group_id": owner,
            "client_group_name": owner_name, "lead_id": lid,
            "lead_data": {"campaign_id": campaign}}


async def _owners(db):
    rows = await db.facebook_leads.find({}, {"_id": 0}).to_list(length=None)
    return {r["lead_id"]: (r["client_group_id"], r["client_group_name"]) for r in rows}


async def _ids(db, gid):
    doc = await db.client_groups.find_one({"id": gid})
    return doc.get("meta_campaign_ids", "__missing__")


async def test_reassign_moves_leads_by_campaign(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ["c1"]), _group("g2", "Plush Face", ["c2"])])
    await mock_db.facebook_leads.insert_many([
        _lead("l1", "c1"), _lead("l2", "c2"), _lead("l3", "c3"),
        _lead("other_acct", "c2", account="act_9"),
        _lead("other_user", "c2", user="B"),
    ])
    n = await reassign_account_leads(mock_db, "A", "act_1")
    owners = await _owners(mock_db)
    assert owners["l1"] == ("g1", "Plush Body")
    assert owners["l2"] == ("g2", "Plush Face")
    assert owners["l3"] == (None, None)
    assert owners["other_acct"] == ("g1", "Plush Body")
    assert owners["other_user"] == ("g1", "Plush Body")
    assert n == 2
    # Idempotent: a second pass changes nothing.
    assert await reassign_account_leads(mock_db, "A", "act_1") == 0


async def test_reassign_single_null_group_owns_everything(mock_db):
    await mock_db.client_groups.insert_one(_group("g1", "Plush Body"))
    await mock_db.facebook_leads.insert_many([
        _lead("l1", "c1", owner=None, owner_name=None), _lead("l2", "c2", owner="gone",
                                                            owner_name="Gone")])
    n = await reassign_account_leads(mock_db, "A", "act_1")
    assert await _owners(mock_db) == {"l1": ("g1", "Plush Body"), "l2": ("g1", "Plush Body")}
    assert n == 2


async def test_reassign_uses_bulk_writes_not_per_row(mock_db, monkeypatch):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ["c1"]), _group("g2", "Plush Face", ["c2"])])
    await mock_db.facebook_leads.insert_many([_lead(f"l{i}", f"c{i % 3 + 1}") for i in range(30)])
    coll = mock_db.facebook_leads
    calls = {"many": 0, "one": 0}
    real_many = coll.update_many

    async def many(*a, **k):
        calls["many"] += 1
        return await real_many(*a, **k)

    async def one(*a, **k):
        calls["one"] += 1
        raise AssertionError("per-row write")

    monkeypatch.setattr(type(coll), "update_many", lambda self, *a, **k: many(*a, **k))
    monkeypatch.setattr(type(coll), "update_one", lambda self, *a, **k: one(*a, **k))
    await reassign_account_leads(mock_db, "A", "act_1")
    assert calls == {"many": 3, "one": 0}   # g1, g2, unassigned


async def test_first_share_auto_splits_null_sibling(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body"), _group("g2", "Plush Face", None)])
    await mock_db.client_groups.update_one({"id": "g2"}, {"$unset": {"meta_campaign_ids": ""}})
    await mock_db.facebook_leads.insert_many([
        _lead("l1", "c1"), _lead("l2", "c2"), _lead("l3", "c3")])

    out = await apply_selection(mock_db, "A", "g2", ["c2"], ACCOUNT)

    assert await _ids(mock_db, "g1") == ["c1", "c3"]
    assert await _ids(mock_db, "g2") == ["c2"]
    assert out["converted"] == [{"id": "g1", "name": "Plush Body"}]
    assert sorted(out["changed_group_ids"]) == ["g1", "g2"]
    owners = await _owners(mock_db)
    assert owners["l2"] == ("g2", "Plush Face")
    assert owners["l1"] == owners["l3"] == ("g1", "Plush Body")


async def test_auto_split_skips_campaigns_claimed_by_listed_siblings(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", None), _group("g2", "Plush Face", None),
        _group("g3", "Plush Hair", ["c3"])])
    # g1 null, g3 owns c3 already: g2 takes c2, g1 gets only c1.
    await mock_db.client_groups.update_one({"id": "g2"}, {"$set": {"meta_campaign_ids": []}})
    out = await apply_selection(mock_db, "A", "g2", ["c2"], ACCOUNT)
    assert await _ids(mock_db, "g1") == ["c1"]
    assert await _ids(mock_db, "g3") == ["c3"]
    assert sorted(out["changed_group_ids"]) == ["g1", "g2"]


async def test_two_null_siblings_oldest_gets_remainder(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Newer", None, created_at="2026-05-01T00:00:00"),
        _group("g2", "Saver", ["c2"]),
        _group("g3", "Older", None, created_at="2026-02-01T00:00:00"),
    ])
    out = await apply_selection(mock_db, "A", "g2", ["c2"], ACCOUNT)
    assert await _ids(mock_db, "g3") == ["c1", "c3"]
    assert await _ids(mock_db, "g1") == []
    assert out["converted"] == [{"id": "g3", "name": "Older"}, {"id": "g1", "name": "Newer"}]
    assert sorted(out["changed_group_ids"]) == ["g1", "g2", "g3"]


async def test_apply_clears_caches_and_supersedes_jobs(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", None, facebook_cache={"last_7d": {"spend": 1}},
               meta_daily_spend=[{"d": 1}], last_meta_refresh="2026-10-01"),
        _group("g2", "Plush Face", None, facebook_cache={"x": 1}, meta_daily_spend=[],
               last_meta_refresh="2026-10-01"),
        _group("g9", "Untouched", None, facebook_cache={"x": 1}, meta_ad_account_id="act_9"),
    ])
    await mock_db.meta_refresh_jobs.insert_many([
        {"job_id": "j1", "group_id": "g1", "status": "pending"},
        {"job_id": "j2", "group_id": "g1", "status": "in_progress"},
        {"job_id": "j3", "group_id": "g2", "status": "partial"},
        {"job_id": "j4", "group_id": "g1", "status": "complete"},
        {"job_id": "j5", "group_id": "g1", "status": "failed"},
        {"job_id": "j6", "group_id": "g9", "status": "pending"},
    ])
    await apply_selection(mock_db, "A", "g2", ["c2"], ACCOUNT)

    for gid in ("g1", "g2"):
        doc = await mock_db.client_groups.find_one({"id": gid})
        for f in ("facebook_cache", "meta_daily_spend", "last_meta_refresh"):
            assert f not in doc, (gid, f)
    assert "facebook_cache" in await mock_db.client_groups.find_one({"id": "g9"})
    status = {j["job_id"]: j["status"]
              async for j in mock_db.meta_refresh_jobs.find({})}
    assert status == {"j1": "superseded", "j2": "superseded", "j3": "superseded",
                      "j4": "complete", "j5": "failed", "j6": "pending"}


async def test_apply_conflict_writes_nothing(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ["c1"], facebook_cache={"x": 1}),
        _group("g2", "Plush Face", ["c2"], facebook_cache={"y": 1}),
        _group("g3", "Plush Hair", None),
    ])
    await mock_db.meta_refresh_jobs.insert_one({"job_id": "j1", "group_id": "g2", "status": "pending"})
    await mock_db.facebook_leads.insert_one(_lead("l1", "c1", owner="g2", owner_name="Plush Face"))
    before = await mock_db.client_groups.find({}, {"_id": 0}).to_list(length=None)

    with pytest.raises(ScopeConflict) as e:
        await apply_selection(mock_db, "A", "g2", ["c2", "c1"], ACCOUNT)
    assert "Plush Body" in e.value.message and "Body Spring" in e.value.message

    with pytest.raises(ScopeConflict):
        await apply_selection(mock_db, "A", "g2", None, ACCOUNT)

    assert await mock_db.client_groups.find({}, {"_id": 0}).to_list(length=None) == before
    assert (await mock_db.meta_refresh_jobs.find_one({"job_id": "j1"}))["status"] == "pending"
    assert (await _owners(mock_db))["l1"] == ("g2", "Plush Face")


async def test_apply_missing_or_foreign_group_is_lookup_error(mock_db):
    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", user_id="B"))
    with pytest.raises(LookupError):
        await apply_selection(mock_db, "A", "g1", ["c1"], ACCOUNT)
    with pytest.raises(LookupError):
        await apply_selection(mock_db, "A", "nope", ["c1"], ACCOUNT)


async def test_unshared_group_can_go_back_to_all(mock_db):
    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ["c1"]))
    await mock_db.facebook_leads.insert_one(_lead("l2", "c2", owner=None, owner_name=None))
    out = await apply_selection(mock_db, "A", "g1", None, ACCOUNT)
    assert await _ids(mock_db, "g1") is None
    assert out == {"changed_group_ids": ["g1"], "converted": []}
    assert (await _owners(mock_db))["l2"] == ("g1", "Plush Body")


async def test_delete_one_of_two_keeps_survivor_list(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ["c1"]), _group("g2", "Plush Face", ["c2"])])
    await mock_db.facebook_leads.insert_many([
        _lead("l1", "c1"), _lead("l2", "c2", owner="g2", owner_name="Plush Face")])
    await mock_db.client_groups.delete_one({"id": "g2"})

    await on_group_deleted(mock_db, "A", "act_1")

    assert await _ids(mock_db, "g1") == ["c1"]
    owners = await _owners(mock_db)
    assert owners["l1"] == ("g1", "Plush Body")
    assert owners["l2"] == (None, None)


# ── superseded jobs in the refresh state machine ──────────────────────────

async def test_superseded_job_is_never_claimed(mock_mongo_client, mock_db):
    from services.meta_refresh_manager import claim_next_jobs

    await mock_db.meta_refresh_jobs.insert_one({
        "job_id": "j1", "group_id": "g1", "status": "superseded",
        "attempt": 1, "max_attempts": 3, "next_retry_at": None,
        "_claimed_at": None, "_partial_kind": "budget", "created_at": 1,
    })
    seen = []
    jobs = mock_db.meta_refresh_jobs

    class Recorder:
        """mongomock lacks pipeline updates; evaluate the claim filter only."""
        async def update_many(self, filt, update):
            return await jobs.update_many(filt, update)

        async def find_one_and_update(self, filt, update, sort=None):
            doc = await jobs.find_one(filt)
            seen.append(doc)
            return None

    class Db:
        def __getitem__(self, _):
            return Recorder()

    class Client:
        def __getitem__(self, _):
            return Db()

    assert await claim_next_jobs(Client(), 1) == []
    assert seen == [None]
    assert (await jobs.find_one({"job_id": "j1"}))["status"] == "superseded"


async def test_superseded_job_does_not_block_scheduling(mock_mongo_client, mock_db):
    from services.meta_refresh_manager import schedule_stale_groups

    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ["c1"]))
    await mock_db.meta_refresh_jobs.insert_one(
        {"job_id": "old", "group_id": "g1", "status": "superseded"})
    out = await schedule_stale_groups(mock_mongo_client, 5, ["last_7d"])
    assert out == ["g1"]
    assert await mock_db.meta_refresh_jobs.count_documents(
        {"group_id": "g1", "status": "pending"}) == 1


async def test_superseded_job_finalize_leaves_it_and_group_alone(mock_db):
    from services.meta_refresh_manager import _finalize_job

    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ["c1"]))
    await mock_db.meta_refresh_jobs.insert_one({
        "job_id": "j1", "group_id": "g1", "status": "superseded",
        "attempt": 1, "max_attempts": 3,
        "steps": {"presets": {"last_7d": {"status": "success"}},
                  "leads": {"status": "success"}, "lead_counts": {"status": "success"}},
    })
    await _finalize_job(mock_db, mock_db.meta_refresh_jobs, "j1", "g1")
    assert (await mock_db.meta_refresh_jobs.find_one({"job_id": "j1"}))["status"] == "superseded"
    g = await mock_db.client_groups.find_one({"id": "g1"})
    assert "last_meta_refresh" not in g and "meta_refresh_status" not in g


async def test_superseded_inflight_job_stops_before_next_preset(mock_mongo_client, mock_db, monkeypatch):
    import services.meta_refresh_manager as mrm

    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ["c1"]))
    await mock_db.meta_refresh_jobs.insert_one({
        "job_id": "j1", "group_id": "g1", "user_id": "A", "ad_account_id": "act_1",
        "currency": "GBP", "status": "in_progress", "attempt": 1, "max_attempts": 3,
        "steps": {"presets": {"p1": {"status": "pending", "attempt": 0, "error": None},
                              "p2": {"status": "pending", "attempt": 0, "error": None}},
                  "leads": {"status": "pending", "attempt": 0, "error": None},
                  "lead_counts": {"status": "pending", "attempt": 0, "error": None}},
    })
    fetched = []

    async def token(*a, **k):
        return {"access_token": "t"}

    async def fetch(**k):
        fetched.append(k["presets"][0])
        # The selection changes while the first preset is in flight.
        await mock_db.meta_refresh_jobs.update_one(
            {"job_id": "j1"}, {"$set": {"status": "superseded"}})

    async def no_sleep(_):
        return None

    monkeypatch.setattr(mrm, "get_facebook_token", token)
    monkeypatch.setattr(mrm, "fetch_meta_all_presets_for_group", fetch)
    monkeypatch.setattr(mrm.asyncio, "sleep", no_sleep)

    await mrm.execute_refresh("j1", mock_mongo_client, max_seconds=1000)

    assert fetched == ["p1"]
    assert (await mock_db.meta_refresh_jobs.find_one({"job_id": "j1"}))["status"] == "superseded"
    assert "last_meta_refresh" not in await mock_db.client_groups.find_one({"id": "g1"})


# ── Fix round 1: writes inside a step race a selection change ─────────────
#
# Each fake below runs between the step's scope read and its write, and saves a
# new selection for g2 there — which converts g1 (null) to ["c1", "c3"]. The
# old-scope write must then match nothing, and leads must end with the owner
# of their campaign.

async def _race_setup(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ad_account_currency="USD",
               last_meta_refresh="old-stamp"),
        _group("g2", "Plush Face", [], created_at="2026-06-01T00:00:00"),
    ])
    await mock_db.client_groups.update_one({"id": "g1"}, {"$unset": {"last_meta_refresh": ""}})


async def _no_sleep(*_a, **_k):
    return None


async def _change_selection(mock_db):
    await apply_selection(mock_db, "A", "g2", ["c2"], ACCOUNT)


async def test_preset_write_skipped_when_selection_changes_mid_fetch(
        mock_mongo_client, mock_db, monkeypatch):
    import httpx
    import json as _json
    import urllib.parse
    from services import meta_service

    await _race_setup(mock_db)

    def handler(request):
        if request.method == "POST":
            n = len(_json.loads(dict(urllib.parse.parse_qsl(request.content.decode()))["batch"]))
            return httpx.Response(200, json=[
                {"code": 200, "body": _json.dumps({"data": []})} for _ in range(n)])
        return httpx.Response(200, json={"data": []})

    real = httpx.AsyncClient
    monkeypatch.setattr("services.meta_service.httpx.AsyncClient", lambda *a, **kw: real(
        transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "timeout"}))

    async def token(*_a, **_k):
        return {"access_token": "T"}

    async def creatives_then_change(*_a, **_k):
        await _change_selection(mock_db)
        return {}

    monkeypatch.setattr(meta_service, "get_facebook_token", token)
    monkeypatch.setattr(meta_service.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(meta_service, "_fetch_ad_creatives", creatives_then_change)

    await meta_service.fetch_meta_all_presets_for_group(
        "g1", "act_1", "A", mock_mongo_client, ad_account_currency="USD", presets=["today"])

    g1 = await mock_db.client_groups.find_one({"id": "g1"})
    assert g1["meta_campaign_ids"] == ["c1", "c3"]
    assert "facebook_cache" not in g1
    assert "last_meta_refresh" not in g1


async def test_preset_write_lands_when_selection_unchanged(
        mock_mongo_client, mock_db, monkeypatch):
    import httpx
    from services import meta_service

    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ["c1"]))
    real = httpx.AsyncClient
    monkeypatch.setattr("services.meta_service.httpx.AsyncClient", lambda *a, **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, json={})),
        **{k: v for k, v in kw.items() if k != "timeout"}))

    async def token(*_a, **_k):
        return {"access_token": "T"}

    async def creatives(*_a, **_k):
        return {}

    monkeypatch.setattr(meta_service, "get_facebook_token", token)
    monkeypatch.setattr(meta_service.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(meta_service, "_fetch_ad_creatives", creatives)
    try:
        await meta_service.fetch_meta_all_presets_for_group(
            "g1", "act_1", "A", mock_mongo_client, ad_account_currency="USD", presets=["today"])
    except RuntimeError:
        pass  # preset missing after retries; the account-level fields still land
    assert "last_meta_refresh" in await mock_db.client_groups.find_one({"id": "g1"})


async def test_leads_written_mid_change_end_with_right_owner(
        mock_mongo_client, mock_db, monkeypatch):
    import integrations.facebook_utils.facebook as fb
    import integrations.facebook_utils.meta_incremental_refresh as inc
    from integrations.facebook_utils.facebook_leads import fetch_and_cache_facebook_leads_FIXED
    from utils.currency_exchange import CurrencyService

    await _race_setup(mock_db)
    await mock_db.facebook_leads.insert_one(_lead("l0", "c2"))

    async def token(*_a, **_k):
        return {"access_token": "T"}

    async def currency(*_a, **_k):
        return "USD"

    async def fetch_then_change(**_k):
        await _change_selection(mock_db)
        return 2, [{"lead_id": "l1", "campaign_id": "c1"}, {"lead_id": "l2", "campaign_id": "c2"}]

    monkeypatch.setattr(fb, "get_facebook_token", token)
    monkeypatch.setattr(CurrencyService, "get_user_currency", staticmethod(currency))
    monkeypatch.setattr(inc, "fetch_todays_facebook_leads_incremental", fetch_then_change)

    await fetch_and_cache_facebook_leads_FIXED(
        "USD", "g1", "act_1", "A", mock_mongo_client, is_initial_load=False)

    owners = await _owners(mock_db)
    assert owners["l1"] == ("g1", "Plush Body")
    assert owners["l2"] == ("g2", "Plush Face")
    assert owners["l0"] == ("g2", "Plush Face")
    g1 = await mock_db.client_groups.find_one({"id": "g1"})
    assert "facebook_cache" not in g1 and "last_meta_refresh" not in g1
    assert not g1.get("leads_initial_sync_done")


async def test_lead_counts_write_skipped_when_selection_changes(
        mock_mongo_client, mock_db, monkeypatch):
    from services import meta_service

    await _race_setup(mock_db)

    async def timeline_then_change(*_a, **_k):
        await _change_selection(mock_db)
        return []

    monkeypatch.setattr(meta_service, "fetch_lead_timeline", timeline_then_change)
    await meta_service.update_preset_lead_counts("g1", "A", mock_mongo_client, presets=["today"])
    g1 = await mock_db.client_groups.find_one({"id": "g1"})
    assert "facebook_cache" not in g1


async def test_daily_spend_write_skipped_when_selection_changes(
        mock_mongo_client, mock_db, monkeypatch):
    from integrations.facebook_utils import meta_daily_spend

    await _race_setup(mock_db)

    async def rows_then_change(*_a, **_k):
        await _change_selection(mock_db)
        return [{"date": "2026-10-01", "spend": 5.0, "currency": "USD"}]

    monkeypatch.setattr(meta_daily_spend, "fetch_account_daily_spend", rows_then_change)
    await meta_daily_spend.cache_account_daily_spend(
        "g1", "act_1", "T", mock_mongo_client, user_currency="USD", ad_account_currency="USD")
    assert "meta_daily_spend" not in await mock_db.client_groups.find_one({"id": "g1"})
