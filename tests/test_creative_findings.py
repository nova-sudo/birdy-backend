"""Creative test findings -> suggestions (Task 21)."""

from datetime import datetime, timezone

import pytest

from ai.suggestions import creative_findings as cf
from ai.suggestions import store
from routers.dashboard import _suggestion_to_api
from services.creatives import posting, reconcile
from services.creatives.store import (
    POST_JOBS, TEST_ADS, add_test_ads, create_test, get_test, list_test_ads, update_test,
)
from services.meta_status_service import MetaStatusError

U = "u1"
NOW = datetime(2026, 11, 3, 12, 0)
SUMMARY = {"spend": 600.0, "currency": "GBP", "leads": 20, "cpl": 30.0, "accounts_total": 3,
           "accounts_scored": 3, "accounts_pass": 2, "accounts_fail": 1}


@pytest.fixture(autouse=True)
def _no_slack(monkeypatch):
    import services.slack_suggestion_notifier as n
    posted = []

    async def post(db, user_id, docs):
        posted.extend(docs)
        return len(docs)

    async def update(db, user_id, docs):
        return 0

    monkeypatch.setattr(n, "post_new_suggestions", post)
    monkeypatch.setattr(n, "update_suggestions", update)
    return posted


class Status:
    def __init__(self, monkeypatch, fail=()):
        self.calls, self.fail = [], set(fail)

        async def set_status(user_id, object_id, object_type, status, client):
            if object_id in self.fail:
                raise MetaStatusError("nope", 400)
            self.calls.append((object_id, status))

        monkeypatch.setattr(posting, "set_object_status", set_status)


async def seed(db, states=("pass", "pass", "fail"), *, verdict=None, status="testing",
               on_verdict=None, extra_groups=(), uid=U, post_states=None, meta_status=None):
    t = await create_test(db, uid, name="Hook", origin="new", creative={"format": "video"},
                          targets={"metrics": {"cpl": {"op": "lte", "value": 35}}},
                          on_verdict=on_verdict)
    await update_test(db, uid, t["id"], {"status": status, "verdict": verdict, "summary": SUMMARY})
    rows = []
    for i, st in enumerate(states):
        rows.append({"ad_account_id": f"act_{i}", "client_group_id": f"g{i}", "adset_id": f"as{i}",
                     "post_state": (post_states or {}).get(i, "active"),
                     "meta": {"ad_id": f"{uid}-ad{i}"}, "score": {"state": st, "cpl": 30.0},
                     "meta_status": (meta_status or {}).get(i, "ACTIVE")})
        await db["client_groups"].insert_one({"id": f"g{i}", "user_id": uid, "name": f"Client {i}",
                                              "meta_ad_account_id": f"act_{i}"})
    for gid, acct in extra_groups:
        await db["client_groups"].insert_one({"id": gid, "user_id": uid, "name": gid,
                                              "meta_ad_account_id": acct})
    await add_test_ads(db, uid, t["id"], rows)
    return await get_test(db, uid, t["id"]), await list_test_ads(db, t["id"], user_id=uid)


async def groups(db, uid=U):
    return {g["id"]: g for g in await db["client_groups"].find({"user_id": uid}).to_list(None)}


async def open_docs(db, uid=U):
    return await db[store.SUGGESTIONS].find({"user_id": uid, "agent": cf.AGENT, "status": "open"}).to_list(None)


async def sync(db, client, t, uid=U):
    t = await get_test(db, uid, t["id"])
    return await cf.sync_creative_suggestions(db, uid, t, await list_test_ads(db, t["id"], user_id=uid),
                                              mongo_client=client)


# ── findings_for_test (pure) ────────────────────────────────────────────────

async def test_winner_with_all_accounts_used_has_no_winner_finding(mock_db):
    t, ads = await seed(mock_db, verdict="winner", status="winner")
    rules = [f.rule_id for f in cf.findings_for_test(t, ads, await groups(mock_db))]
    assert "creative_winner" not in rules


async def test_winner_with_unrun_account_suggests_scale(mock_db):
    t, ads = await seed(mock_db, verdict="winner", status="winner", extra_groups=[("g9", "act_9")])
    [f] = [f for f in cf.findings_for_test(t, ads, await groups(mock_db)) if f.rule_id == "creative_winner"]
    assert f.severity == "OPPORTUNITY"
    assert f.action.type == "scale_creative" and f.action.params == {"test_id": t["id"]}
    assert f.dedup_key == f"creative_winner:{t['id']}"
    assert f.evidence.window == "weekly" and f.evidence.raw["creative_test_id"] == t["id"]
    assert f.evidence.raw["unrun_ad_account_ids"] == ["act_9"]
    assert f.agent == cf.AGENT and f.client_group_id in {"g0", "g1", "g2"}
    labels = {s.label for s in f.evidence.stats}
    assert {"CPL", "Accounts passing", "Spent"} <= labels


async def test_loser_targets_only_failing_unpaused_ads(mock_db):
    t, ads = await seed(mock_db, ("fail", "pass", "fail", "fail"), verdict="loser", status="loser",
                        meta_status={3: "PAUSED"})
    [f] = [f for f in cf.findings_for_test(t, ads, await groups(mock_db)) if f.rule_id == "creative_loser"]
    assert f.severity == "HIGH" and f.action.type == "pause_ads"
    assert sorted(x["object_id"] for x in f.action.targets) == ["u1-ad0", "u1-ad2"]
    assert all(x["object_type"] == "ad" for x in f.action.targets)
    assert f.dedup_key == f"creative_loser:{t['id']}"


async def test_account_fail_per_account_only_when_enabled_and_testing(mock_db):
    t, ads = await seed(mock_db, ("fail", "pass", "fail"), on_verdict={"suggest_pause_failing": True})
    fails = [f for f in cf.findings_for_test(t, ads, await groups(mock_db)) if f.rule_id == "creative_account_fail"]
    assert sorted(f.dedup_key for f in fails) == [f"creative_account_fail:{t['id']}:act_0",
                                                   f"creative_account_fail:{t['id']}:act_2"]
    assert {f.client_group_id for f in fails} == {"g0", "g2"}
    assert all(f.severity == "MEDIUM" and f.action.type == "pause_ads" for f in fails)
    t2, ads2 = await seed(mock_db, ("fail",), on_verdict={"suggest_pause_failing": False}, uid="u2")
    assert not [f for f in cf.findings_for_test(t2, ads2, await groups(mock_db, "u2"))
                if f.rule_id == "creative_account_fail"]


async def test_post_failed_finding_from_failed_row(mock_db):
    t, ads = await seed(mock_db, ("pass", "scoring"), post_states={1: "failed"})
    await mock_db[TEST_ADS].update_one({"id": ads[1]["id"]}, {"$set": {"post_error": {
        "code": 100, "message": "This lead form does not belong to the selected Page.",
        "step": "creative", "fix": "use_website_url"}}})
    t = await update_test(mock_db, U, t["id"], {"creative": {"format": "video", "website_url": "https://x.co"}})
    ads = await list_test_ads(mock_db, t["id"], user_id=U)
    [f] = [f for f in cf.findings_for_test(t, ads, await groups(mock_db)) if f.rule_id == "creative_post_failed"]
    assert f.severity == "HIGH" and f.action.type == "retry_creative_post"
    assert f.action.params == {"test_id": t["id"], "test_ad_id": ads[1]["id"],
                               "patch": {"destination": "website"}}
    assert f.dedup_key == f"creative_post_failed:{t['id']}:act_1" and f.client_group_id == "g1"


# ── sync_creative_suggestions ───────────────────────────────────────────────

async def test_dedup_keys_stable_across_runs(mock_db, mock_mongo_client):
    t, _ = await seed(mock_db, ("fail", "pass", "fail"), on_verdict={"suggest_pause_failing": True})
    await sync(mock_db, mock_mongo_client, t)
    first = sorted(d["_id"] for d in await open_docs(mock_db))
    await sync(mock_db, mock_mongo_client, t)
    assert sorted(d["_id"] for d in await open_docs(mock_db)) == first and len(first) == 2
    assert await mock_db[store.SUGGESTIONS].count_documents({}) == 2
    created = await mock_db[store.ACTIVITY].count_documents({"kind": store.KIND_SUGGESTION_CREATED})
    assert created == 2


async def test_new_findings_reach_slack(mock_db, mock_mongo_client, _no_slack):
    t, _ = await seed(mock_db, ("fail", "pass"), on_verdict={"suggest_pause_failing": True})
    await sync(mock_db, mock_mongo_client, t)
    assert len(_no_slack) == 1
    await sync(mock_db, mock_mongo_client, t)
    assert len(_no_slack) == 1  # a refresh is not a new post


async def test_turning_winner_closes_account_fail_items_of_this_test_only(mock_db, mock_mongo_client):
    t, _ = await seed(mock_db, ("fail", "pass", "pass"), on_verdict={"suggest_pause_failing": True})
    other, _ = await seed(mock_db, ("fail", "pass"), on_verdict={"suggest_pause_failing": True})
    await sync(mock_db, mock_mongo_client, t)
    await sync(mock_db, mock_mongo_client, other)
    assert len(await open_docs(mock_db)) == 2
    await update_test(mock_db, U, t["id"], {"status": "winner", "verdict": "winner"})
    await sync(mock_db, mock_mongo_client, t)
    still = await open_docs(mock_db)
    assert [d["evidence"]["creative_test_id"] for d in still] == [other["id"]]
    closed = await mock_db[store.SUGGESTIONS].find_one({"rule_id": "creative_account_fail",
                                                       "evidence.creative_test_id": t["id"]})
    assert closed["status"] == store.STATUS_RESOLVED


async def test_auto_pause_on_loser_pauses_failing_and_stores_applied(mock_db, mock_mongo_client, monkeypatch):
    st = Status(monkeypatch)
    t, _ = await seed(mock_db, ("fail", "pass", "fail"), verdict="loser", status="loser",
                      on_verdict={"auto_pause_losers": True})
    await sync(mock_db, mock_mongo_client, t)
    assert sorted(st.calls) == [("u1-ad0", "PAUSED"), ("u1-ad2", "PAUSED")]
    ads = {a["meta"]["ad_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    assert ads["u1-ad1"]["meta_status"] == "ACTIVE" and ads["u1-ad0"]["meta_status"] == "PAUSED"
    doc = await mock_db[store.SUGGESTIONS].find_one({"rule_id": "creative_loser"})
    assert doc["status"] == store.STATUS_APPLIED
    assert sorted(x["object_id"] for x in doc["applied_targets"]) == ["u1-ad0", "u1-ad2"]
    assert doc["decided_by"]["actor"] == "birdy"
    logged = await mock_db[store.ACTIVITY].find({"kind": store.KIND_ACTION_APPLIED}).to_list(None)
    # One row per affected client group (g0, g2), for the pause cap.
    assert sorted(a["client_group_id"] for a in logged) == ["g0", "g2"]
    assert all(a["actor"] == "birdy" for a in logged)
    # Re-run: no second pause, no second log.
    await sync(mock_db, mock_mongo_client, t)
    assert len(st.calls) == 2
    assert await mock_db[store.ACTIVITY].count_documents({"kind": store.KIND_ACTION_APPLIED}) == 2


async def test_auto_pause_after_user_undo_does_not_repause(mock_db, mock_mongo_client, monkeypatch):
    st = Status(monkeypatch)
    t, _ = await seed(mock_db, ("fail", "pass"), verdict="loser", status="loser",
                      on_verdict={"auto_pause_losers": True})
    await sync(mock_db, mock_mongo_client, t)
    doc = await mock_db[store.SUGGESTIONS].find_one({"rule_id": "creative_loser"})
    await store.mark_undone(mock_db, U, doc["_id"])
    await mock_db[TEST_ADS].update_many({}, {"$set": {"meta_status": "ACTIVE"}})
    await sync(mock_db, mock_mongo_client, t)
    assert len(st.calls) == 1


async def test_loser_without_auto_pause_stays_open(mock_db, mock_mongo_client, monkeypatch):
    st = Status(monkeypatch)
    t, _ = await seed(mock_db, ("fail", "pass"), verdict="loser", status="loser")
    await sync(mock_db, mock_mongo_client, t)
    assert st.calls == []
    doc = await mock_db[store.SUGGESTIONS].find_one({"rule_id": "creative_loser"})
    assert doc["status"] == "open"


async def test_api_passthrough_exposes_creative_test_id(mock_db, mock_mongo_client):
    t, _ = await seed(mock_db, ("fail", "pass"), on_verdict={"suggest_pause_failing": True})
    await sync(mock_db, mock_mongo_client, t)
    [doc] = await open_docs(mock_db)
    api = _suggestion_to_api(doc)
    assert api["creative_test_id"] == t["id"] and api["rule_id"] == "creative_account_fail"


async def test_sync_is_owner_scoped(mock_db, mock_mongo_client):
    t, _ = await seed(mock_db, ("fail", "pass"), on_verdict={"suggest_pause_failing": True})
    t2, _ = await seed(mock_db, ("fail", "pass"), on_verdict={"suggest_pause_failing": True}, uid="u2")
    await sync(mock_db, mock_mongo_client, t)
    await sync(mock_db, mock_mongo_client, t2, uid="u2")
    await update_test(mock_db, U, t["id"], {"status": "winner", "verdict": "winner"})
    await sync(mock_db, mock_mongo_client, t)
    assert len(await open_docs(mock_db, "u2")) == 1


# ── hooks: tracking, posting failure, reconcile ─────────────────────────────

async def test_failed_post_emits_finding(mock_db, mock_mongo_client):
    t, ads = await seed(mock_db, ("pass", "scoring"), post_states={1: "uploading"})
    job = {"id": "j1", "user_id": U, "test_id": t["id"], "test_ad_id": ads[1]["id"],
           "state": "running", "lease_id": "L"}
    await mock_db[POST_JOBS].insert_one(dict(job))
    err = {"code": 100, "subcode": None, "message": "Bad", "step": "ad", "fix": None}
    await posting._fail(mock_db, job, ads[1], err, NOW)
    [doc] = await open_docs(mock_db)
    assert doc["rule_id"] == "creative_post_failed"
    assert doc["action"]["params"] == {"test_id": t["id"], "test_ad_id": ads[1]["id"], "patch": {}}


async def test_tracking_survives_suggestion_failure(mock_db, mock_mongo_client, monkeypatch):
    from jobs import creative_tracking as ct

    async def boom(*a, **k):
        raise RuntimeError("suggestions down")

    monkeypatch.setattr(cf, "sync_creative_suggestions", boom)
    out = await ct._sync_suggestions(mock_db, mock_mongo_client, {"id": "x", "user_id": U})
    assert out is None


async def test_reconcile_emits_adopt_findings(mock_db, mock_mongo_client):
    t = await create_test(mock_db, U, name="x", origin="new", creative={"format": "video"}, targets={})
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": U, "name": "Client 1", "meta_ad_account_id": "act_1",
        "facebook_cache": {"entities": {"ads": [
            {"id": "a1", "name": f"{t['code']} | hook", "adset_id": "as1", "campaign_id": "c1"}]}}})
    await reconcile.reconcile_all(mock_db)
    [doc] = await open_docs(mock_db)
    assert doc["rule_id"] == "creative_adopt" and doc["severity"] == "MEDIUM"
    assert doc["action"] == {"type": "adopt_creative_ad", "targets": [],
                             "params": {"test_id": t["id"], "ad_id": "a1"}}
    assert doc["evidence"]["creative_test_id"] == t["id"] and doc["client_group_id"] == "g1"
    first = doc["_id"]
    await reconcile.reconcile_all(mock_db)
    assert [d["_id"] for d in await open_docs(mock_db)] == [first]
    # Once adopted it is no longer adoptable: the next pass resolves it.
    await reconcile.adopt(mock_db, U, t["id"], "a1")
    await reconcile.reconcile_all(mock_db)
    assert await open_docs(mock_db) == []


async def test_adopt_sync_leaves_test_findings_alone(mock_db, mock_mongo_client):
    t, _ = await seed(mock_db, ("fail", "pass"), on_verdict={"suggest_pause_failing": True})
    await sync(mock_db, mock_mongo_client, t)
    await cf.sync_adopt_suggestions(mock_db, U, [])
    assert len(await open_docs(mock_db)) == 1


async def test_track_test_syncs_suggestions(mock_db, mock_mongo_client, monkeypatch):
    from jobs import creative_tracking as ct
    from tests.test_creative_tracking import Meta, make_test

    async def token(uid, client):
        return {"access_token": "tok"}

    await mock_db["users"].insert_one({"user_id": U, "default_currency": "GBP"})
    monkeypatch.setattr(ct, "get_facebook_token", token)
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    await update_test(mock_db, U, t["id"], {"on_verdict": {"suggest_pause_failing": True}})
    m.leads = {"u1-ad0": 10, "u1-ad1": 2, "u1-ad2": 2}
    out = await ct.track_test(mock_db, mock_mongo_client, await get_test(mock_db, U, t["id"]), today=ct.date(2026, 11, 3))
    assert out["verdict"] == "loser"
    [doc] = await open_docs(mock_db)
    assert doc["rule_id"] == "creative_loser" and len(doc["action"]["targets"]) == 2


# ── fix round 1 ─────────────────────────────────────────────────────────────

async def seed_rows(db, rows, *, verdict="loser", status="loser", on_verdict=None):
    """rows: (ad_account_id, client_group_id, score_state) per ad."""
    t = await create_test(db, U, name="Hook", origin="new", creative={"format": "video"},
                          targets={}, on_verdict=on_verdict or {"auto_pause_losers": True})
    await update_test(db, U, t["id"], {"status": status, "verdict": verdict, "summary": SUMMARY})
    for gid in sorted({g for _, g, _ in rows}):
        await db["client_groups"].insert_one({"id": gid, "user_id": U, "name": gid,
                                              "meta_ad_account_id": f"act_{gid}"})
    await add_test_ads(db, U, t["id"], [
        {"ad_account_id": acct, "client_group_id": gid, "post_state": "active", "meta_status": "ACTIVE",
         "meta": {"ad_id": f"ad{i}"}, "score": {"state": st}} for i, (acct, gid, st) in enumerate(rows)])
    return await get_test(db, U, t["id"])


async def test_auto_pause_is_per_ad_and_undo_restores_exactly_it(mock_db, mock_mongo_client, monkeypatch):
    import ai.suggestions.actions as actions_mod
    from ai.suggestions import actions

    st = Status(monkeypatch)
    undo_writes = []

    async def set_status(user_id, object_id, object_type, status, client):
        undo_writes.append((object_id, status))

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(actions_mod, "set_object_status", set_status)
    monkeypatch.setattr(actions_mod, "_sync_slack", nothing)
    t = await seed_rows(mock_db, [("act_1", "g1", "fail"), ("act_1", "g1", "pass")])
    await sync(mock_db, mock_mongo_client, t)
    assert st.calls == [("ad0", "PAUSED")]
    rows = {a["meta"]["ad_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    assert rows["ad0"]["meta_status"] == "PAUSED" and rows["ad1"]["meta_status"] == "ACTIVE"
    doc = await mock_db[store.SUGGESTIONS].find_one({"rule_id": "creative_loser"})
    assert [x["object_id"] for x in doc["applied_targets"]] == ["ad0"]
    res = await actions.undo_suggestion(mock_db, mock_mongo_client, U, doc["_id"])
    assert res["outcome"] == "undone" and undo_writes == [("ad0", "ACTIVE")]
    rows = {a["meta"]["ad_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    assert rows["ad0"]["meta_status"] == "ACTIVE"


async def test_partial_auto_pause_keeps_open_card_for_the_rest(mock_db, mock_mongo_client, monkeypatch):
    st = Status(monkeypatch, fail={"ad1"})
    t = await seed_rows(mock_db, [("act_1", "g1", "fail"), ("act_2", "g2", "fail")])
    await sync(mock_db, mock_mongo_client, t)
    assert st.calls == [("ad0", "PAUSED")]
    [doc] = await open_docs(mock_db)
    assert doc["rule_id"] == "creative_loser" and doc["auto_failed"] is True
    assert [x["object_id"] for x in doc["action"]["targets"]] == ["ad1"]
    logged = await mock_db[store.ACTIVITY].find({"kind": store.KIND_ACTION_APPLIED}).to_list(None)
    assert [(a["client_group_id"], a["actor"]) for a in logged] == [("g1", "birdy")]
    await sync(mock_db, mock_mongo_client, t)  # once only
    assert st.calls == [("ad0", "PAUSED")]


async def test_auto_pause_logs_once_per_client_group(mock_db, mock_mongo_client, monkeypatch):
    Status(monkeypatch)
    t = await seed_rows(mock_db, [("act_1", "g1", "fail"), ("act_1", "g1", "fail"),
                                  ("act_2", "g2", "fail"), ("act_3", "g3", "pass")])
    await sync(mock_db, mock_mongo_client, t)
    logged = await mock_db[store.ACTIVITY].find({"kind": store.KIND_ACTION_APPLIED}).to_list(None)
    assert sorted(a["client_group_id"] for a in logged) == ["g1", "g2"]
    assert all(a["applied_action_type"] == "pause_ads" for a in logged)


async def test_refailure_after_applied_retry_is_a_new_card(mock_db, mock_mongo_client):
    t, ads = await seed(mock_db, ("pass", "scoring"), post_states={1: "uploading"})
    job = {"id": "j1", "user_id": U, "test_id": t["id"], "test_ad_id": ads[1]["id"],
           "state": "running", "lease_id": "L"}
    await mock_db[POST_JOBS].insert_one(dict(job))
    err = {"code": 100, "subcode": None, "message": "Bad", "step": "ad", "fix": None}
    await posting._fail(mock_db, job, ads[1], err, NOW)
    [first] = await open_docs(mock_db)
    row = await mock_db[TEST_ADS].find_one({"id": ads[1]["id"]})
    assert row["post_error"]["at"] == NOW
    await store.mark_applied(mock_db, U, first["_id"], applied_action_type="retry_creative_post")
    await posting.retry(mock_db, U, t["id"], ads[1]["id"], now=NOW)
    job2 = await mock_db[POST_JOBS].find_one({"test_ad_id": ads[1]["id"]})
    await mock_db[POST_JOBS].update_one({"id": job2["id"]}, {"$set": {"lease_id": "L2"}})
    later = datetime(2026, 11, 3, 13, 0)
    await posting._fail(mock_db, {**job2, "lease_id": "L2"}, ads[1], err, later)
    [second] = await open_docs(mock_db)
    assert second["_id"] != first["_id"] and second["rule_id"] == "creative_post_failed"
    assert (await store.get_suggestion(mock_db, U, first["_id"]))["status"] == "applied"


async def test_close_is_scoped_to_this_test_and_unclaimed(mock_db):
    for sid, test_id, claim in (("a", "t1", None), ("b", "t1", datetime.now(timezone.utc).replace(tzinfo=None)), ("c", "t2", None)):
        await mock_db[store.SUGGESTIONS].insert_one({
            "_id": sid, "id": sid, "user_id": U, "agent": cf.AGENT, "origin_window": "weekly",
            "status": "open", "rule_id": "creative_loser", "applying_at": claim,
            "evidence": {"creative_test_id": test_id}})
    assert await cf.close_test_items(mock_db, U, "t1", set()) == 1
    status = {d["_id"]: d["status"] for d in await mock_db[store.SUGGESTIONS].find({}).to_list(None)}
    assert status == {"a": "resolved", "b": "open", "c": "open"}
