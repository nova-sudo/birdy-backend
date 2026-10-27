"""
The nightly refresh slot: daily Meta / GHL / HotProspector refreshes fall due
once a night at DAILY_REFRESH_LOCAL_HOUR in the account's timezone, so the
morning starts with yesterday complete — not 24 hours after whenever the group
last happened to refresh.
"""

from datetime import datetime

from services.daily_refresh_slot import daily_due_clauses, last_slot_utc


def test_slot_is_last_night_in_london_across_both_offsets():
    # Winter (GMT): 01:00 local is 01:00 UTC.
    assert last_slot_utc(datetime(2026, 12, 3, 9, 0)) == datetime(2026, 12, 3, 1, 0)
    # Before the slot, it is the previous night's.
    assert last_slot_utc(datetime(2026, 12, 3, 0, 30)) == datetime(2026, 12, 2, 1, 0)
    # Summer (BST): 01:00 local is 00:00 UTC.
    assert last_slot_utc(datetime(2026, 7, 10, 0, 0)) == datetime(2026, 7, 10, 0, 0)
    assert last_slot_utc(datetime(2026, 7, 9, 23, 59)) == datetime(2026, 7, 9, 0, 0)


def test_slot_follows_the_users_timezone_and_survives_a_bad_one():
    # 01:00 in New York (EST, UTC-5) is 06:00 UTC.
    assert last_slot_utc(datetime(2026, 12, 3, 7, 0), "America/New_York") == datetime(2026, 12, 3, 6, 0)
    assert last_slot_utc(datetime(2026, 12, 3, 5, 0), "America/New_York") == datetime(2026, 12, 2, 6, 0)
    assert last_slot_utc(datetime(2026, 12, 3, 9, 0), "Not/AZone") == datetime(2026, 12, 3, 1, 0)


async def test_due_clauses_pick_groups_not_refreshed_since_their_slot(mock_db):
    await mock_db["users"].insert_many([
        {"user_id": "uk@x.com", "timezone": "Europe/London"},
        {"user_id": "none@x.com"},
        {"user_id": "ny@x.com", "timezone": "America/New_York"},
    ])
    now = datetime(2026, 12, 3, 3, 0)  # 03:00 UTC: past London's slot, before New York's (06:00)
    await mock_db["client_groups"].insert_many([
        {"id": "uk-stale", "user_id": "uk@x.com", "last_ghl_refresh": datetime(2026, 12, 2, 18, 0)},
        {"id": "uk-fresh", "user_id": "uk@x.com", "last_ghl_refresh": datetime(2026, 12, 3, 1, 5)},
        {"id": "none-stale", "user_id": "none@x.com", "last_ghl_refresh": datetime(2026, 12, 3, 0, 59)},
        {"id": "never", "user_id": "uk@x.com"},
        # Refreshed after New York's previous slot (12-02 06:00 UTC): not due until 06:00 today.
        {"id": "ny-yesterday", "user_id": "ny@x.com", "last_ghl_refresh": datetime(2026, 12, 2, 18, 0)},
        {"id": "ny-stale", "user_id": "ny@x.com", "last_ghl_refresh": datetime(2026, 12, 2, 5, 0)},
    ])

    clauses = await daily_due_clauses(mock_db, "last_ghl_refresh", now)
    due = {g["id"] async for g in mock_db["client_groups"].find({"$or": clauses})}
    assert due == {"uk-stale", "none-stale", "never", "ny-stale"}
