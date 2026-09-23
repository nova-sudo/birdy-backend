"""
tests/test_onboarding_prep_concurrency.py
-----------------------------------------
The review-prep job's fan-out over a GHL agency's sub-accounts.

This is the longest wait in onboarding. It used to walk the sub-accounts one
at a time, each costing a token mint, a location fetch and a contact search,
so an agency with 174 of them sat through 174 round trips in series watching a
bar crawl. The calls do not depend on each other.

Three things have to hold once they run together, and each has a way of going
wrong quietly:

  · the bound is real, or an agency with two hundred sub-accounts opens two
    hundred sockets at GoHighLevel and earns a rate limit the user sees as
    sub-accounts that mysteriously failed
  · progress counts completions, not positions — out-of-order completion with
    a positional counter makes the bar jump backwards
  · one dead sub-account cannot take the batch with it
"""

import asyncio

import pytest

from core.database import DB_NAME
from routers import onboarding

USER = "owner@example.com"


class _Tracker:
    """Records how many probes are in flight at once."""

    def __init__(self):
        self.live = 0
        self.peak = 0
        self.seen = []

    def enter(self, location_id):
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.seen.append(location_id)

    def leave(self):
        self.live -= 1


@pytest.fixture
def prep(monkeypatch, mock_mongo_client):
    """Runs _prepare_review_job against fakes, returning the tracker and db."""
    tracker = _Tracker()
    db = mock_mongo_client[DB_NAME]

    async def _fake_latest_contact(location_id, _token, delay=0.01):
        tracker.enter(location_id)
        try:
            await asyncio.sleep(delay)
            if location_id == "loc_bad":
                raise RuntimeError("this location is broken")
            return {
                "last_lead_at": "2026-09-20T00:00:00Z", "contact_count": 3,
                "leads_30d": 3, "leads_30d_capped": False,
            }
        finally:
            tracker.leave()

    monkeypatch.setattr(onboarding, "_latest_contact", _fake_latest_contact)
    monkeypatch.setattr(onboarding, "_fetch_fb_accounts", lambda *_a: _empty_list())
    monkeypatch.setattr(onboarding, "_ai_match_accounts", lambda *_a: _empty_dict())
    return tracker, db


async def _empty_list():
    return []


async def _empty_dict():
    return {}


async def _wire_ghl(monkeypatch, mock_mongo_client, location_count, extra=()):
    """Agency token, locations and a pre-minted token per location, so the job
    reaches the contact probe without minting anything.

    The user document is created up front because the job's progress writes do
    not upsert — in the real system the account always exists by this point.
    """
    await mock_mongo_client[DB_NAME]["users"].insert_one({"user_id": USER})
    locations = [{"id": f"loc_{i}", "name": f"Client {i}"} for i in range(location_count)]
    locations += [{"id": lid, "name": lid} for lid in extra]

    async def _agency_token(*_a, **_kw):
        return {"company_id": "co_1", "access_token": "agency-token"}

    async def _fetch_locations(*_a, **_kw):
        return True, locations

    async def _subaccount_tokens(*_a, **_kw):
        return {loc["id"]: {"access_token": "tok"} for loc in locations}

    monkeypatch.setattr(onboarding, "get_agency_token", _agency_token)
    monkeypatch.setattr(onboarding, "get_subaccount_tokens", _subaccount_tokens)
    monkeypatch.setattr(onboarding.ghl_integration, "fetch_locations", _fetch_locations)
    monkeypatch.setattr(
        onboarding, "get_mongo_client", lambda: _Ctx(mock_mongo_client)
    )
    return locations


class _Ctx:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *_):
        return False


async def _prep_doc(db):
    user = await db["users"].find_one({"user_id": USER})
    return ((user or {}).get("onboarding") or {}).get("review_prep") or {}


async def test_sub_accounts_are_probed_concurrently(prep, monkeypatch, mock_mongo_client):
    tracker, db = prep
    await _wire_ghl(monkeypatch, mock_mongo_client, 20)

    await onboarding._prepare_review_job(USER)

    assert tracker.peak > 1, "the probes ran one at a time"


async def test_the_fan_out_stays_within_the_bound(prep, monkeypatch, mock_mongo_client):
    """GoHighLevel's rate limit is the ceiling, not ours. Unbounded gather on a
    large agency is how a burst turns into 429s the user reads as failures."""
    tracker, db = prep
    await _wire_ghl(monkeypatch, mock_mongo_client, 60)

    await onboarding._prepare_review_job(USER)

    # Exactly the bound, not merely under it: with sixty sub-accounts and a
    # bound of eight, anything less would mean the semaphore is not what is
    # actually limiting the fan-out, and the assertion would pass for a job
    # that had quietly gone back to running two at a time.
    assert tracker.peak == onboarding.REVIEW_PREP_CONCURRENCY


async def test_every_sub_account_is_still_probed_exactly_once(prep, monkeypatch, mock_mongo_client):
    tracker, db = prep
    await _wire_ghl(monkeypatch, mock_mongo_client, 25)

    await onboarding._prepare_review_job(USER)

    assert sorted(tracker.seen) == sorted(f"loc_{i}" for i in range(25))
    assert len(tracker.seen) == len(set(tracker.seen))


async def test_progress_counts_completions_not_positions(prep, monkeypatch, mock_mongo_client):
    """Out of order now, so a positional counter would let the bar go
    backwards between two polls."""
    tracker, db = prep
    await _wire_ghl(monkeypatch, mock_mongo_client, 25)

    await onboarding._prepare_review_job(USER)

    doc = await _prep_doc(db)
    assert doc["done"] == 25
    assert doc["total"] == 25


async def test_one_broken_sub_account_does_not_take_the_batch_down(prep, monkeypatch, mock_mongo_client):
    tracker, db = prep
    await _wire_ghl(monkeypatch, mock_mongo_client, 9, extra=["loc_bad"])

    await onboarding._prepare_review_job(USER)

    doc = await _prep_doc(db)
    assert doc["status"] == "complete"
    assert doc["done"] == 10
    assert doc["accounts"]["loc_bad"]["error"]
    assert doc["accounts"]["loc_0"]["leads_30d"] == 3


async def test_a_failed_probe_is_recorded_as_unknown_not_as_no_leads(prep, monkeypatch, mock_mongo_client):
    """The review endpoint reads the error to tell "we could not look" apart
    from "we looked and there is nothing" — the second pre-labels a client
    Inactive."""
    tracker, db = prep
    await _wire_ghl(monkeypatch, mock_mongo_client, 1, extra=["loc_bad"])

    await onboarding._prepare_review_job(USER)

    bad = (await _prep_doc(db))["accounts"]["loc_bad"]
    assert bad["error"]
    assert bad["last_lead_at"] is None
