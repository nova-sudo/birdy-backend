"""
tests/test_dashboards_router.py
-------------------------------
CRUD for dashkit dashboard documents: round-trip, listing, schema validation,
per-user isolation, delete, and path/body id agreement.
"""

import copy

import pytest
from fastapi.testclient import TestClient

from dependencies import get_current_user
from main import app


def _doc(dash_id="d1", title="My dashboard"):
    return {
        "id": dash_id,
        "version": 1,
        "title": title,
        "sections": [{"id": "s1", "title": "Overview", "widgets": []}],
        "meta": {
            "createdAt": "2026-10-01T00:00:00Z",
            "updatedAt": "2026-10-01T00:00:00Z",
            "lastAuthor": "user",
        },
    }


@pytest.fixture
def client(mock_db):
    app.dependency_overrides[get_current_user] = lambda: "alice@example.com"
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user


def test_put_then_get_round_trips(client):
    r = client.put("/api/dashboards/d1", json=_doc())
    assert r.status_code == 200
    assert r.json()["id"] == "d1"

    got = client.get("/api/dashboards/d1")
    assert got.status_code == 200
    body = got.json()
    assert body["title"] == "My dashboard"
    assert body["sections"] == _doc()["sections"]
    assert "_id" not in body


def test_put_refreshes_meta_updated_at(client):
    r = client.put("/api/dashboards/d1", json=_doc())
    assert r.json()["meta"]["updatedAt"] != "2026-10-01T00:00:00Z"
    assert r.json()["meta"]["createdAt"] == "2026-10-01T00:00:00Z"


def test_put_is_an_upsert(client):
    client.put("/api/dashboards/d1", json=_doc())
    client.put("/api/dashboards/d1", json=_doc(title="Renamed"))
    assert client.get("/api/dashboards/d1").json()["title"] == "Renamed"
    assert len(client.get("/api/dashboards").json()["items"]) == 1


def test_list_returns_title_and_updated_at_newest_first(client):
    client.put("/api/dashboards/d1", json=_doc("d1", "First"))
    client.put("/api/dashboards/d2", json=_doc("d2", "Second"))

    items = client.get("/api/dashboards").json()["items"]
    assert [i["id"] for i in items] == ["d2", "d1"]
    assert items[0]["title"] == "Second"
    assert set(items[0]) == {"id", "title", "updatedAt"}
    assert isinstance(items[0]["updatedAt"], str)


def test_invalid_doc_is_422_with_errors(client):
    bad = _doc()
    del bad["title"]
    bad["sections"][0]["widgets"] = "nope"

    r = client.put("/api/dashboards/d1", json=bad)
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert isinstance(errors, list) and errors
    assert any(e.startswith("/sections/0/widgets: ") for e in errors)
    assert any(e.startswith("/: ") and "title" in e for e in errors)
    assert client.get("/api/dashboards/d1").status_code == 404


def test_another_users_dashboard_is_404(client):
    client.put("/api/dashboards/d1", json=_doc())
    _as("bob@example.com")
    assert client.get("/api/dashboards/d1").status_code == 404
    assert client.delete("/api/dashboards/d1").status_code == 404
    assert client.get("/api/dashboards").json()["items"] == []

    # Bob saving the same id does not overwrite Alice's copy.
    client.put("/api/dashboards/d1", json=_doc(title="Bob's"))
    _as("alice@example.com")
    assert client.get("/api/dashboards/d1").json()["title"] == "My dashboard"


def test_delete_then_get_is_404(client):
    client.put("/api/dashboards/d1", json=_doc())
    assert client.delete("/api/dashboards/d1").status_code == 204
    assert client.get("/api/dashboards/d1").status_code == 404


def test_id_mismatch_is_400(client):
    r = client.put("/api/dashboards/other", json=_doc("d1"))
    assert r.status_code == 400


def test_put_does_not_mutate_unrelated_fields(client):
    doc = _doc()
    doc["description"] = "hello"
    sent = copy.deepcopy(doc)
    client.put("/api/dashboards/d1", json=doc)
    got = client.get("/api/dashboards/d1").json()
    assert got["description"] == sent["description"]


@pytest.mark.asyncio
async def test_unique_index_on_user_and_id(mock_mongo_client, mock_db):
    from pymongo.errors import DuplicateKeyError
    from routers.dashboards import create_dashboards_indexes

    await create_dashboards_indexes(mock_mongo_client)
    await mock_db["dashboards"].insert_one({"user_id": "u", "id": "d1"})
    await mock_db["dashboards"].insert_one({"user_id": "v", "id": "d1"})
    with pytest.raises(DuplicateKeyError):
        await mock_db["dashboards"].insert_one({"user_id": "u", "id": "d1"})


# --- catalog ---------------------------------------------------------------

_FORMATS = {"integer", "currency", "percentage", "decimal", "compact"}
_AGGREGATIONS = {"sum", "avg", "last"}
_POLARITIES = {"higher_is_better", "lower_is_better"}


def _metric(catalog, key):
    return next(m for m in catalog["metrics"] if m["key"] == key)


def test_catalog_lists_spend_with_dimensions_client_and_day(client):
    r = client.get("/api/dashboards/catalog")
    assert r.status_code == 200
    spend = _metric(r.json(), "spend")
    assert spend["dimensions"] == ["client", "day"]
    assert spend["format"] == "currency"
    assert spend["aggregation"] == "sum"
    assert spend["label"] == "Spend"
    # leads / meta_leads read Meta results, which have no daily series.
    assert _metric(r.json(), "meta_leads")["dimensions"] == ["client"]
    assert _metric(r.json(), "leads")["dimensions"] == ["client"]


def test_catalog_day_dimension_for_daily_backed_metrics(client):
    cat = client.get("/api/dashboards/catalog").json()
    for key in ("ghl_leads", "ghl_contacts", "impressions", "clicks", "meta_spend", "hp_total_calls"):
        assert _metric(cat, key)["dimensions"] == ["client", "day"], key


def test_catalog_lists_ctr_with_dimensions_client_only(client):
    ctr = _metric(client.get("/api/dashboards/catalog").json(), "ctr")
    assert ctr["dimensions"] == ["client"]
    assert ctr["aggregation"] == "avg"
    assert ctr["format"] == "percentage"


def test_catalog_polarity_marks_cost_metrics_lower_is_better(client):
    cat = client.get("/api/dashboards/catalog").json()
    for key in ("cpc", "cpm", "cpl", "cost_per_result", "cost_per_lead"):
        assert _metric(cat, key)["polarity"] == "lower_is_better", key
    for key in ("ghl_lost_opps", "ghl_abandoned_opps"):
        assert _metric(cat, key)["polarity"] == "lower_is_better", key
    for key in ("spend", "ctr", "leads", "ghl_won_opps", "ghl_open_opps"):
        assert _metric(cat, key)["polarity"] == "higher_is_better", key


def test_windows_exclude_data_maximum(client):
    windows = client.get("/api/dashboards/catalog").json()["windows"]
    presets = [w["preset"] for w in windows]
    assert "data_maximum" not in presets
    assert "last_7d" in presets and "maximum" in presets
    assert next(w for w in windows if w["preset"] == "last_7d")["label"] == "Last 7 days"


def test_window_labels_read_as_sentences(client):
    labels = {w["preset"]: w["label"] for w in client.get("/api/dashboards/catalog").json()["windows"]}
    assert labels["last_14d"] == "Last 14 days"
    assert labels["last_30d"] == "Last 30 days"
    assert labels["maximum"] == "All time"
    assert labels["this_week"] == "This week"
    assert labels["this_month"] == "This month"
    assert labels["last_quarter"] == "Last quarter"
    assert labels["this_year"] == "This year"
    assert labels["today"] == "Today"
    assert labels["yesterday"] == "Yesterday"


def test_metric_labels_keep_acronyms_in_capitals(client):
    cat = client.get("/api/dashboards/catalog").json()
    assert _metric(cat, "ctr")["label"] == "CTR"
    assert _metric(cat, "cpl")["label"] == "CPL"
    assert _metric(cat, "meta_cpc")["label"] == "Meta CPC"
    assert _metric(cat, "ghl_won_opps")["label"] == "GHL Won Opps"
    assert _metric(cat, "hp_total_calls")["label"] == "HP Total Calls"
    assert _metric(cat, "cost_per_lead")["label"] == "Cost Per Lead"


def test_windows_dedupe_by_alias_target_keeping_first_key(client):
    presets = [w["preset"] for w in client.get("/api/dashboards/catalog").json()["windows"]]
    assert "this_week" in presets
    assert "this_week_mon_today" not in presets
    assert len(presets) == len(set(presets))


def test_catalog_validates_against_dashkit_catalog_schema_shape(client):
    cat = client.get("/api/dashboards/catalog").json()
    assert set(cat) == {"metrics", "dimensions", "windows"}
    assert cat["dimensions"] == [
        {"key": "client", "label": "Client"},
        {"key": "day", "label": "Day"},
    ]
    dim_keys = {d["key"] for d in cat["dimensions"]}
    assert cat["metrics"]
    for m in cat["metrics"]:
        assert {"key", "label", "format", "dimensions"} <= set(m)
        assert set(m) <= {"key", "label", "unit", "format", "dimensions",
                          "description", "aggregation", "polarity"}
        assert m["format"] in _FORMATS
        assert m["aggregation"] in _AGGREGATIONS
        assert m["polarity"] in _POLARITIES
        assert "client" in m["dimensions"] and set(m["dimensions"]) <= dim_keys
    for w in cat["windows"]:
        assert set(w) == {"preset", "label"}


def test_catalog_requires_auth(mock_db):
    app.dependency_overrides.pop(get_current_user, None)
    assert TestClient(app).get("/api/dashboards/catalog").status_code in (401, 403)
