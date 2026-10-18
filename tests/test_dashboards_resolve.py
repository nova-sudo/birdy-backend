"""
tests/test_dashboards_resolve.py
--------------------------------
POST /api/dashboards/resolve: dashkit bindings -> ResolvedSlot dicts, read
from seeded client_groups through metric_orchestrator. Per-binding failures
come back as a null slot plus an `errors` entry; only an oversized or
malformed request is a 422.
"""

import asyncio
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from dependencies import get_current_user
from main import app

USER = "alice@example.com"


def _insights(spend, impressions, clicks, results):
    return {"metrics": {"insights": {
        "spend": spend, "impressions": impressions, "clicks": clicks, "results": results,
    }}}


def _group(gid, name, *, maximum, this_month=None, last_month=None, daily=None,
           user=USER, status="Active", **extra):
    presets = {"maximum": maximum}
    if this_month is not None:
        presets["this_month"] = this_month
    if last_month is not None:
        presets["last_month"] = last_month
    return {
        "id": gid,
        "name": name,
        "user_id": user,
        "client_status": status,
        "facebook_cache": {"presets": presets},
        "meta_daily_spend": daily or [],
        "ghl_daily_leads": [],
        "hp_daily_calls": [],
        **extra,
    }


@pytest.fixture
def seeded(mock_db):
    today = date.today()
    d = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
    groups = [
        _group(
            "g1", "Acme",
            maximum=_insights(100, 1000, 50, 5),
            this_month=_insights(40, 400, 20, 2),
            last_month=_insights(30, 300, 10, 1),
            daily=[
                {"date": "2020-01-02", "spend": 10, "impressions": 100, "clicks": 5},
                {"date": "2020-01-01", "spend": 5},
                {"date": d(2), "spend": 7},
                {"date": d(40), "spend": 1},
            ],
            ghl_daily_leads=[
                {"date": "2020-01-01", "leads": 2},
                {"date": "2020-01-05", "leads": "abc"},
            ],
            gohighlevel_cache={"metrics": {"total_contacts": 12}},
            ghl_opp_cache={"maximum": {"won": 50}, "this_month": {"won": 4}, "last_month": {"won": 3}},
            hotprospector_call_cache={
                "maximum": {"total_calls": 500},
                "this_month": {"total_calls": 40},
                "last_month": {"total_calls": 30},
            },
        ),
        _group(
            "g2", "Beta",
            maximum=_insights(250, 2000, 100, 10),
            this_month=_insights(60, 600, 30, 3),
            last_month=_insights(20, 200, 10, 2),
            daily=[
                {"date": "2020-01-01", "spend": 3},
                {"date": "2020-01-03", "spend": 4},
                {"date": d(2), "spend": 2},
            ],
            gohighlevel_cache={"metrics": {"total_contacts": 8}},
            ghl_opp_cache={"maximum": {"won": 70}, "this_month": {"won": 6}},
            hotprospector_call_cache={"maximum": {"total_calls": 900}, "this_month": {"total_calls": 60}},
        ),
        _group("g3", "Dormant", maximum=_insights(999, 1, 1, 1), status="Inactive"),
        _group("g4", "Someone else", maximum=_insights(777, 1, 1, 1), user="bob@example.com"),
    ]
    del groups[1]["ghl_daily_leads"]  # a group that has never had the daily field written
    asyncio.run(mock_db["client_groups"].insert_many(groups))
    return {"today": today, "d": d}


@pytest.fixture
def client(seeded):
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _resolve(client, bindings, shapes=None):
    body = {"bindings": bindings}
    if shapes is not None:
        body["shapes"] = shapes
    r = client.post("/api/dashboards/resolve", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _b(metric="spend", preset="maximum", **kw):
    return {"metric": metric, "window": {"preset": preset}, **kw}


# --- value slots -------------------------------------------------------------

def test_value_slot_sums_across_active_groups(client):
    out = _resolve(client, [_b("spend")])
    assert out["errors"] == []
    assert out["slots"] == [{
        "shape": "value", "value": 350.0, "format": "currency", "polarity": "higher_is_better",
    }]


def test_value_slot_recomputes_ratio_metrics(client):
    slot = _resolve(client, [_b("cpc")])["slots"][0]
    assert slot["value"] == pytest.approx(350 / 150)
    assert slot["polarity"] == "lower_is_better"


def test_previous_present_where_the_previous_preset_is_cached(client):
    slot = _resolve(client, [_b("spend", "this_month", compare="previous")])["slots"][0]
    assert slot["value"] == 100.0
    assert slot["previous"] == 50.0


def test_previous_absent_for_last_7d_and_maximum(client):
    slots = _resolve(client, [
        _b("spend", "last_7d", compare="previous"),
        _b("spend", "maximum", compare="previous"),
    ])["slots"]
    assert all("previous" not in s for s in slots)


def test_previous_absent_without_compare(client):
    slot = _resolve(client, [_b("spend", "this_month")])["slots"][0]
    assert "previous" not in slot


# --- by client ---------------------------------------------------------------

def test_by_client_one_point_per_group_sorted_desc(client):
    slot = _resolve(client, [_b("spend", by="client")])["slots"][0]
    assert slot["shape"] == "series"
    assert slot["points"] == [{"label": "Beta", "value": 250.0}, {"label": "Acme", "value": 100.0}]
    assert slot["format"] == "currency"


def test_by_client_limit_1(client):
    slot = _resolve(client, [_b("spend", by="client", limit=1)])["slots"][0]
    assert slot["points"] == [{"label": "Beta", "value": 250.0}]


def test_by_client_sort_asc(client):
    slot = _resolve(client, [_b("spend", by="client", sort="asc")])["slots"][0]
    assert [p["label"] for p in slot["points"]] == ["Acme", "Beta"]


def test_by_client_compare_previous_per_point(client):
    slot = _resolve(client, [_b("spend", "this_month", by="client", compare="previous")])["slots"][0]
    assert slot["points"] == [
        {"label": "Beta", "value": 60.0, "previous": 20.0},
        {"label": "Acme", "value": 40.0, "previous": 30.0},
    ]


# --- by day ------------------------------------------------------------------

def test_by_day_on_spend_returns_ascending_iso_dates_summed_across_groups(client, seeded):
    slot = _resolve(client, [_b("spend", by="day")])["slots"][0]
    d = seeded["d"]
    assert slot["shape"] == "series"
    assert slot["points"] == sorted(
        [
            {"label": "2020-01-01", "value": 8.0},
            {"label": "2020-01-02", "value": 10.0},
            {"label": "2020-01-03", "value": 4.0},
            {"label": d(40), "value": 1.0},
            {"label": d(2), "value": 9.0},
        ],
        key=lambda p: p["label"],
    )
    assert slot["format"] == "currency"


def test_by_day_respects_preset_bounds(client, seeded):
    slot = _resolve(client, [_b("spend", "last_7d", by="day")])["slots"][0]
    assert slot["points"] == [{"label": seeded["d"](2), "value": 9.0}]


def test_by_day_custom_window(client):
    binding = {"metric": "spend", "by": "day", "window": {"from": "2020-01-02", "to": "2020-01-03"}}
    slot = _resolve(client, [binding])["slots"][0]
    assert slot["points"] == [{"label": "2020-01-02", "value": 10.0}, {"label": "2020-01-03", "value": 4.0}]


def test_by_day_ignores_compare(client):
    slot = _resolve(client, [_b("spend", by="day", compare="previous")])["slots"][0]
    assert all("previous" not in p for p in slot["points"])


def test_by_day_on_a_metric_without_daily_source_is_null_with_error(client):
    out = _resolve(client, [_b("ctr", by="day"), _b("spend")])
    assert out["slots"][0] is None
    assert out["slots"][1]["value"] == 350.0
    assert out["errors"] == [{"index": 0, "code": "DAY_SERIES_UNAVAILABLE", "message": out["errors"][0]["message"]}]
    assert out["errors"][0]["message"]


# --- filters -----------------------------------------------------------------

def test_filters_client_restricts(client):
    out = _resolve(client, [
        _b("spend", filters={"client": ["g1"]}),
        _b("spend", by="client", filters={"client": ["g2"], "ignored": ["x"]}),
        _b("spend", by="day", filters={"client": ["g2"]}),
    ])
    value, by_client, by_day = out["slots"]
    assert value["value"] == 100.0
    assert by_client["points"] == [{"label": "Beta", "value": 250.0}]
    assert by_day["points"][0] == {"label": "2020-01-01", "value": 3.0}


def test_filters_client_never_reaches_other_users_groups(client):
    slot = _resolve(client, [_b("spend", filters={"client": ["g4", "g3"]})])["slots"][0]
    assert slot["value"] == 0.0


# --- errors ------------------------------------------------------------------

def test_unknown_metric_is_null_with_error(client):
    out = _resolve(client, [_b("nope")])
    assert out["slots"] == [None]
    assert out["errors"][0]["code"] == "METRIC_UNKNOWN"
    assert out["errors"][0]["index"] == 0


def test_custom_window_on_value_is_window_unsupported(client):
    out = _resolve(client, [{"metric": "spend", "window": {"from": "2020-01-01", "to": "2020-01-31"}}])
    assert out["slots"] == [None]
    assert out["errors"][0]["code"] == "WINDOW_UNSUPPORTED"


def test_unknown_dimension_is_null_with_error(client):
    out = _resolve(client, [_b("spend", by="campaign")])
    assert out["slots"] == [None]
    assert out["errors"][0]["code"] == "DIMENSION_UNKNOWN"


def test_unexpected_exception_is_resolve_failed(client, monkeypatch):
    import services.dashboards_resolve as mod

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mod, "aggregate_metric", boom)
    out = _resolve(client, [_b("spend"), _b("spend", by="client")])
    assert out["slots"][0] is None
    assert out["slots"][1]["shape"] == "series"
    assert out["errors"][0]["code"] == "RESOLVE_FAILED"
    assert "kaboom" not in out["errors"][0]["message"]


def test_51_bindings_is_422(client):
    r = client.post("/api/dashboards/resolve", json={"bindings": [_b()] * 51})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "TOO_MANY_BINDINGS"


def test_50_bindings_is_fine(client):
    assert len(_resolve(client, [_b()] * 50)["slots"]) == 50


def test_malformed_body_is_422(client):
    assert client.post("/api/dashboards/resolve", json={"bindings": "nope"}).status_code == 422


def test_empty_bindings(client):
    assert _resolve(client, []) == {"slots": [], "errors": []}


# --- table shape -------------------------------------------------------------

def test_table_shape_returns_columns(client):
    slot = _resolve(client, [_b("spend", by="client")], shapes=["table"])["slots"][0]
    assert slot == {
        "shape": "table",
        "columns": [
            {"key": "label", "label": "Client"},
            {"key": "value", "label": "Spend", "format": "currency"},
        ],
        "rows": [{"label": "Beta", "value": 250.0}, {"label": "Acme", "value": 100.0}],
    }


def test_table_shape_by_day_and_value(client, seeded):
    day, value = _resolve(
        client, [_b("spend", by="day", limit=1), _b("spend")], shapes=["table", "table"]
    )["slots"]
    assert day["columns"][0] == {"key": "label", "label": "Day"}
    assert day["rows"] == [{"label": seeded["d"](2), "value": 9.0}]
    assert value["rows"] == [{"label": "Total", "value": 350.0}]


def test_shapes_shorter_than_bindings_default_to_natural_shape(client):
    slots = _resolve(client, [_b("spend", by="client"), _b("spend")], shapes=["table"])["slots"]
    assert [s["shape"] for s in slots] == ["table", "value"]


def test_resolve_requires_auth(mock_db):
    app.dependency_overrides.pop(get_current_user, None)
    r = TestClient(app).post("/api/dashboards/resolve", json={"bindings": []})
    assert r.status_code in (401, 403)


# --- fix round 1 -------------------------------------------------------------

def test_compare_on_ghl_general_metric_has_no_previous(client):
    value, by_client = _resolve(client, [
        _b("ghl_leads", "this_month", compare="previous"),
        _b("ghl_leads", "this_month", by="client", compare="previous"),
    ])["slots"]
    assert value["value"] == 20.0
    assert "previous" not in value
    assert all("previous" not in p for p in by_client["points"])


def test_compare_on_recompute_metric_with_a_ghl_component_has_no_previous(client):
    slot = _resolve(client, [_b("cost_per_lead", "this_month", compare="previous")])["slots"][0]
    assert "previous" not in slot


def test_compare_on_hp_metric_uses_only_groups_with_the_previous_bucket(client):
    slot = _resolve(client, [_b("hp_total_calls", "this_month", compare="previous")])["slots"][0]
    assert slot["value"] == 100.0
    # g2 has no last_month bucket; its reader would fall back to all-time (900).
    assert slot["previous"] == 30.0


def test_compare_on_hp_metric_without_any_previous_bucket_has_no_previous(client):
    slot = _resolve(client, [
        _b("hp_total_calls", "this_month", compare="previous", filters={"client": ["g2"]}),
    ])["slots"][0]
    assert slot["value"] == 60.0
    assert "previous" not in slot


def test_compare_on_ghl_opp_metric_by_client_per_point(client):
    slot = _resolve(client, [_b("ghl_won_opps", "this_month", by="client", compare="previous")])["slots"][0]
    assert slot["points"] == [
        {"label": "Beta", "value": 6.0},
        {"label": "Acme", "value": 4.0, "previous": 3.0},
    ]


def test_compare_on_ghl_opp_metric_without_previous_bucket(client):
    slot = _resolve(client, [
        _b("ghl_won_opps", "this_month", compare="previous", filters={"client": ["g2"]}),
    ])["slots"][0]
    assert "previous" not in slot


def test_compare_on_meta_metric_ignores_the_legacy_flat_fallback(client, mock_db):
    legacy = _group("g6", "Legacy", maximum=_insights(1, 1, 1, 1), this_month=_insights(5, 1, 1, 1))
    legacy["facebook_cache"]["metrics"] = {"insights": {"spend": 999}}
    asyncio.run(mock_db["client_groups"].insert_one(legacy))
    slot = _resolve(client, [_b("spend", "this_month", compare="previous", filters={"client": ["g6"]})])["slots"][0]
    assert slot["value"] == 5.0
    assert "previous" not in slot


def test_meta_leads_by_day_is_unavailable(client):
    out = _resolve(client, [_b("leads", by="day"), _b("meta_leads", by="day")])
    assert out["slots"] == [None, None]
    assert [e["code"] for e in out["errors"]] == ["DAY_SERIES_UNAVAILABLE"] * 2


def test_ghl_leads_by_day_reads_ghl_daily_leads(client):
    slot = _resolve(client, [_b("ghl_leads", by="day")])["slots"][0]
    # Beta has no ghl_daily_leads field at all; "abc" counts as 0.
    assert slot["points"] == [{"label": "2020-01-01", "value": 2.0}, {"label": "2020-01-05", "value": 0.0}]
    assert slot["format"] == "integer"


def test_impressions_by_day_reads_the_impressions_key(client):
    slot = _resolve(client, [_b("impressions", by="day", filters={"client": ["g1"]})])["slots"][0]
    assert {"label": "2020-01-02", "value": 100.0} in slot["points"]
    assert {"label": "2020-01-01", "value": 0.0} in slot["points"]


def test_non_finite_numbers_become_zero(client, mock_db):
    asyncio.run(mock_db["client_groups"].insert_one(_group(
        "g5", "Broken",
        maximum=_insights(float("nan"), 0, 0, 0),
        daily=[{"date": "2020-01-01", "spend": float("inf")}],
    )))
    value, by_client, by_day = _resolve(client, [
        _b("spend", filters={"client": ["g5"]}),
        _b("spend", by="client", filters={"client": ["g5"]}),
        _b("spend", by="day", filters={"client": ["g5"]}),
    ])["slots"]
    assert value["value"] == 0.0
    assert by_client["points"] == [{"label": "Broken", "value": 0.0}]
    assert by_day["points"] == [{"label": "2020-01-01", "value": 0.0}]


def test_by_day_limit_keeps_most_recent_days_in_requested_order(client, seeded):
    d = seeded["d"]
    asc, desc = _resolve(client, [
        _b("spend", by="day", limit=2),
        _b("spend", by="day", limit=2, sort="desc"),
    ])["slots"]
    assert [p["label"] for p in asc["points"]] == [d(40), d(2)]
    assert [p["label"] for p in desc["points"]] == [d(2), d(40)]


def test_limit_zero_returns_no_points(client):
    by_client, by_day = _resolve(client, [
        _b("spend", by="client", limit=0),
        _b("spend", by="day", limit=0),
    ])["slots"]
    assert by_client["points"] == []
    assert by_day["points"] == []


def test_empty_client_filter_means_no_filter(client):
    slot = _resolve(client, [_b("spend", filters={"client": []})])["slots"][0]
    assert slot["value"] == 350.0


def test_shapes_longer_than_bindings_are_ignored(client):
    out = _resolve(client, [_b("spend")], shapes=["table", "table", "series"])
    assert len(out["slots"]) == 1
    assert out["slots"][0]["shape"] == "table"
