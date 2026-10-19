"""
tests/test_dashboards_query.py
------------------------------
POST /api/dashboards/query: dashkit queries -> dashkit Frames, computed from
seeded client groups (daily rows + preset caches) in an in-memory Mongo.

The frame maths mirror dashkit's reference data source
(src/adapters/mock-data-source.ts): pivoting, Σnum÷Σden ratios, top-N for
over+by, ISO-week / month bucket keys. The previous period follows the spec
(this_* windows clamp at the previous period's end; the dashkit mock does not).
"""

import asyncio
from datetime import date, timedelta
from typing import Dict, List, Literal, Optional

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError

from core.constants import PRESET_ALIAS, previous_window, window_range
from dependencies import get_current_user
from main import app
from services.dashboards_query import run_queries

USER = "alice@example.com"
TODAY = date(2026, 10, 20)  # a Tuesday
HISTORY_START = date(2024, 1, 1)
GAP = date(2026, 10, 17)  # Beta has no row this day
COBALT_START = date(2026, 10, 1)


def _days(start: date, end: date) -> List[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _iso(d: date) -> str:
    return d.isoformat()


# --- seeded data ----------------------------------------------------------------
# Acme:   every day since 2024-01-01; spend = day of month, 1000 impressions, 10 clicks, 2 results.
# Beta:   every day since 2024-01-01 except GAP; spend 1, 500 impressions, 5 clicks, 0 results.
# Cobalt: every day since 2026-10-01; spend 100, 100 impressions, 2 clicks, 1 result.

def _acme_row(d):
    return {"date": _iso(d), "spend": d.day, "impressions": 1000, "clicks": 10, "results": 2, "leads": 2}


def _beta_row(d):
    return {"date": _iso(d), "spend": 1, "impressions": 500, "clicks": 5, "results": 0, "leads": 0}


def _cobalt_row(d):
    return {"date": _iso(d), "spend": 100, "impressions": 100, "clicks": 2, "results": 1, "leads": 1}


def _insights(**values):
    return {"metrics": {"insights": values}}


def _group(gid, name, rows, presets, user=USER, status="Active", **extra):
    return {
        "id": gid, "name": name, "user_id": user, "client_status": status,
        "facebook_cache": {"presets": presets},
        "meta_daily_spend": rows,
        **extra,
    }


def _seed_groups():
    return [
        _group("g1", "Acme", [_acme_row(d) for d in _days(HISTORY_START, TODAY)], {
            "maximum": _insights(spend=5000, reach=300),
            "this_month": _insights(spend=210, reach=40),
            "today": _insights(spend=20, reach=4),
            "yesterday": _insights(spend=19, reach=3),
        }, hp_daily_calls=[{"date": _iso(TODAY), "calls": 9}]),
        _group("g2", "Beta", [_beta_row(d) for d in _days(HISTORY_START, TODAY) if d != GAP], {
            "maximum": _insights(spend=1000, reach=200),
            "this_month": _insights(spend=19, reach=20),
            "today": _insights(spend=1, reach=2),
            "yesterday": _insights(spend=1, reach=1),
        }),
        _group("g3", "Cobalt", [_cobalt_row(d) for d in _days(COBALT_START, TODAY)], {
            "maximum": _insights(spend=2000, reach=100),
            "this_month": _insights(spend=2000, reach=10),
        }),
        _group("g4", "Dormant", [_acme_row(TODAY)], {"maximum": _insights(spend=999)}, status="Inactive"),
        _group("g5", "Someone else", [_acme_row(TODAY)], {"maximum": _insights(spend=777)}, user="bob@example.com"),
    ]


def _spend(gid: str, start: date, end: date) -> float:
    days = _days(start, end)
    if gid == "g1":
        return float(sum(d.day for d in days if d >= HISTORY_START))
    if gid == "g2":
        return float(sum(1 for d in days if d >= HISTORY_START and d != GAP))
    return float(sum(100 for d in days if d >= COBALT_START))


def _spend_all(start, end, gids=("g1", "g2", "g3")):
    return sum(_spend(g, start, end) for g in gids)


@pytest.fixture
async def db(mock_db):
    await mock_db["client_groups"].insert_many(_seed_groups())
    return mock_db


async def _run(db, *queries, today=TODAY):
    return await run_queries(db, USER, list(queries), today=today)


async def _one(db, query, today=TODAY):
    frames = await _run(db, query, today=today)
    assert frames[0] is not None
    return frames[0]


def _q(*metrics, preset="last_7d", **kw):
    window = kw.pop("window", {"preset": preset})
    return {"metrics": [{"metric": m} for m in metrics], "window": window, **kw}


SPEND_COLUMN = {"key": "spend", "label": "Spend", "format": "currency", "polarity": "higher_is_better"}
LAST_7D = (date(2026, 10, 14), TODAY)


# --- totals ---------------------------------------------------------------------

async def test_totals_only_query(db):
    frame = await _one(db, _q("spend"))
    assert frame == {
        "columns": [SPEND_COLUMN],
        "rows": [],
        "totals": {"values": {"spend": _spend_all(*LAST_7D)}},
        "previousAvailable": True,
        "window": {"label": "Last 7 days", "from": "2026-10-14", "to": "2026-10-20"},
    }
    assert frame["totals"]["values"]["spend"] == 119 + 6 + 700


async def test_metric_label_override_and_query_order(db):
    query = {"metrics": [{"metric": "leads", "label": "Leads in"}, {"metric": "spend"}], "window": {"preset": "last_7d"}}
    frame = await _one(db, query)
    assert [c["key"] for c in frame["columns"]] == ["leads", "spend"]
    assert frame["columns"][0]["label"] == "Leads in"
    assert frame["totals"]["values"] == {"leads": 14 + 0 + 7, "spend": 825}


# --- by client ------------------------------------------------------------------

async def test_by_client_sort_limit(db):
    unsorted = await _one(db, _q("spend", "leads", by="client"))
    assert [r["key"] for r in unsorted["rows"]] == ["g1", "g2", "g3"]  # by name when no sort
    assert unsorted["rows"][0] == {"key": "g1", "label": "Acme", "values": {"spend": 119, "leads": 14}}

    frame = await _one(db, _q("spend", "leads", by="client", sort={"metric": "spend", "dir": "asc"}, limit=2))
    assert [r["label"] for r in frame["rows"]] == ["Beta", "Acme"]
    # totals cover every client, not only the returned rows
    assert frame["totals"]["values"] == {"spend": 825, "leads": 21}

    desc = await _one(db, _q("spend", by="client", sort={"metric": "spend", "dir": "desc"}, limit=1))
    assert [r["key"] for r in desc["rows"]] == ["g3"]


async def test_by_client_sort_puts_nulls_last_both_ways(db):
    asc = await _one(db, _q("cpl", by="client", sort={"metric": "cpl", "dir": "asc"}))
    desc = await _one(db, _q("cpl", by="client", sort={"metric": "cpl", "dir": "desc"}))
    assert [r["key"] for r in asc["rows"]] == ["g1", "g3", "g2"]
    assert [r["key"] for r in desc["rows"]] == ["g3", "g1", "g2"]
    assert asc["rows"][-1]["values"] == {"cpl": None}


async def test_by_client_previous_per_row(db):
    frame = await _one(db, _q("spend", by="client", compare="previous"))
    prev = (date(2026, 10, 7), date(2026, 10, 13))
    rows = {r["key"]: r for r in frame["rows"]}
    assert rows["g1"]["previous"] == {"spend": _spend("g1", *prev)}
    assert rows["g2"]["previous"] == {"spend": _spend("g2", *prev)}
    assert rows["g3"]["previous"] == {"spend": _spend("g3", *prev)}
    assert frame["totals"]["previous"] == {"spend": _spend_all(*prev)}


# --- over -----------------------------------------------------------------------

async def test_over_week_iso_monday(db):
    start, end = date(2026, 9, 24), TODAY  # a Thursday
    frame = await _one(db, _q("spend", window={"from": _iso(start), "to": _iso(end)}, over="week"))
    keys = [r["key"] for r in frame["rows"]]
    assert keys == ["2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"]
    assert all(date.fromisoformat(k).weekday() == 0 for k in keys)
    assert [r["label"] for r in frame["rows"]] == keys
    # the first bucket holds only the window's days (Thu 24 - Sun 27)
    assert frame["rows"][0]["values"] == {"spend": _spend_all(start, date(2026, 9, 27))}
    assert frame["rows"][3]["values"] == {"spend": _spend_all(date(2026, 10, 12), date(2026, 10, 18))}
    assert frame["rows"][4]["values"] == {"spend": _spend_all(date(2026, 10, 19), end)}
    assert frame["totals"]["values"] == {"spend": _spend_all(start, end)}
    assert frame["window"] == {"label": "2026-09-24 – 2026-10-20", "from": "2026-09-24", "to": "2026-10-20"}


async def test_over_month(db):
    start = date(2026, 8, 15)
    frame = await _one(db, _q("spend", window={"from": _iso(start), "to": _iso(TODAY)}, over="month"))
    assert [r["key"] for r in frame["rows"]] == ["2026-08-01", "2026-09-01", "2026-10-01"]
    assert frame["rows"][0]["values"]["spend"] == _spend_all(start, date(2026, 8, 31))
    assert frame["rows"][1]["values"]["spend"] == _spend_all(date(2026, 9, 1), date(2026, 9, 30))
    assert frame["rows"][2]["values"]["spend"] == _spend_all(date(2026, 10, 1), TODAY)


async def test_over_ignores_sort_and_runs_time_ascending(db):
    frame = await _one(db, _q("spend", over="day", sort={"metric": "spend", "dir": "desc"}, limit=2))
    assert [r["key"] for r in frame["rows"]] == [_iso(d) for d in _days(*LAST_7D)]


async def test_over_previous_aligns_buckets_by_index(db):
    frame = await _one(db, _q("spend", over="day", compare="previous"))
    prev_days = _days(date(2026, 10, 7), date(2026, 10, 13))
    for row, prev_day in zip(frame["rows"], prev_days):
        assert row["previous"] == {"spend": _spend_all(prev_day, prev_day)}
    assert frame["totals"]["previous"] == {"spend": _spend_all(prev_days[0], prev_days[-1])}


async def test_over_by_top_n(db):
    frame = await _one(db, _q("spend", preset="last_30d", over="week", by="client", compare="previous"))
    window = (date(2026, 9, 21), TODAY)
    # ranked by the window total: Cobalt 2000, Acme, Beta
    assert frame["columns"] == [
        {"key": "g3", "label": "Cobalt", "format": "currency", "polarity": "higher_is_better"},
        {"key": "g1", "label": "Acme", "format": "currency", "polarity": "higher_is_better"},
        {"key": "g2", "label": "Beta", "format": "currency", "polarity": "higher_is_better"},
    ]
    assert frame["totals"]["values"] == {g: _spend(g, *window) for g in ("g1", "g2", "g3")}
    first = frame["rows"][0]
    assert first["key"] == "2026-09-21"
    assert first["values"] == {g: _spend(g, date(2026, 9, 21), date(2026, 9, 27)) for g in ("g1", "g2", "g3")}
    assert set(first["previous"]) == {"g1", "g2", "g3"}

    top2 = await _one(db, _q("spend", preset="last_30d", over="week", by="client", limit=2))
    assert [c["key"] for c in top2["columns"]] == ["g3", "g1"]
    assert all(set(r["values"]) == {"g3", "g1"} for r in top2["rows"])


async def test_over_by_defaults_to_5_and_caps_at_8(db):
    await db["client_groups"].insert_many([
        _group(f"x{i}", f"Extra {i}", [{"date": _iso(TODAY), "spend": 1000 + i}], {}) for i in range(10)
    ])
    default = await _one(db, _q("spend", over="day", by="client"))
    assert [c["key"] for c in default["columns"]] == ["x9", "x8", "x7", "x6", "x5"]
    capped = await _one(db, _q("spend", over="day", by="client", limit=20))
    assert len(capped["columns"]) == 8


async def test_over_by_takes_exactly_one_metric(db):
    frames = await _run(db, _q("spend", "leads", over="day", by="client"))
    assert frames == [None]


# --- ratios ---------------------------------------------------------------------

async def test_ratio_totals_recompute(db):
    frame = await _one(db, _q("cpl", "ctr", by="client"))
    spend, results = 825, 14 + 0 + 7
    assert frame["totals"]["values"]["cpl"] == pytest.approx(spend / results)
    # ctr is Σclicks ÷ Σimpressions on a 0-100 scale, not the average of the clients' ctrs (1, 1, 2)
    assert frame["totals"]["values"]["ctr"] == pytest.approx(100 * (70 + 30 + 14) / (7000 + 3000 + 700))
    rows = {r["key"]: r["values"] for r in frame["rows"]}
    assert rows["g1"]["cpl"] == pytest.approx(119 / 14)
    assert rows["g2"]["cpl"] is None  # zero leads: no cost per lead, never 0
    assert rows["g3"]["cpl"] == pytest.approx(100.0)
    assert rows["g3"]["ctr"] == pytest.approx(2.0)


async def test_ratio_buckets_and_previous_recompute(db):
    frame = await _one(db, _q("cpl", over="day", compare="previous"))
    by_day = {r["key"]: r for r in frame["rows"]}
    gap = by_day[_iso(GAP)]
    assert gap["values"]["cpl"] == pytest.approx((17 + 100) / (2 + 1))
    assert frame["totals"]["previous"]["cpl"] == pytest.approx(
        _spend_all(date(2026, 10, 7), date(2026, 10, 13)) / (2 * 7 + 7))

    beta = await _one(db, _q("cpl", over="day", filters={"client": ["g2"]}))
    assert all(r["values"]["cpl"] is None for r in beta["rows"])
    assert beta["totals"]["values"]["cpl"] is None


# --- previous period --------------------------------------------------------------

# preset -> (current from, to), (previous from, to) | None, for today = 2026-10-20 (Tuesday)
_WINDOWS_2026_10_20 = {
    "maximum":             (None, None),
    "data_maximum":        (None, None),
    "today":               (("2026-10-20", "2026-10-20"), ("2026-10-19", "2026-10-19")),
    "yesterday":           (("2026-10-19", "2026-10-19"), ("2026-10-18", "2026-10-18")),
    "this_week":           (("2026-10-19", "2026-10-20"), ("2026-10-12", "2026-10-13")),
    "this_week_mon_today": (("2026-10-19", "2026-10-20"), ("2026-10-12", "2026-10-13")),
    "last_7d":             (("2026-10-14", "2026-10-20"), ("2026-10-07", "2026-10-13")),
    "last_14d":            (("2026-10-07", "2026-10-20"), ("2026-09-23", "2026-10-06")),
    "last_30d":            (("2026-09-21", "2026-10-20"), ("2026-08-22", "2026-09-20")),
    "this_month":          (("2026-10-01", "2026-10-20"), ("2026-09-01", "2026-09-20")),
    "last_month":          (("2026-09-01", "2026-09-30"), ("2026-08-01", "2026-08-31")),
    "this_quarter":        (("2026-10-01", "2026-10-20"), ("2026-07-01", "2026-07-20")),
    "last_quarter":        (("2026-07-01", "2026-09-30"), ("2026-04-01", "2026-06-30")),
    "this_year":           (("2026-01-01", "2026-10-20"), ("2025-01-01", "2025-10-20")),
    "last_year":           (("2025-01-01", "2025-12-31"), ("2024-01-01", "2024-12-31")),
}


def test_previous_table_covers_every_preset():
    assert set(_WINDOWS_2026_10_20) == set(PRESET_ALIAS)


@pytest.mark.parametrize("window", [{"preset": p} for p in PRESET_ALIAS] + [{"from": "2026-10-01", "to": "2026-10-10"}],
                         ids=lambda w: w.get("preset", "custom"))
async def test_previous_every_window(db, window):
    if "preset" in window:
        current, previous = _WINDOWS_2026_10_20[window["preset"]]
    else:
        current, previous = ("2026-10-01", "2026-10-10"), ("2026-09-21", "2026-09-30")
    expected_prev = None if previous is None else tuple(date.fromisoformat(d) for d in previous)
    assert previous_window(window, today=TODAY) == expected_prev

    frame = await _one(db, {"metrics": [{"metric": "spend"}], "window": window,
                            "compare": "previous", "filters": {"client": ["g1", "g2"]}})
    if previous is None:
        assert frame["previousAvailable"] is False
        assert "previous" not in frame["totals"]
        return
    assert (frame["window"]["from"], frame["window"]["to"]) == current
    assert window_range(window, today=TODAY) == tuple(date.fromisoformat(d) for d in current)
    assert frame["previousAvailable"] is True
    assert frame["totals"]["previous"] == {"spend": _spend_all(*expected_prev, gids=("g1", "g2"))}


@pytest.mark.parametrize("today, preset, current, previous", [
    # this_* windows are cut to the elapsed length, then clamped at the previous period's end
    ("2026-03-31", "this_month",   ("2026-03-01", "2026-03-31"), ("2026-02-01", "2026-02-28")),
    ("2026-03-30", "this_month",   ("2026-03-01", "2026-03-30"), ("2026-02-01", "2026-02-28")),
    ("2028-03-31", "this_month",   ("2028-03-01", "2028-03-31"), ("2028-02-01", "2028-02-29")),  # leap year
    ("2025-05-31", "this_month",   ("2025-05-01", "2025-05-31"), ("2025-04-01", "2025-04-30")),
    ("2026-01-31", "this_month",   ("2026-01-01", "2026-01-31"), ("2025-12-01", "2025-12-31")),
    ("2026-03-31", "this_quarter", ("2026-01-01", "2026-03-31"), ("2025-10-01", "2025-12-29")),
    ("2026-06-30", "this_quarter", ("2026-04-01", "2026-06-30"), ("2026-01-01", "2026-03-31")),  # 91 vs 90 days
    ("2026-12-31", "this_quarter", ("2026-10-01", "2026-12-31"), ("2026-07-01", "2026-09-30")),
    ("2028-12-31", "this_year",    ("2028-01-01", "2028-12-31"), ("2027-01-01", "2027-12-31")),  # 366 vs 365 days
    ("2026-03-31", "this_week",    ("2026-03-30", "2026-03-31"), ("2026-03-23", "2026-03-24")),
    ("2026-03-31", "last_month",   ("2026-02-01", "2026-02-28"), ("2026-01-01", "2026-01-31")),
    ("2026-03-31", "last_quarter", ("2025-10-01", "2025-12-31"), ("2025-07-01", "2025-09-30")),
    ("2026-03-01", "yesterday",    ("2026-02-28", "2026-02-28"), ("2026-02-27", "2026-02-27")),
])
def test_previous_window_clamps_at_period_end(today, preset, current, previous):
    t = date.fromisoformat(today)
    as_dates = lambda pair: tuple(date.fromisoformat(d) for d in pair)  # noqa: E731
    assert window_range({"preset": preset}, today=t) == as_dates(current)
    assert previous_window({"preset": preset}, today=t) == as_dates(previous)


async def test_maximum_no_previous(db):
    frame = await _one(db, _q("spend", preset="maximum", compare="previous", by="client"))
    assert frame["previousAvailable"] is False
    assert "previous" not in frame["totals"]
    assert all("previous" not in r for r in frame["rows"])
    # all-time totals come from the cached `maximum` bucket (daily history is capped)
    assert frame["totals"]["values"] == {"spend": 8000.0}
    assert {r["key"]: r["values"]["spend"] for r in frame["rows"]} == {"g1": 5000, "g2": 1000, "g3": 2000}
    assert frame["window"] == {"label": "All time", "from": "2024-01-01", "to": "2026-10-20"}


async def test_previous_falls_back_to_the_previous_preset_bucket(db):
    # Delta's history starts today, so its daily rows cannot answer "yesterday";
    # the cached `yesterday` bucket covers exactly that day.
    await db["client_groups"].insert_one(_group("g6", "Delta", [{"date": _iso(TODAY), "spend": 50}], {
        "today": _insights(spend=50), "yesterday": _insights(spend=30),
    }))
    frame = await _one(db, _q("spend", preset="today", compare="previous"))
    assert frame["totals"]["values"] == {"spend": 20 + 1 + 100 + 50}
    assert frame["totals"]["previous"] == {"spend": 19 + 1 + 100 + 30}

    # this_month's previous (1-20 Sep) is not a cached bucket (last_month is all of September): unknown.
    month = await _one(db, _q("spend", preset="this_month", compare="previous", by="client"))
    assert month["totals"]["previous"] == {"spend": None}
    rows = {r["key"]: r for r in month["rows"]}
    assert rows["g6"]["previous"] == {"spend": None}
    assert rows["g1"]["previous"] == {"spend": _spend("g1", date(2026, 9, 1), date(2026, 9, 20))}


async def test_short_history_without_a_bucket_gives_null_previous(db):
    # Cobalt's rows start on 1 Oct, so the previous 7 days before 1-10 Oct are not on record.
    frame = await _one(db, _q("spend", window={"from": "2026-10-01", "to": "2026-10-10"}, compare="previous"))
    assert frame["totals"]["previous"] == {"spend": None}


# --- preset-only metrics, filters, spark -----------------------------------------------

async def test_preset_only_metric_reads_the_preset_bucket(db):
    frame = await _one(db, _q("reach", preset="this_month", compare="previous", by="client"))
    assert frame["totals"]["values"] == {"reach": 70.0}
    assert frame["totals"]["previous"] == {"reach": None}  # last_month is not this_month's previous
    today = await _one(db, _q("reach", preset="today", compare="previous", filters={"client": ["g1", "g2"]}))
    assert today["totals"] == {"values": {"reach": 6.0}, "previous": {"reach": 4.0}}
    custom = await _one(db, _q("reach", window={"from": "2026-10-01", "to": "2026-10-10"}))
    assert custom["totals"]["values"] == {"reach": None}


async def test_over_rejects_a_metric_without_daily_history(db):
    assert await _run(db, _q("reach", over="day")) == [None]


async def test_unknown_client_filter_matches_nothing(db):
    frame = await _one(db, _q("spend", "cpl", by="client", filters={"client": ["nope"]}))
    assert frame["rows"] == []
    assert frame["totals"]["values"] == {"spend": None, "cpl": None}
    one = await _one(db, _q("spend", filters={"client": ["g2", "nope"]}))
    assert one["totals"]["values"] == {"spend": 6}


async def test_day_filter_keeps_only_listed_days(db):
    frame = await _one(db, _q("spend", "reach", compare="previous",
                              filters={"day": ["2026-10-17", "2026-10-18", "2020-01-01"]}))
    assert frame["totals"]["values"] == {"spend": 17 + 18 + 1 + 200, "reach": None}
    assert frame["totals"]["previous"] == {"spend": None, "reach": None}


async def test_unknown_filter_dimension_fails_the_query(db):
    assert await _run(db, _q("spend", filters={"campaign": ["c1"]})) == [None]


async def test_by_day_rows_sort_and_limit(db):
    frame = await _one(db, _q("spend", by="day", sort={"metric": "spend", "dir": "desc"}, limit=2,
                              filters={"client": ["g1"]}))
    assert [(r["key"], r["values"]["spend"]) for r in frame["rows"]] == [("2026-10-20", 20), ("2026-10-19", 19)]


async def test_spark_daily(db):
    frame = await _one(db, _q("spend", "cpl", "reach", spark=True, filters={"client": ["g2"]}))
    days = [_iso(d) for d in _days(*LAST_7D)]
    assert list(frame["spark"]) == ["spend", "cpl"]  # reach has no daily history
    assert frame["spark"]["spend"] == [{"date": d, "value": 0.0 if d == _iso(GAP) else 1.0} for d in days]
    assert frame["spark"]["cpl"] == [{"date": d, "value": None} for d in days]

    everyone = await _one(db, _q("cpl", spark=True))
    gap = next(p for p in everyone["spark"]["cpl"] if p["date"] == _iso(GAP))
    assert gap["value"] == pytest.approx(117 / 3)

    assert "spark" not in await _one(db, _q("spend"))
    assert "spark" not in await _one(db, _q("spend", spark=False))


# --- failures and loading ---------------------------------------------------------

async def test_failed_query_is_none_others_ok(db, caplog):
    good = _q("spend")
    frames = await _run(db, good, _q("nope"), {"metrics": [], "window": {"preset": "last_7d"}},
                        {**good, "bogus": 1}, _q("spend", preset="last_99d"), _q("spend", "spend"),
                        _q("spend", sort={"metric": "leads", "dir": "asc"}, by="client"), good)
    assert frames[0] == frames[-1] and frames[0] is not None
    assert frames[1:-1] == [None] * 6
    assert len([r for r in caplog.records if "dashboard query" in r.getMessage()]) == 6


class _Recorder:
    """A db stand-in that records client_groups.find calls."""

    def __init__(self, groups):
        self.calls, self.groups = [], groups

    def __getitem__(self, name):
        return self

    def find(self, flt, projection):
        self.calls.append(projection)
        groups = self.groups

        class _Cursor:
            async def to_list(self, length=None):
                return groups

        return _Cursor()


async def test_loads_groups_once_and_reads_only_needed_daily_fields():
    db = _Recorder(_seed_groups()[:3])
    frames = await run_queries(db, USER, [_q("spend"), _q("cpl", over="day"), _q("reach"), _q("nope")], today=TODAY)
    assert frames[0] is not None and frames[-1] is None
    assert len(db.calls) == 1
    projection = db.calls[0]
    assert projection.get("meta_daily_spend") == 1
    assert not {"ghl_daily_leads", "ghl_daily_opps", "hp_daily_calls"} & set(projection)
    assert projection.get("facebook_cache") == 1


async def test_no_queries_reads_nothing():
    db = _Recorder([])
    assert await run_queries(db, USER, []) == []
    assert db.calls == []


# --- contract: frames match dashkit's strict FrameSchema (src/core/query.ts) -----------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Column(_Strict):
    key: str
    label: str
    format: Literal["integer", "currency", "percentage", "decimal", "compact"]
    polarity: Optional[Literal["higher_is_better", "lower_is_better"]] = None


class _Row(_Strict):
    key: str
    label: str
    values: Dict[str, Optional[float]]
    previous: Optional[Dict[str, Optional[float]]] = None


class _Totals(_Strict):
    values: Dict[str, Optional[float]]
    previous: Optional[Dict[str, Optional[float]]] = None


class _SparkPoint(_Strict):
    date: str
    value: Optional[float]


class _Window(_Strict):
    label: str
    to: str
    from_: str

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False,
                              alias_generator=lambda name: "from" if name == "from_" else name)


class _Frame(_Strict):
    columns: List[_Column]
    rows: List[_Row]
    totals: _Totals
    spark: Optional[Dict[str, List[_SparkPoint]]] = None
    previousAvailable: bool
    window: _Window


def _check_frame(frame: dict):
    """FrameSchema: strict keys; optional keys are absent rather than null."""
    _Frame.model_validate(frame)
    nullable_absent = [frame.get("spark", 0), frame["totals"].get("previous", 0)]
    nullable_absent += [r.get("previous", 0) for r in frame["rows"]]
    nullable_absent += [c.get("polarity", "") for c in frame["columns"]]
    assert None not in nullable_absent


def test_contract_model_rejects_extra_and_missing_keys():
    ok = {"columns": [], "rows": [], "totals": {"values": {}}, "previousAvailable": True,
          "window": {"label": "x", "from": "2026-01-01", "to": "2026-01-02"}}
    _check_frame(ok)
    with pytest.raises(ValidationError):
        _Frame.model_validate({**ok, "extra": 1})
    with pytest.raises(ValidationError):
        _Frame.model_validate({k: v for k, v in ok.items() if k != "window"})
    with pytest.raises(ValidationError):
        _Frame.model_validate({**ok, "totals": {"values": {"a": "1"}}})


async def test_contract_frames_match_dashkit_frame_schema(db):
    queries = [
        _q("spend", "cpl", "reach", compare="previous", spark=True),
        _q("spend", "ctr", by="client", compare="previous", sort={"metric": "ctr", "dir": "desc"}, limit=2),
        _q("cpl", over="week", preset="last_30d", compare="previous"),
        _q("spend", over="day", by="client", compare="previous", limit=2),
        _q("spend", preset="maximum", over="month", compare="previous", spark=True),
        _q("spend", by="day", window={"from": "2026-10-01", "to": "2026-10-05"}, compare="previous"),
        _q("spend", filters={"client": ["nope"]}, compare="previous", spark=True),
    ]
    frames = await _run(db, *queries)
    assert None not in frames
    for frame in frames:
        _check_frame(frame)


# --- route ------------------------------------------------------------------------

@pytest.fixture
def client(mock_db):
    asyncio.run(mock_db["client_groups"].insert_many(_seed_groups()))
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def test_route_returns_one_frame_per_query(client):
    r = client.post("/api/dashboards/query", json={"queries": [_q("spend"), _q("nope")]})
    assert r.status_code == 200, r.text
    frames = r.json()["frames"]
    assert len(frames) == 2 and frames[1] is None
    _check_frame(frames[0])
    assert client.post("/api/dashboards/query", json={"queries": []}).json() == {"frames": []}


@pytest.mark.parametrize("body", [
    {},
    {"queries": "spend"},
    {"queries": [1]},
    {"queries": [_q("spend")] * 51},
    [_q("spend")],
], ids=["missing", "not-a-list", "not-an-object", "too-many", "bare-list"])
def test_route_422_on_bad_body(client, body):
    assert client.post("/api/dashboards/query", json=body).status_code == 422


def test_route_accepts_fifty_queries(client):
    r = client.post("/api/dashboards/query", json={"queries": [_q("spend")] * 50})
    assert r.status_code == 200
    assert len(r.json()["frames"]) == 50


def test_route_requires_auth(mock_db):
    app.dependency_overrides.pop(get_current_user, None)
    r = TestClient(app).post("/api/dashboards/query", json={"queries": [_q("spend")]})
    assert r.status_code in (401, 403)
