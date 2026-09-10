"""
tests/test_meta_static_scheduling.py
-----------------------------------
The static preset tier, and the starvation it used to suffer.

Meta's cache is refreshed in two tiers: the ongoing presets (today, last_7d,
maximum, …) every minute against a 5-hour cutoff, and the static ones — Last
Month, Last Quarter, Last Year — once a month against a 28-day cutoff, because
those windows only move at period boundaries.

Both tiers decided staleness from the same field, `last_meta_refresh`, and only
one of them could keep it fresh: the every-minute tick writes it on every group
it touches. So the monthly sweep asked for groups untouched for 28 days, was
told there were none, and scheduled nothing — every month, since the tiers were
split. Measured on the live collection: 14 of 16 Meta-connected groups had no
`last_month` spend cached at all, and Last Month / Last Quarter / Last Year read
£0 on all of them.

These pin the fix: each tier reads the stamp its own work writes.
"""

from datetime import datetime, timedelta

import pytest

from core.constants import META_ONGOING_PRESETS, META_STATIC_PRESETS
from services.meta_refresh_manager import schedule_stale_groups


class FakeCursor:
    """Just enough cursor to satisfy the scheduler's find().sort().limit()."""

    def __init__(self, docs):
        self._docs = docs

    def sort(self, field, _direction):
        self._docs = sorted(
            self._docs,
            # Missing sorts first, the way Mongo orders a missing value.
            key=lambda d: (d.get(field) is not None, d.get(field) or datetime.min),
        )
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, _n):
        return self._docs


class FakeGroups:
    def __init__(self, docs):
        self.docs = docs
        self.last_filter = None

    def find(self, filt, _projection=None):
        self.last_filter = filt
        return FakeCursor([d for d in self.docs if self._matches(d, filt)])

    @staticmethod
    def _matches(doc, filt):
        for key, cond in filt.items():
            if key == "$or":
                if not any(FakeGroups._matches(doc, branch) for branch in cond):
                    return False
                continue
            value = doc.get(key)
            if isinstance(cond, dict):
                if "$exists" in cond and (value is not None) != cond["$exists"]:
                    return False
                if "$ne" in cond and value == cond["$ne"]:
                    return False
                if "$lt" in cond and not (value is not None and value < cond["$lt"]):
                    return False
            elif cond is None:
                # {field: None} matches a missing field too, as Mongo does.
                if value is not None:
                    return False
            elif value != cond:
                return False
        return True


class FakeJobsCol:
    """No group is ever busy — open-job filtering is tested elsewhere."""

    def find(self, _filt, _projection=None):
        return FakeCursor([])


class FakeDb:
    def __init__(self, groups):
        self.groups = FakeGroups(groups)
        self.jobs = FakeJobsCol()

    def __getitem__(self, name):
        return self.groups if name == "client_groups" else self.jobs


class FakeMongo:
    def __init__(self, groups):
        self.db = FakeDb(groups)

    def __getitem__(self, _name):
        return self.db


def group(gid, *, ongoing_at=None, static_at=None):
    doc = {
        "id": gid,
        "user_id": "someone@example.com",
        "name": gid,
        "meta_ad_account_id": f"act_{gid}",
        "ad_account_currency": "GBP",
    }
    if ongoing_at is not None:
        doc["last_meta_refresh"] = ongoing_at
    if static_at is not None:
        doc["last_meta_static_refresh"] = static_at
    return doc


@pytest.fixture(autouse=True)
def _no_real_job_writes(monkeypatch):
    created = []

    async def fake_create(**kwargs):
        created.append(kwargs)
        return "job-1"

    monkeypatch.setattr(
        "services.meta_refresh_manager.create_refresh_job", fake_create
    )
    return created


# A group the every-minute tick refreshed moments ago, and whose static presets
# have never been fetched — the live shape of 14 of 16 groups.
JUST_REFRESHED = datetime.utcnow() - timedelta(minutes=2)


@pytest.mark.asyncio
async def test_static_tier_schedules_a_group_the_ongoing_tick_keeps_warm():
    """The bug, stated directly.

    `last_meta_refresh` is two minutes old because meta-tick just ran. Read
    that field and this group is fresh; read the static stamp and it has never
    had a static refresh at all.
    """
    mongo = FakeMongo([group("g1", ongoing_at=JUST_REFRESHED)])

    scheduled = await schedule_stale_groups(
        mongo,
        cutoff_hours=24 * 28,
        presets=META_STATIC_PRESETS,
        stale_field="last_meta_static_refresh",
    )

    assert scheduled == ["g1"]


@pytest.mark.asyncio
async def test_the_old_behaviour_would_have_scheduled_nothing():
    """Kept as the regression's own witness: same group, same cutoff, reading
    the field both tiers used to share."""
    mongo = FakeMongo([group("g1", ongoing_at=JUST_REFRESHED)])

    scheduled = await schedule_stale_groups(
        mongo,
        cutoff_hours=24 * 28,
        presets=META_STATIC_PRESETS,
        stale_field="last_meta_refresh",
    )

    assert scheduled == []


@pytest.mark.asyncio
async def test_a_group_with_fresh_static_data_is_left_alone():
    """Once the sweep lands, it must not re-fetch every minute — the cutoff is
    what keeps this to roughly one refresh a month per group."""
    mongo = FakeMongo([
        group(
            "g1",
            ongoing_at=JUST_REFRESHED,
            static_at=datetime.utcnow() - timedelta(days=3),
        )
    ])

    scheduled = await schedule_stale_groups(
        mongo,
        cutoff_hours=24 * 28,
        presets=META_STATIC_PRESETS,
        stale_field="last_meta_static_refresh",
    )

    assert scheduled == []


@pytest.mark.asyncio
async def test_static_data_older_than_the_cutoff_is_refreshed_again():
    mongo = FakeMongo([
        group(
            "g1",
            ongoing_at=JUST_REFRESHED,
            static_at=datetime.utcnow() - timedelta(days=30),
        )
    ])

    scheduled = await schedule_stale_groups(
        mongo,
        cutoff_hours=24 * 28,
        presets=META_STATIC_PRESETS,
        stale_field="last_meta_static_refresh",
    )

    assert scheduled == ["g1"]


@pytest.mark.asyncio
async def test_the_ongoing_tier_still_reads_its_own_stamp():
    """The default is unchanged, and a never-refreshed static stamp must not
    drag a group into the 5-hour tier."""
    mongo = FakeMongo([group("g1", ongoing_at=JUST_REFRESHED)])

    scheduled = await schedule_stale_groups(
        mongo, cutoff_hours=5, presets=META_ONGOING_PRESETS
    )

    assert scheduled == []


@pytest.mark.asyncio
async def test_each_tier_asks_for_its_own_presets(_no_real_job_writes):
    mongo = FakeMongo([group("g1", ongoing_at=JUST_REFRESHED)])

    await schedule_stale_groups(
        mongo,
        cutoff_hours=24 * 28,
        presets=META_STATIC_PRESETS,
        stale_field="last_meta_static_refresh",
    )

    assert _no_real_job_writes[0]["presets"] == META_STATIC_PRESETS
    assert "last_month" in _no_real_job_writes[0]["presets"]
