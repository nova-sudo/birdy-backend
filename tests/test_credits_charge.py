"""_apply_charge: draws allowance first, then top-up; writes a ledger row; never goes negative; atomic under races."""

import asyncio

import pytest

import credits

USER = "carol@example.com"


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    credits._settings_cache["data"] = None
    yield
    credits._settings_cache["data"] = None


async def _seed(db, *, allowance=0, used=0.0, topup=0.0):
    c, _ = credits._effective_credits({})
    c.update(allowance=allowance, used=used, topup_balance=topup)
    await db["users"].update_one({"user_id": USER}, {"$set": {"credits": c}}, upsert=True)


async def _credits(db):
    return (await db["users"].find_one({"user_id": USER}))["credits"]


@pytest.mark.asyncio
async def test_allowance_is_used_before_topup(mock_db, monkeypatch):
    # allowance is derived from the subscription; pin it so the seeded value is kept
    monkeypatch.setattr(credits, "_allowance_for", lambda sub: 10)
    await _seed(mock_db, allowance=10, used=4.0, topup=5.0)
    await credits._apply_charge(mock_db, USER, 8.0, ledger={"feature": "x"})
    c = await _credits(mock_db)
    assert c["used"] == 10.0 and c["topup_balance"] == 3.0


@pytest.mark.asyncio
async def test_ledger_row_is_written(mock_db):
    await _seed(mock_db, topup=5.0)
    await credits._apply_charge(mock_db, USER, 1.5, ledger={"source": "t", "feature": "f"})
    row = await mock_db["ai_usage"].find_one({"user_id": USER})
    assert row["source"] == "t" and row["feature"] == "f" and row["credits"] == 1.5 and row["created_at"]


@pytest.mark.asyncio
async def test_charge_larger_than_balance_goes_to_zero(mock_db, monkeypatch):
    monkeypatch.setattr(credits, "_allowance_for", lambda sub: 2)
    await _seed(mock_db, allowance=2, used=0.0, topup=1.0)
    await credits._apply_charge(mock_db, USER, 9.0, ledger={})
    c = await _credits(mock_db)
    assert c["topup_balance"] == 0.0 and credits._available(c) <= 0


@pytest.mark.asyncio
async def test_concurrent_charges_both_land(mock_db, monkeypatch):
    await _seed(mock_db, topup=10.0)
    real = credits._load_and_sync

    async def yielding(db, user_id):
        out = await real(db, user_id)
        await asyncio.sleep(0.01)  # force every racer to read before anyone writes
        return out

    monkeypatch.setattr(credits, "_load_and_sync", yielding)
    await asyncio.gather(*[credits._apply_charge(mock_db, USER, 1.0, ledger={}) for _ in range(5)])
    assert (await _credits(mock_db))["topup_balance"] == 5.0
    assert await mock_db["ai_usage"].count_documents({"user_id": USER}) == 5
