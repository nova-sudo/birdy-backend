"""
The GHL opportunity refresh stores won closes for the exact window the weekly
health rule measures (first of the month → previous Sunday), alongside the
usual presets.
"""
from datetime import date

import pytest

import services.ghl_service as ghl_service


class _FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 30)  # Wednesday; previous Sunday is Sep 27


@pytest.mark.asyncio
async def test_refresh_stores_closes_through_the_previous_sunday(mock_mongo_client, mock_db, monkeypatch):
    opps = [
        {"status": "won", "lastStatusChangeAt": "2026-08-31T12:00:00Z"},  # last month
        {"status": "won", "lastStatusChangeAt": "2026-09-01T09:00:00Z"},
        {"status": "won", "lastStatusChangeAt": "2026-09-27T23:00:00Z"},  # Sunday itself
        {"status": "won", "lastStatusChangeAt": "2026-09-29T10:00:00Z"},  # after Sunday
        {"status": "lost", "lastStatusChangeAt": "2026-09-10T10:00:00Z"},
    ]

    async def fake_fetch(location_id, token):
        return True, opps

    monkeypatch.setattr(ghl_service.ghl_integration, "fetch_all_opportunities", fake_fetch)
    monkeypatch.setattr(ghl_service, "date", _FixedDate)
    await mock_db["client_groups"].insert_one({"id": "g1"})

    await ghl_service.cache_ghl_opp_stats_all_presets("g1", "loc", "tok", mock_mongo_client)

    stored = await mock_db["client_groups"].find_one({"id": "g1"})
    window = stored["ghl_opp_cache"]["health_window"]
    assert window["through"] == "2026-09-27"
    assert window["won"] == 2
    assert window["lost"] == 1
