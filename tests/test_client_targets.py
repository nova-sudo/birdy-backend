"""
tests/test_client_targets.py
----------------------------
Per-client monthly goals.

The important property is that a save MERGES. Two surfaces write these — the
onboarding wizard collects three fields, the Client Detail settings modal
collects six — and the endpoint used to replace the whole `targets` object, so
whichever saved last blanked the other's fields. `monthly_wins` drives the
health band, and a blanked target reads as "no target", which resolves to
Healthy: an account would quietly stop being monitored.
"""

import contextlib

import pytest
from fastapi import HTTPException

import routers.onboarding as onboarding
from core.models import *  # noqa: F401,F403  (kept parallel with the router's own imports)
from routers.onboarding import TargetsRequest

USER = "user@example.com"
OTHER = "someone@else.com"


@pytest.fixture
def targets_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(onboarding, "get_mongo_client", fake_client)
    return onboarding


async def a_group(db, group_id="g1", user_id=USER, targets=None):
    doc = {"id": group_id, "user_id": user_id, "name": "Aura"}
    if targets is not None:
        doc["targets"] = targets
    await db["client_groups"].insert_one(doc)


async def save(group_id="g1", user=USER, **fields):
    return await onboarding.set_client_targets(
        group_id, TargetsRequest(**fields), current_user=user
    )


# ── merging ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_partial_save_keeps_the_fields_it_did_not_send(targets_api, mock_db):
    await a_group(mock_db, targets={"monthly_wins": 12, "cpa": 40})

    await save(cpl=9.5)

    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["cpl"] == 9.5
    assert stored["monthly_wins"] == 12, "the health target must survive"
    assert stored["cpa"] == 40


@pytest.mark.asyncio
async def test_the_onboarding_wizard_cannot_blank_the_settings_fields(targets_api, mock_db):
    """The exact collision this endpoint used to lose."""
    await a_group(mock_db)
    await save(cpl=9, monthly_revenue=50000, monthly_spend=8000, aov=1200)

    # Wizard saves only its three.
    await save(cpa=40, monthly_wins=12, conversion_rate=0.25)

    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["monthly_revenue"] == 50000
    assert stored["monthly_spend"] == 8000
    assert stored["aov"] == 1200
    assert stored["monthly_wins"] == 12


@pytest.mark.asyncio
async def test_a_later_save_overwrites_the_same_field(targets_api, mock_db):
    await a_group(mock_db)
    await save(monthly_wins=10)
    await save(monthly_wins=20)

    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["monthly_wins"] == 20


@pytest.mark.asyncio
async def test_all_six_design_fields_round_trip(targets_api, mock_db):
    await a_group(mock_db)

    out = await save(
        cpl=9.5, monthly_wins=12, monthly_revenue=50000,
        conversion_rate=0.25, monthly_spend=8000, aov=1200,
    )

    for field, value in [
        ("cpl", 9.5), ("monthly_wins", 12), ("monthly_revenue", 50000),
        ("conversion_rate", 0.25), ("monthly_spend", 8000), ("aov", 1200),
    ]:
        assert out["targets"][field] == value


@pytest.mark.asyncio
async def test_zero_is_a_real_target_not_an_omission(targets_api, mock_db):
    """Only `None` means "not sent" — 0 is a value someone chose."""
    await a_group(mock_db, targets={"monthly_wins": 12})

    await save(monthly_wins=0)

    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["monthly_wins"] == 0


@pytest.mark.asyncio
async def test_the_response_returns_the_merged_result(targets_api, mock_db):
    await a_group(mock_db, targets={"cpa": 40})

    out = await save(cpl=9.5)

    assert out["targets"]["cpa"] == 40
    assert out["targets"]["cpl"] == 9.5


# ── validation and scoping ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_save_with_no_fields_is_rejected(targets_api, mock_db):
    await a_group(mock_db, targets={"monthly_wins": 12})

    with pytest.raises(HTTPException) as exc:
        await save()
    assert exc.value.status_code == 400

    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["monthly_wins"] == 12


@pytest.mark.asyncio
async def test_another_users_group_is_not_found(targets_api, mock_db):
    await a_group(mock_db, user_id=OTHER, targets={"monthly_wins": 12})

    with pytest.raises(HTTPException) as exc:
        await save(monthly_wins=99)
    assert exc.value.status_code == 404

    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["monthly_wins"] == 12


@pytest.mark.asyncio
async def test_unknown_group_is_404(targets_api, mock_db):
    with pytest.raises(HTTPException) as exc:
        await save("nope", monthly_wins=1)
    assert exc.value.status_code == 404


# ── agency defaults ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_as_default_stores_the_agency_default(targets_api, mock_db):
    await a_group(mock_db)

    out = await save(monthly_wins=12, save_as_default=True)

    user = await mock_db["users"].find_one({"user_id": USER})
    assert user["default_targets"]["monthly_wins"] == 12
    assert out["saved_as_default"] is True


@pytest.mark.asyncio
async def test_defaults_merge_too(targets_api, mock_db):
    await a_group(mock_db)
    await mock_db["users"].insert_one(
        {"user_id": USER, "default_targets": {"cpa": 40}}
    )

    await save(monthly_wins=12, save_as_default=True)

    user = await mock_db["users"].find_one({"user_id": USER})
    assert user["default_targets"] == {"cpa": 40, "monthly_wins": 12}


@pytest.mark.asyncio
async def test_defaults_are_untouched_without_the_flag(targets_api, mock_db):
    await a_group(mock_db)

    await save(monthly_wins=12)

    user = await mock_db["users"].find_one({"user_id": USER})
    assert user is None or "default_targets" not in user


# ── the link to health ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_saved_closes_target_is_what_health_reads(targets_api, mock_db):
    from datetime import date
    from services.client_health import health_for_group, CRITICAL

    await a_group(mock_db)
    await save(monthly_wins=100)

    group = await mock_db["client_groups"].find_one({"id": "g1"})
    result = health_for_group(group, through=date(2026, 8, 15))

    assert result["close_target"] == 100
    assert result["health"] == CRITICAL   # no closes cached against a 100 target
