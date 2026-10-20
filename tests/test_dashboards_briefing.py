"""
tests/test_dashboards_briefing.py
---------------------------------
POST /api/dashboards/{id}/briefing: the live briefing for dashkit's `briefing`
widget. The LLM is a fake provider behind ai.provider_factory (no network) and
the frames come from a stubbed run_queries, so these tests pin the endpoint's
own rules: grounding, caching, the refresh rate limit and the error codes.
"""

import json

import pytest
from fastapi.testclient import TestClient

from ai.providers.base import ProviderResponse, Usage
from dependencies import get_current_user
from main import app
from services import dashboards_briefing as briefing
from services.dashboards_briefing import Grounding, extract_numbers, split_bold

USER = "alice@example.com"


# --- fixtures ---------------------------------------------------------------------

def _frame():
    """Spend $1,234.50 (was $1,100), 50 leads (was 40); two clients."""
    return {
        "columns": [
            {"key": "spend", "label": "Spend", "format": "currency"},
            {"key": "leads", "label": "Leads", "format": "integer"},
        ],
        "rows": [
            {"key": "g1", "label": "Acme 2", "values": {"spend": 800.0, "leads": 30.0},
             "previous": {"spend": 700.0, "leads": 25.0}},
            {"key": "g2", "label": "Beta", "values": {"spend": 434.5, "leads": 20.0},
             "previous": {"spend": 400.0, "leads": 15.0}},
        ],
        "totals": {"values": {"spend": 1234.5, "leads": 50.0}, "previous": {"spend": 1100.0, "leads": 40.0}},
        "previousAvailable": True,
        "window": {"preset": "last_7d", "from": "2026-10-14", "to": "2026-10-20", "label": "Last 7 days"},
    }


def _query(metric="spend"):
    return {"metrics": [{"metric": metric}], "by": "client", "window": {"preset": "last_30d"}, "compare": "previous"}


def _doc(updated="2026-10-01T00:00:00.000Z"):
    return {
        "id": "d1", "version": 3, "title": "Board",
        "widgets": [
            {"id": "w1", "type": "kpi", "title": "Spend", "bindings": {"query": _query("spend")},
             "layout": {"x": 0, "y": 0, "w": 3, "h": 3}, "data": {}},
            {"id": "w2", "type": "text", "title": "Notes", "layout": {"x": 3, "y": 0, "w": 3, "h": 3},
             "data": {"markdown": "Revenue was 9,999"}},
            {"id": "w3", "type": "briefing", "title": "Briefing", "layout": {"x": 6, "y": 0, "w": 6, "h": 3},
             "data": {}},
        ],
        "meta": {"createdAt": "2026-10-01T00:00:00.000Z", "updatedAt": updated},
    }


ANSWER = {
    "lead": "Spend reached **$1.2k** across 2 clients, up 12% on the last period.",
    "findings": [
        {"text": "Leads grew 25% to 50.", "tone": "positive"},
        {"text": "Acme 2 drove $800 of the spend.", "tone": "neutral"},
    ],
}


class FakeProvider:
    model = "fake-model"

    def __init__(self, reply=None, error=None):
        self.reply = ANSWER if reply is None else reply
        self.error = error
        self.calls = []

    async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
        self.calls.append(messages)
        if self.error:
            raise self.error
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return ProviderResponse(content=content, usage=Usage(input_tokens=100, output_tokens=50))


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()

    async def get_provider(user_id, db):
        return fake

    monkeypatch.setattr("ai.provider_factory.get_provider_for_user", get_provider)

    async def no_billing(*a, **k):
        return 0.0

    monkeypatch.setattr("credits.record_usage", no_billing)
    return fake


@pytest.fixture
def queried(monkeypatch):
    """Stub run_queries with one frame per query; records what it was asked."""
    seen = []

    async def fake_run(db, user_id, queries, today=None):
        seen.append(queries)
        return [_frame() for _ in queries]

    monkeypatch.setattr(briefing, "run_queries", fake_run)
    return seen


@pytest.fixture
def client(mock_db, provider, queried):
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


async def _seed(mock_db, doc=None, user=USER, dash_id="d1"):
    await mock_db["dashboards"].insert_one({"id": dash_id, "user_id": user, "doc": doc or _doc()})


@pytest.fixture
async def board(mock_db):
    await _seed(mock_db)


def _post(client, body=None, dash_id="d1"):
    return client.post(f"/api/dashboards/{dash_id}/briefing", json=body or {})


# --- grounding ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_briefing_grounded_numbers_kept(client, board):
    r = _post(client)
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is False
    assert body["parts"] == [
        {"text": "Spend reached "}, {"text": "$1.2k", "bold": True},
        {"text": " across 2 clients, up 12% on the last period."},
    ]
    assert body["findings"] == ANSWER["findings"]
    assert body["generated_at"].endswith("Z")


@pytest.mark.asyncio
async def test_ungrounded_finding_dropped(client, board, provider):
    provider.reply = {**ANSWER, "findings": [
        {"text": "Leads hit 5,000 this week.", "tone": "positive"},
        {"text": "Leads grew 25% to 50.", "tone": "positive"},
    ]}
    findings = _post(client).json()["findings"]
    assert [f["text"] for f in findings] == ["Leads grew 25% to 50."]


@pytest.mark.asyncio
async def test_ungrounded_lead_replaced(client, board, provider):
    provider.reply = {**ANSWER, "lead": "Spend hit **$9.9k** this week."}
    body = _post(client).json()
    assert body["parts"] == [{"text": "Here's how this board looks for Last 7 days."}]
    assert len(body["findings"]) == 2  # the grounded findings still stand


@pytest.mark.asyncio
async def test_unknown_tone_becomes_neutral(client, board, provider):
    provider.reply = {"lead": "All steady.", "findings": [{"text": "Leads grew 25%.", "tone": "warning"}]}
    assert _post(client).json()["findings"] == [{"text": "Leads grew 25%.", "tone": "neutral"}]


@pytest.mark.asyncio
async def test_years_and_dates_are_not_grounded_as_numbers(client, board, provider):
    provider.reply = {"lead": "Since Oct 14, 2026 spend is **$1.2k**.", "findings": [
        {"text": "Between 2026-10-14 and 10/20/2026 leads grew 25% in the last 30 days.", "tone": "positive"},
        {"text": "On October 20th spend was steady in 2026.", "tone": "neutral"},
    ]}
    body = _post(client).json()
    assert body["parts"][0]["text"].startswith("Since Oct 14, 2026 spend is ")
    assert len(body["findings"]) == 2


def test_extract_numbers_skips_years_dates_and_durations():
    assert extract_numbers("In 2026, on Oct 20 and 20 Oct, 2026-10-20, 10/20, over the last 30 days, in a 7-day span.") == []
    figures = extract_numbers("Spend $1.2k, 12.5%, 1,234 leads, 2.5m reach")
    assert [(v, pct) for v, _, pct in figures] == [(1200, False), (12.5, True), (1234, False), (2500000, False)]
    # a 4-digit count that is not a year-like 19xx/20xx number is still a figure
    assert [v for v, _, _ in extract_numbers("5,000 clicks and 3200 views")] == [5000.0, 3200.0]


def test_names_with_digits_are_not_figures():
    assert extract_numbers("Acme 2 led the board", names={"Acme 2"}) == []
    assert [v for v, _, _ in extract_numbers("Acme 2 led by 15%", names={"Acme 2"})] == [15.0]


def test_grounding_tolerance_and_percentage_changes():
    g = Grounding([_frame()])
    holds = lambda text: all(g.holds(*n) for n in extract_numbers(text))
    assert holds("$1.2k")             # 1234.5 rounded to the shown precision
    assert holds("1,234")             # within 1%
    assert holds("12%")               # spend change: 1234.5 vs 1100 = +12.2%
    assert holds("25%")               # leads change, 40 -> 50
    assert holds("3%") is False
    assert holds("$2k") is False
    assert holds("$1,500") is False


def test_split_bold_runs():
    assert split_bold("a **b** c") == [{"text": "a "}, {"text": "b", "bold": True}, {"text": " c"}]
    assert split_bold("**all**") == [{"text": "all", "bold": True}]
    assert split_bold("stray ** marker") == [{"text": "stray  marker"}]


# --- what the model is given --------------------------------------------------------

@pytest.mark.asyncio
async def test_only_widgets_with_a_query_are_asked_about(client, board, queried, provider):
    _post(client)
    assert len(queried) == 1 and len(queried[0]) == 1  # the text and briefing widgets carry no query
    facts = provider.calls[0][1]["content"]
    assert "Spend → Spend: 1234.50 (previous: 1100)" in facts
    assert "Spend → Acme 2 / Leads: 30 (previous: 25)" in facts
    assert "9,999" not in facts


@pytest.mark.asyncio
async def test_body_window_and_filters_override_the_queries(client, board, queried):
    window = {"from": "2026-09-01", "to": "2026-09-30"}
    _post(client, {"window": window, "filters": {"client": ["g1"]}, "focus": "cost"})
    sent = queried[0][0]
    assert sent["window"] == window and sent["filters"] == {"client": ["g1"]}
    assert sent["metrics"] == _query()["metrics"]


@pytest.mark.asyncio
async def test_focus_reaches_the_model(client, board, provider):
    _post(client, {"focus": "cost per lead"})
    assert "cost per lead" in provider.calls[0][1]["content"]


@pytest.mark.asyncio
async def test_board_without_data_skips_the_model(mock_db, client, provider):
    doc = _doc()
    doc["widgets"] = doc["widgets"][1:]
    await _seed(mock_db, doc)
    body = _post(client).json()
    assert provider.calls == []
    assert body["findings"] == [] and body["cached"] is False
    assert body["parts"][0]["text"] == briefing.NO_DATA_LEAD


# --- cache --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_returns_cached_true(client, board, provider):
    first = _post(client).json()
    second = _post(client).json()
    assert first["cached"] is False and second["cached"] is True
    assert second["parts"] == first["parts"] and second["generated_at"] == first["generated_at"]
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_cache_is_keyed_by_inputs(client, board, provider, mock_db):
    _post(client)
    _post(client, {"focus": "cost"})
    _post(client, {"window": {"preset": "last_7d"}})
    assert len(provider.calls) == 3
    await mock_db["dashboards"].update_one({"id": "d1"}, {"$set": {"doc.meta.updatedAt": "2026-10-02T00:00:00.000Z"}})
    _post(client)  # the board was edited: a new briefing
    assert len(provider.calls) == 4


@pytest.mark.asyncio
async def test_expired_cache_entry_is_not_served(client, board, provider, mock_db):
    from datetime import timedelta
    _post(client)
    await mock_db["dashboard_briefings"].update_many({}, {"$set": {"created_at": briefing._now() - timedelta(hours=7)}})
    assert _post(client).json()["cached"] is False
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_refresh_bypasses_cache(client, board, provider):
    _post(client)
    refreshed = _post(client, {"refresh": True}).json()
    assert refreshed["cached"] is False
    assert len(provider.calls) == 2
    assert _post(client).json()["cached"] is True  # the refresh replaced the stored briefing


@pytest.mark.asyncio
async def test_cache_is_per_user(mock_db, client, provider):
    await _seed(mock_db, user="bob@example.com")
    await _seed(mock_db)
    _post(client)
    app.dependency_overrides[get_current_user] = lambda: "bob@example.com"
    assert _post(client).json()["cached"] is False
    assert len(provider.calls) == 2


# --- rate limit and errors ------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_429(client, board, provider):
    for _ in range(briefing.MAX_REFRESHES_PER_HOUR):
        assert _post(client, {"refresh": True}).status_code == 200
    assert _post(client, {"refresh": True}).status_code == 429
    assert len(provider.calls) == briefing.MAX_REFRESHES_PER_HOUR
    assert _post(client).status_code == 200  # reading the stored briefing is not limited


@pytest.mark.asyncio
async def test_rate_limit_is_per_user_and_rolls(client, board, mock_db):
    from datetime import timedelta
    await mock_db["dashboard_briefing_calls"].insert_many(
        [{"user_id": "bob@example.com", "at": briefing._now()} for _ in range(10)]
        + [{"user_id": USER, "at": briefing._now() - timedelta(hours=2)} for _ in range(10)]
    )
    assert _post(client, {"refresh": True}).status_code == 200


@pytest.mark.asyncio
async def test_llm_error_502(client, board, provider):
    provider.error = RuntimeError("upstream down")
    r = _post(client)
    assert r.status_code == 502
    assert _post(client, {"refresh": True}).status_code == 502


@pytest.mark.asyncio
async def test_unparseable_model_reply_502(client, board, provider):
    provider.reply = "Sorry, I can't help with that."
    assert _post(client).status_code == 502


@pytest.mark.asyncio
async def test_fenced_json_is_accepted(client, board, provider):
    provider.reply = "```json\n" + json.dumps(ANSWER) + "\n```"
    assert _post(client).status_code == 200


@pytest.mark.asyncio
async def test_other_users_dashboard_404(mock_db, client, provider):
    await _seed(mock_db, user="bob@example.com")
    r = _post(client)
    assert r.status_code == 404
    assert provider.calls == []


def test_missing_dashboard_404(client):
    assert _post(client, dash_id="nope").status_code == 404


# --- indexes ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_indexes(mock_mongo_client, mock_db):
    await briefing.create_dashboard_briefing_indexes(mock_mongo_client)
    cache = await mock_db["dashboard_briefings"].index_information()
    calls = await mock_db["dashboard_briefing_calls"].index_information()
    assert cache["ttl_dashboard_briefings"]["expireAfterSeconds"] == 6 * 3600
    assert calls["ttl_dashboard_briefing_calls"]["expireAfterSeconds"] == 3600


@pytest.mark.asyncio
async def test_body_is_optional(client, board):
    assert client.post("/api/dashboards/d1/briefing").status_code == 200
