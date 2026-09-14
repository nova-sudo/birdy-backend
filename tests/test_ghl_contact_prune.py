"""
tests/test_ghl_contact_prune.py
-------------------------------
The prune at the end of a GHL FULL LOAD is the only step in the sync that
deletes anything, and it got one real client badly wrong: Aura went from 3,247
contacts to 1,747 in a single run that reported "0 new, 3247 updated".

The cause was concurrency, not pagination. The create/import path started a
FULL LOAD inline; the every-minute ghl-tick saw a group with no
`last_ghl_refresh` — maximally stale — and started a second one against the
same location. Each stamped its own `sync_batch_id`, and the one that finished
first deleted every row the other had just written, because "not tagged with my
batch id" was the whole definition of stale.

These tests pin the fixed definition: a row is stale only if this run didn't
stamp it *and* nobody wrote it since this run began.
"""

from datetime import datetime, timedelta

import pytest

from services.ghl_service import stale_contact_filter

USER = "hello@soupgrowth.com"
OTHER_USER = "hadley@soupgrowth.com"
LOCATION = "VhG8MijwGfHn3C0F2nO5"
MY_BATCH = "batch-mine"
RUN_STARTED = datetime(2026, 9, 14, 12, 0, 0)


def _contact(contact_id, *, user_id=USER, location_id=LOCATION, batch=MY_BATCH, updated_at=RUN_STARTED, **extra):
    doc = {
        "user_id": user_id,
        "location_id": location_id,
        "contact_id": contact_id,
        "sync_batch_id": batch,
        "updated_at": updated_at,
    }
    doc.update(extra)
    return doc


async def _surviving(db, docs):
    await db["ghl_contacts"].insert_many(docs)
    await db["ghl_contacts"].delete_many(
        stale_contact_filter(USER, LOCATION, MY_BATCH, RUN_STARTED)
    )
    return {d["contact_id"] async for d in db["ghl_contacts"].find({})}


@pytest.mark.asyncio
async def test_a_contact_from_an_older_run_is_pruned(mock_db):
    """The prune's actual job: GHL stopped returning this person, so drop them."""
    survivors = await _surviving(mock_db, [
        _contact("still-here"),
        _contact("gone-from-ghl", batch="batch-last-week",
                 updated_at=RUN_STARTED - timedelta(days=7)),
    ])
    assert survivors == {"still-here"}


@pytest.mark.asyncio
async def test_a_concurrent_load_s_contacts_survive(mock_db):
    """
    The Aura bug, in one assertion.

    A second FULL LOAD of the same location wrote these rows seconds ago under
    its own batch id. They are the freshest data we have. The old filter
    deleted all 1,500 of them; this one must keep every one.
    """
    concurrent = [
        _contact(f"c{i}", batch="batch-theirs", updated_at=RUN_STARTED + timedelta(seconds=30))
        for i in range(1500)
    ]
    survivors = await _surviving(mock_db, [_contact("mine")] + concurrent)
    assert len(survivors) == 1501
    assert "c0" in survivors and "c1499" in survivors


@pytest.mark.asyncio
async def test_a_row_written_at_the_instant_the_run_began_survives(mock_db):
    """Ties go to the contact. Deleting live data is the expensive mistake here;
    keeping a stale row costs one extra cycle."""
    survivors = await _surviving(mock_db, [
        _contact("exactly-now", batch="batch-theirs", updated_at=RUN_STARTED),
    ])
    assert survivors == {"exactly-now"}


@pytest.mark.asyncio
async def test_a_legacy_row_with_no_updated_at_is_still_prunable(mock_db):
    """`$not`/`$gte`, not `$lt` — a plain `$lt` never matches a missing field,
    which would leave pre-`updated_at` rows in the database forever."""
    doc = _contact("ancient", batch="batch-2024")
    del doc["updated_at"]
    survivors = await _surviving(mock_db, [doc, _contact("mine")])
    assert survivors == {"mine"}


@pytest.mark.asyncio
async def test_another_birdy_account_s_copy_of_this_location_is_untouched(mock_db):
    """Two accounts legitimately hold the same GHL location. Neither one's
    prune may reach across into the other's rows."""
    survivors = await _surviving(mock_db, [
        _contact("mine"),
        _contact("theirs", user_id=OTHER_USER, batch="batch-hadley",
                 updated_at=RUN_STARTED - timedelta(days=7)),
    ])
    assert survivors == {"mine", "theirs"}


@pytest.mark.asyncio
async def test_a_different_location_is_untouched(mock_db):
    survivors = await _surviving(mock_db, [
        _contact("mine"),
        _contact("elsewhere", location_id="some-other-location",
                 batch="batch-old", updated_at=RUN_STARTED - timedelta(days=7)),
    ])
    assert survivors == {"mine", "elsewhere"}
