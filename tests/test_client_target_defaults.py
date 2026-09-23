"""
tests/test_client_target_defaults.py
------------------------------------
The agency defaults a new client group inherits.

The wizard's "Save these as your defaults?" step promises, in as many words,
that every new client is pre-filled with those numbers. Nothing read them —
they were written to users.default_targets and never looked at again. What
that cost is specific: `monthly_wins` is the field the weekly health pass
measures a client against, a client with no target has no expectation to
miss, and no expectation resolves to Healthy. An agency that set targets once
and then bulk-imported twenty-four sub-accounts got twenty-four accounts that
were silently unmonitored.

So what is pinned here is that the defaults arrive, that nothing but a real
goal comes with them, and that a lookup failure costs the client its targets
rather than its creation.
"""

import pytest

from core.database import DB_NAME
from services import client_targets

USER = "owner@example.com"


@pytest.fixture
def db(mock_mongo_client):
    return mock_mongo_client[DB_NAME]


async def test_defaults_reach_a_new_client(db):
    await db["users"].insert_one(
        {"user_id": USER, "default_targets": {"cpa": 120, "monthly_wins": 12}}
    )

    assert await client_targets.defaults_for(USER, db) == {"cpa": 120, "monthly_wins": 12}


async def test_an_agency_with_no_defaults_gets_an_empty_object(db):
    await db["users"].insert_one({"user_id": USER})

    assert await client_targets.defaults_for(USER, db) == {}


async def test_an_unknown_user_gets_an_empty_object(db):
    assert await client_targets.defaults_for("nobody@example.com", db) == {}


async def test_bookkeeping_and_unknown_keys_do_not_travel(db):
    """`updated_at` is not a goal, and a stray key must never be mistaken for
    one — `hasAnyTarget` in the wizard reads this shape to decide whether a
    client has been given a target at all."""
    await db["users"].insert_one({"user_id": USER, "default_targets": {
        "monthly_wins": 12, "updated_at": "2026-01-01", "made_up": 5,
    }})

    assert await client_targets.defaults_for(USER, db) == {"monthly_wins": 12}


async def test_an_unset_default_is_omitted_rather_than_stored_as_null(db):
    """A null target is not the same as a target of nothing: the Targets tab
    would render it as a filled-in blank and the health pass would read it as
    a goal that exists."""
    await db["users"].insert_one(
        {"user_id": USER, "default_targets": {"cpa": None, "monthly_wins": 12}}
    )

    assert await client_targets.defaults_for(USER, db) == {"monthly_wins": 12}


async def test_a_failed_lookup_costs_the_targets_not_the_client():
    class _Exploding:
        def __getitem__(self, _name):
            return self

        async def find_one(self, *_a, **_kw):
            raise RuntimeError("mongo is down")

    assert await client_targets.defaults_for(USER, _Exploding()) == {}


def test_cpa_and_cpl_are_both_real_and_separate_fields():
    """They differ by the width of the funnel. Folding one into the other is
    what put an agency's cost-per-acquisition answer on the dashboard under a
    cost-per-lead label."""
    assert "cpa" in client_targets.TARGET_FIELDS
    assert "cpl" in client_targets.TARGET_FIELDS


def test_the_api_accepts_every_field_the_canonical_list_names():
    """The wizard's request model is built from this list. A name in one and
    not the other is the failure mode the list exists to prevent: an unknown
    field 422s the whole PUT, taking the valid targets down with it."""
    from routers.onboarding import TargetsRequest

    for field in client_targets.TARGET_FIELDS:
        assert field in TargetsRequest.model_fields
