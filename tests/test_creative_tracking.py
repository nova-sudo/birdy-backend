from datetime import date, datetime, timedelta

import pytest

from integrations.facebook_utils.meta_creative_reader import MetaReadError
from jobs import creative_tracking as ct
from services.creatives.store import (
    AD_DAYS, TEST_ADS, TESTS, add_test_ads, create_test, get_test, list_test_ads, update_test,
)

TODAY = date(2026, 11, 3)
TARGETS = {"mode": "thresholds", "metrics": {"cpl": {"op": "lte", "value": 35}}}


def day(ad_id, leads, spend=200, d="2026-10-30"):
    return {"ad_id": ad_id, "date_start": d, "spend": str(spend), "impressions": "10000",
            "clicks": "300", "inline_link_clicks": "250",
            "actions": [{"action_type": "lead", "value": str(leads)}]}


@pytest.fixture(autouse=True)
def _patches(monkeypatch):
    from utils.currency_exchange import CurrencyService

    async def cur(_uid):
        return "GBP"

    monkeypatch.setattr(CurrencyService, "get_user_currency", staticmethod(cur))

    async def token(uid, client):
        return {"access_token": "tok"}

    monkeypatch.setattr(ct, "get_facebook_token", token)


class Meta:
    """Programmable fake of the reader functions."""

    def __init__(self, monkeypatch):
        self.leads = {}      # ad_id -> leads
        self.status = {}     # ad_id -> status / None
        self.account_rows = []
        self.err = {}        # ad_account_id -> exception
        monkeypatch.setattr(ct, "fetch_tracked_ad_days", self.days)
        monkeypatch.setattr(ct, "fetch_ad_status", self.stat)
        monkeypatch.setattr(ct, "fetch_account_daily", self.acct)

    async def days(self, acct, token, ad_ids, since, until):
        if acct in self.err:
            raise self.err[acct]
        return [day(a, self.leads[a]) for a in ad_ids if a in self.leads]

    async def stat(self, ad_id, token):
        return self.status.get(ad_id, "ACTIVE")

    async def acct(self, acct, token, since, until):
        return self.account_rows


async def make_test(db, n_ads=3, targets=TARGETS, uid="u1", accounts=None):
    t = await create_test(db, uid, name="Hook", origin="new", creative={"format": "video"}, targets=targets)
    await update_test(db, uid, t["id"], {"status": "testing", "started_at": datetime(2026, 10, 28, 9)})
    rows = []
    for i in range(n_ads):
        acct = (accounts or [f"act_{i}"] * 1)[i] if accounts else f"act_{i}"
        rows.append({"ad_account_id": acct, "client_group_id": f"g{i}", "adset_id": f"as{i}",
                     "post_state": "active", "meta": {"ad_id": f"{uid}-ad{i}"}})
        await db["client_groups"].insert_one({"id": f"g{i}", "user_id": uid, "meta_ad_account_id": acct,
                                              "ad_account_currency": "GBP"})
    await add_test_ads(db, uid, t["id"], rows)
    return await get_test(db, uid, t["id"])


async def test_track_scores_and_sets_verdict(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"u1-ad0": 10, "u1-ad1": 10, "u1-ad2": 2}
    out = await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    t2 = await get_test(mock_db, "u1", t["id"])
    assert out["verdict"] == "winner"
    assert any(e["kind"] == "verdict" for e in out["changed"])
    assert t2["verdict"] == "winner" and t2["status"] == "winner" and t2["verdict_at"]
    assert t2["summary"]["accounts_pass"] == 2 and t2["summary"]["updated_at"]
    states = sorted(a["score"]["state"] for a in await list_test_ads(mock_db, t["id"], user_id="u1"))
    assert states == ["fail", "pass", "pass"]
    assert {a["currency"] for a in await list_test_ads(mock_db, t["id"], user_id="u1")} == {"GBP"}


async def test_upsert_is_idempotent(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"u1-ad0": 10, "u1-ad1": 10, "u1-ad2": 2}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    n = await mock_db[AD_DAYS].count_documents({})
    await ct.track_test(mock_db, mock_mongo_client, await get_test(mock_db, "u1", t["id"]), today=TODAY)
    assert n == 3 and await mock_db[AD_DAYS].count_documents({}) == n


async def test_removed_ad_is_excluded(mock_db, mock_mongo_client, monkeypatch):  # Review Focus 2
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"u1-ad0": 10, "u1-ad1": 10}
    m.status = {"u1-ad2": None}
    out = await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    ads = {a["meta"]["ad_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id="u1")}
    assert ads["u1-ad2"]["post_state"] == "removed" and ads["u1-ad2"]["score"]["state"] == "removed"
    assert out["verdict"] == "winner"
    assert (await get_test(mock_db, "u1", t["id"]))["summary"]["accounts_total"] == 2


async def test_verdict_at_not_overwritten(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"u1-ad0": 10, "u1-ad1": 10, "u1-ad2": 2}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    first = (await get_test(mock_db, "u1", t["id"]))["verdict_at"]
    out = await ct.track_test(mock_db, mock_mongo_client, await get_test(mock_db, "u1", t["id"]), today=TODAY)
    assert (await get_test(mock_db, "u1", t["id"]))["verdict_at"] == first
    assert out["changed"] == []


async def test_events_on_change(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"u1-ad0": 10, "u1-ad1": 10, "u1-ad2": 10}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    m.leads["u1-ad2"] = 2
    out = await ct.track_test(mock_db, mock_mongo_client, await get_test(mock_db, "u1", t["id"]), today=TODAY)
    assert {"kind": "account_fail", "ad_account_id": "act_2"} in out["changed"]


async def test_pending_ads_not_scored(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n_ads=2)
    await mock_db[TEST_ADS].update_one({"meta.ad_id": "u1-ad1"}, {"$set": {"post_state": "failed"}})
    m.leads = {"u1-ad0": 10, "u1-ad1": 10}
    out = await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    assert out["verdict"] == "testing"
    assert await mock_db[AD_DAYS].count_documents({"ad_id": "u1-ad1"}) == 0


async def test_group_cpl_applies_per_account(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n_ads=2)
    await mock_db["client_groups"].update_one({"id": "g0"}, {"$set": {"targets": {"cpl": 10}}})
    m.leads = {"u1-ad0": 10, "u1-ad1": 10}  # cpl 20: fails g0 (10), passes g1 (35)
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    ads = {a["meta"]["ad_id"]: a["score"]["state"] for a in await list_test_ads(mock_db, t["id"], user_id="u1")}
    assert ads == {"u1-ad0": "fail", "u1-ad1": "pass"}


async def test_baseline_excludes_own_days(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n_ads=1, targets={"mode": "baseline", "baseline_pct": 15})
    m.leads = {"u1-ad0": 10}  # cpl 20
    # account: ad (200, 10 leads) + others (400, 10 leads) -> baseline cpl 40 -> threshold 34
    m.account_rows = [{"date_start": "2026-10-30", "spend": "600", "impressions": "30000",
                       "inline_link_clicks": "600", "actions": [{"action_type": "lead", "value": "20"}]}]
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    ad = (await list_test_ads(mock_db, t["id"], user_id="u1"))[0]
    assert ad["score"]["state"] == "pass" and round(ad["score"]["baseline_cpl"]) == 40


async def test_due_selection_respects_slot(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    now = datetime(2026, 11, 3, 12, 0)  # slot = 01:00 London today (Nov: UTC+0)
    ids = {}
    for uid, tracked in (("a", datetime(2026, 11, 3, 2)), ("b", datetime(2026, 11, 2, 23)), ("c", None)):
        t = await make_test(mock_db, n_ads=1, uid=uid)
        m.leads[f"{uid}-ad0"] = 1
        if tracked:
            await update_test(mock_db, uid, t["id"], {"tracked_at": tracked})
        ids[uid] = t["id"]
    arch = await make_test(mock_db, n_ads=1, uid="d")
    await update_test(mock_db, "d", arch["id"], {"status": "archived"})
    m.leads["d-ad0"] = 1
    out = await ct.track_due_tests(mock_db, mock_mongo_client, now)
    assert out["tracked"] == 2
    assert (await get_test(mock_db, "b", ids["b"]))["tracked_at"] >= now
    assert (await get_test(mock_db, "a", ids["a"]))["tracked_at"] == datetime(2026, 11, 3, 2)
    assert await mock_db[AD_DAYS].count_documents({"ad_id": "d-ad0"}) == 0


async def test_retryable_error_skips_one_test(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t1 = await make_test(mock_db, n_ads=1, uid="a", accounts=["act_bad"])
    t2 = await make_test(mock_db, n_ads=1, uid="b")
    m.err["act_bad"] = MetaReadError("rate", code=17)
    m.leads = {"b-ad0": 5}
    res = await ct.track_due_tests(mock_db, mock_mongo_client, datetime(2026, 11, 3, 12))
    assert res["tracked"] == 1 and res["skipped"] == 1
    assert (await get_test(mock_db, "a", t1["id"])).get("tracked_at") is None
    assert (await get_test(mock_db, "b", t2["id"]))["tracked_at"]


async def test_tick_reconciles_once_per_utc_day(mock_db, mock_mongo_client, monkeypatch):
    calls = []

    async def fake_reconcile(db):
        calls.append(1)
        return {"adoptable": 0}

    monkeypatch.setattr(ct, "reconcile_all", fake_reconcile)
    first = await ct.run_creatives_tick(datetime(2026, 11, 3, 9))
    second = await ct.run_creatives_tick(datetime(2026, 11, 3, 18))
    third = await ct.run_creatives_tick(datetime(2026, 11, 4, 0, 5))
    assert len(calls) == 2 and "reconcile" in first and "reconcile" not in second and "reconcile" in third


async def _run_due(db, mc, now=datetime(2026, 11, 3, 12)):
    return await ct.track_due_tests(db, mc, now)


async def test_non_retryable_error_waits_for_next_slot(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n_ads=1)
    m.err["act_0"] = MetaReadError("bad token", code=190)
    await _run_due(mock_db, mock_mongo_client)
    got = await get_test(mock_db, "u1", t["id"])
    assert got["track_error"]["code"] == 190 and got["tracked_at"]
    res = await _run_due(mock_db, mock_mongo_client, datetime(2026, 11, 3, 12, 1))
    assert res["tracked"] == 0 and res["skipped"] == 0
    # success clears the error at the next slot
    m.err.clear(); m.leads = {"u1-ad0": 5}
    await _run_due(mock_db, mock_mongo_client, datetime(2026, 11, 4, 12))
    assert "track_error" not in await get_test(mock_db, "u1", t["id"])


async def test_no_token_stamps_error(mock_db, mock_mongo_client, monkeypatch):
    Meta(monkeypatch)
    t = await make_test(mock_db, n_ads=1)

    async def none(uid, c):
        return None

    monkeypatch.setattr(ct, "get_facebook_token", none)
    await _run_due(mock_db, mock_mongo_client)
    got = await get_test(mock_db, "u1", t["id"])
    assert got["track_error"] and got["tracked_at"]


async def test_retryable_sets_retry_after_and_blocks_user(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t1 = await make_test(mock_db, n_ads=1, uid="a", accounts=["act_bad"])
    m.err["act_bad"] = MetaReadError("rate", code=17)
    now = datetime(2026, 11, 3, 12)
    res = await _run_due(mock_db, mock_mongo_client, now)
    got = await get_test(mock_db, "a", t1["id"])
    assert got["retry_after"] == now + timedelta(minutes=20) and not got.get("tracked_at")
    res = await _run_due(mock_db, mock_mongo_client, now + timedelta(minutes=5))
    assert res["skipped"] == 0
    m.err.clear(); m.leads = {"a-ad0": 5}
    res = await _run_due(mock_db, mock_mongo_client, now + timedelta(minutes=21))
    assert res["tracked"] == 1


async def test_removed_plus_replacement_same_account_emits_one_fail(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n_ads=1)
    m.leads = {"u1-ad0": 10}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    await add_test_ads(mock_db, "u1", t["id"], [{"ad_account_id": "act_0", "client_group_id": "g0",
                                                 "post_state": "active", "meta": {"ad_id": "u1-new"}}])
    m.status = {"u1-ad0": None}
    m.leads = {"u1-new": 1}
    out = await ct.track_test(mock_db, mock_mongo_client, await get_test(mock_db, "u1", t["id"]), today=TODAY)
    assert [e for e in out["changed"] if e["kind"].startswith("account")] == [
        {"kind": "account_fail", "ad_account_id": "act_0"}]


async def test_verdict_at_kept_on_revert(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"u1-ad0": 10, "u1-ad1": 10, "u1-ad2": 2}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    first = (await get_test(mock_db, "u1", t["id"]))["verdict_at"]
    m.status = {"u1-ad0": None, "u1-ad1": None}
    await ct.track_test(mock_db, mock_mongo_client, await get_test(mock_db, "u1", t["id"]), today=TODAY)
    got = await get_test(mock_db, "u1", t["id"])
    assert got["status"] == "testing" and got["verdict"] is None and got["verdict_at"] == first
