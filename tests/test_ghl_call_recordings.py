"""
tests/test_ghl_call_recordings.py
---------------------------------
GHL calls on the lead page: call messages returned as calls (minus the ones a
webhook already logged), and the recording / transcript fetch with its
ownership check. GHLIntegration methods are mocked; no network.
"""

import contextlib
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import routers.leads as leads_router
import services.ghl_live as gl
import services.ghl_messages as gm
from core.database import DB_NAME
from dependencies import get_current_user
from main import app

LOC = "loc1"
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt "


@pytest.fixture
def tokens(monkeypatch):
    async def fake(user_id, client):
        return {LOC: {"access_token": "tok"}}

    monkeypatch.setattr(gl, "get_subaccount_tokens", fake)


@pytest.fixture
def ghl(monkeypatch, tokens):
    mocks = {
        "export_messages": AsyncMock(return_value=(200, {"messages": [], "nextCursor": None})),
        "get_message": AsyncMock(return_value=(200, {
            "id": "m1", "messageType": "TYPE_CALL", "contactId": "c1", "locationId": LOC})),
        "fetch_call_recording": AsyncMock(return_value=(200, WAV, "audio/x-wav")),
        "fetch_call_transcription": AsyncMock(return_value=(200, [])),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(gm.ghl_integration, name, mock)
    return mocks


def _call(id_, date, **extra):
    return {"id": id_, "messageType": "TYPE_CALL", "dateAdded": date, "direction": "outbound", **extra}


# ── calls in the messages payload ───────────────────────────────────────────

async def test_calls_returned_with_either_meta_shape(ghl, mock_mongo_client):
    ghl["export_messages"].return_value = (200, {"messages": [
        _call("m2", "2026-01-02T10:00:00.000Z", meta={"callStatus": "completed", "callDuration": "95"}),
        _call("m1", "2026-01-01T10:00:00.000Z", meta={"call": {"status": "no-answer", "duration": 0}}),
        {"id": "s1", "messageType": "TYPE_SMS", "dateAdded": "2026-01-01T11:00:00.000Z", "body": "hi"},
        _call("bad", "not a date"),
    ], "nextCursor": None})

    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")

    assert [m["id"] for m in out["messages"]] == ["s1"]
    assert out["calls"] == [
        {"id": "ghl:m1", "ghl_message_id": "m1", "source": "ghl", "direction": "outbound",
         "status": "no-answer", "duration_seconds": 0, "started_at": "2026-01-01T10:00:00+00:00",
         "recording_url": None},
        {"id": "ghl:m2", "ghl_message_id": "m2", "source": "ghl", "direction": "outbound",
         "status": "completed", "duration_seconds": 95, "started_at": "2026-01-02T10:00:00+00:00",
         "recording_url": None},
    ]


async def test_calls_already_logged_by_webhook_are_dropped(ghl, mock_mongo_client):
    db = mock_mongo_client["db"]
    await db["call_logs"].insert_many([
        {"user_id": "u1", "source": "ghl", "source_event_id": "m1"},
        # Same id from another source or another user doesn't count.
        {"user_id": "u1", "source": "hotprospector", "source_event_id": "m2"},
        {"user_id": "u2", "source": "ghl", "source_event_id": "m3"},
    ])
    ghl["export_messages"].return_value = (200, {"messages": [
        _call("m1", "2026-01-01T10:00:00.000Z"),
        _call("m2", "2026-01-01T11:00:00.000Z"),
        _call("m3", "2026-01-01T12:00:00.000Z"),
    ], "nextCursor": None})

    out = await gm.fetch_lead_messages(db, "u1", LOC, "c1")

    assert [c["ghl_message_id"] for c in out["calls"]] == ["m2", "m3"]


# ── recording ───────────────────────────────────────────────────────────────

async def test_recording_returned_for_own_call(ghl, mock_mongo_client):
    audio, ctype = await gm.fetch_call_recording(mock_mongo_client["db"], "u1", LOC, "c1", "m1")

    assert (audio, ctype) == (WAV, "audio/x-wav")
    ghl["get_message"].assert_awaited_once_with("tok", "m1")
    ghl["fetch_call_recording"].assert_awaited_once_with(LOC, "tok", "m1")


async def test_generic_content_type_is_sniffed(ghl, mock_mongo_client):
    ghl["fetch_call_recording"].return_value = (200, WAV, "application/octet-stream")
    _, ctype = await gm.fetch_call_recording(mock_mongo_client["db"], "u1", LOC, "c1", "m1")
    assert ctype == "audio/wav"


async def test_non_audio_body_fails(ghl, mock_mongo_client):
    ghl["fetch_call_recording"].return_value = (200, b"<html>login</html>", "text/html")
    with pytest.raises(gm.MessagingFailed):
        await gm.fetch_call_recording(mock_mongo_client["db"], "u1", LOC, "c1", "m1")


@pytest.mark.parametrize("message", [
    {"id": "m1", "messageType": "TYPE_CALL", "contactId": "someone-else", "locationId": LOC},
    {"id": "m1", "messageType": "TYPE_CALL", "contactId": "c1", "locationId": "other-loc"},
    {"id": "m1", "messageType": "TYPE_SMS", "contactId": "c1", "locationId": LOC},
])
async def test_message_not_a_call_on_this_lead_is_not_found(ghl, mock_mongo_client, message):
    ghl["get_message"].return_value = (200, message)
    with pytest.raises(gm.RecordingNotFound):
        await gm.fetch_call_recording(mock_mongo_client["db"], "u1", LOC, "c1", "m1")
    ghl["fetch_call_recording"].assert_not_awaited()


@pytest.mark.parametrize("code,exc", [
    (401, gm.MessagingReconnect), (403, gm.MessagingReconnect),
    (404, gm.RecordingNotFound), (500, gm.MessagingFailed), (0, gm.MessagingFailed),
])
async def test_recording_status_mapping(ghl, mock_mongo_client, code, exc):
    ghl["fetch_call_recording"].return_value = (code, b"", "")
    with pytest.raises(exc):
        await gm.fetch_call_recording(mock_mongo_client["db"], "u1", LOC, "c1", "m1")


async def test_ownership_check_401_is_reconnect(ghl, mock_mongo_client):
    ghl["get_message"].return_value = (401, {})
    with pytest.raises(gm.MessagingReconnect):
        await gm.fetch_call_recording(mock_mongo_client["db"], "u1", LOC, "c1", "m1")


async def test_no_token_is_unavailable_without_ghl_call(ghl, mock_mongo_client):
    with pytest.raises(gm.MessagingUnavailable):
        await gm.fetch_call_recording(mock_mongo_client["db"], "u1", "unknown-loc", "c1", "m1")
    ghl["get_message"].assert_not_awaited()


# ── transcript ──────────────────────────────────────────────────────────────

async def test_transcript_joined_in_sentence_order(ghl, mock_mongo_client):
    ghl["fetch_call_transcription"].return_value = (200, [
        {"sentenceIndex": 2, "transcript": "Great, see you then."},
        {"sentenceIndex": 1, "transcript": "Hi, is that Rebecca?"},
        {"sentenceIndex": 3, "transcript": "  "},
    ])
    text = await gm.fetch_call_transcript(mock_mongo_client["db"], "u1", LOC, "c1", "m1")
    assert text == "Hi, is that Rebecca?\nGreat, see you then."


async def test_no_transcript_is_none(ghl, mock_mongo_client):
    ghl["fetch_call_transcription"].return_value = (404, [])
    assert await gm.fetch_call_transcript(mock_mongo_client["db"], "u1", LOC, "c1", "m1") is None


# ── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(leads_router, "get_mongo_client", fake_client)
    app.dependency_overrides[get_current_user] = lambda: "u1"
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


async def _seed(mock_mongo_client, user="u1"):
    await mock_mongo_client[DB_NAME]["ghl_contacts"].insert_one(
        {"user_id": user, "location_id": LOC, "contact_data": {"id": "c1"}})


async def test_recording_route_streams_audio(api, mock_mongo_client, monkeypatch):
    await _seed(mock_mongo_client)
    fetch = AsyncMock(return_value=(WAV, "audio/wav"))
    monkeypatch.setattr(leads_router, "fetch_call_recording", fetch)

    resp = api.get("/api/leads/contact/c1/calls/m1/recording")

    assert resp.status_code == 200
    assert resp.content == WAV
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["cache-control"] == "private, max-age=3600"
    assert fetch.await_args.args[1:] == ("u1", LOC, "c1", "m1")


async def test_recording_route_404_for_other_users_contact(api, mock_mongo_client, monkeypatch):
    await _seed(mock_mongo_client, user="someone-else")
    fetch = AsyncMock()
    monkeypatch.setattr(leads_router, "fetch_call_recording", fetch)

    resp = api.get("/api/leads/contact/c1/calls/m1/recording")

    assert resp.status_code == 404
    fetch.assert_not_awaited()


@pytest.mark.parametrize("exc,code", [
    (gm.RecordingNotFound, 404), (gm.MessagingReconnect, 409),
    (gm.MessagingUnavailable, 409), (gm.MessagingFailed, 502),
])
async def test_recording_route_errors(api, mock_mongo_client, monkeypatch, exc, code):
    await _seed(mock_mongo_client)
    monkeypatch.setattr(leads_router, "fetch_call_recording", AsyncMock(side_effect=exc()))
    assert api.get("/api/leads/contact/c1/calls/m1/recording").status_code == code


async def test_bad_message_id_is_rejected(api, mock_mongo_client, monkeypatch):
    await _seed(mock_mongo_client)
    fetch = AsyncMock()
    monkeypatch.setattr(leads_router, "fetch_call_recording", fetch)
    assert api.get("/api/leads/contact/c1/calls/m1..x/recording").status_code == 422
    fetch.assert_not_awaited()


async def test_transcript_route(api, mock_mongo_client, monkeypatch):
    await _seed(mock_mongo_client)
    monkeypatch.setattr(leads_router, "fetch_call_transcript", AsyncMock(return_value="Hello"))
    resp = api.get("/api/leads/contact/c1/calls/m1/transcript")
    assert resp.status_code == 200
    assert resp.json() == {"transcript": "Hello"}
