"""Lead MCP tools (ai/mcp/leads_mcp.py)."""

import pytest
from fastmcp.exceptions import ToolError

from ai.mcp import leads_mcp as t
from services.ghl_messages import (
    MessagingFailed, MessagingReconnect, MessagingUnavailable, RecordingNotFound,
)

ALICE, BOB = "alice@example.com", "bob@example.com"

pytestmark = pytest.mark.asyncio


async def _seed(db, contact_id="c1", user=ALICE, group="g1", location="loc1"):
    await db["client_groups"].insert_one({"id": group, "user_id": user, "name": "Acme"})
    await db["ghl_contacts"].insert_one({
        "user_id": user, "client_group_id": group, "client_group_name": "Acme", "location_id": location,
        "contact_data": {"id": contact_id, "firstName": "Ada", "lastName": "Lovelace", "email": "ada@example.com"},
    })


@pytest.fixture
def alice(set_current_user):
    set_current_user(ALICE)


@pytest.fixture
def bob(set_current_user):
    set_current_user(BOB)


@pytest.fixture
def stubs(monkeypatch):
    calls = {}

    async def summary(db, user_id, contact_id, refresh=False):
        calls["summary"] = (user_id, contact_id)
        return {"status": "ok", "summary": "Hot lead", "generated_at": None, "stale": False}

    async def messages(db, user_id, location_id, contact_id):
        calls["messages"] = (user_id, location_id, contact_id)
        return {"status": "ok", "messages": [{"id": "m1"}], "calls": []}

    async def transcript(db, user_id, location_id, contact_id, message_id):
        calls["transcript"] = (user_id, location_id, contact_id, message_id)
        return "Hello there"

    monkeypatch.setattr(t, "get_lead_summary", summary)
    monkeypatch.setattr(t, "fetch_lead_messages", messages)
    monkeypatch.setattr(t, "fetch_call_transcript", transcript)
    return calls


async def test_tool_tags_and_docstrings():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    for name in ("get_lead", "get_lead_messages", "get_call_transcript"):
        assert tools[name].tags == {"read"}
        assert len(tools[name].description) > 60


async def test_get_lead_combines_detail_journey_summary(mock_db, alice, stubs):
    await _seed(mock_db)
    out = await t.get_lead.fn(contact_id="c1")
    assert out["lead"]["contact"]["id"] == "c1"
    assert out["lead"]["contact"]["group_id"] == "g1"
    assert "events" in out["journey"] or "timeline" in out["journey"] or isinstance(out["journey"], dict)
    assert out["summary"]["summary"] == "Hot lead"
    assert stubs["summary"] == (ALICE, "c1")


async def test_lead_of_other_user_not_found(mock_db, bob, stubs):
    await _seed(mock_db, user=ALICE)
    with pytest.raises(ToolError, match="Lead not found"):
        await t.get_lead.fn(contact_id="c1")
    with pytest.raises(ToolError, match="Lead not found"):
        await t.get_lead_messages.fn(contact_id="c1")
    with pytest.raises(ToolError, match="Lead not found"):
        await t.get_call_transcript.fn(contact_id="c1", message_id="m1")
    assert "summary" not in stubs and "messages" not in stubs and "transcript" not in stubs


async def test_contact_in_group_not_owned_is_not_found(mock_db, alice, stubs):
    # the contact row carries alice's user_id but points at a group that is not hers
    await mock_db["ghl_contacts"].insert_one({
        "user_id": ALICE, "client_group_id": "foreign", "location_id": "loc9",
        "contact_data": {"id": "c9", "firstName": "X"},
    })
    with pytest.raises(ToolError, match="Lead not found"):
        await t.get_lead_messages.fn(contact_id="c9")


async def test_get_lead_messages_passes_location(mock_db, alice, stubs):
    await _seed(mock_db)
    out = await t.get_lead_messages.fn(contact_id="c1")
    assert out["messages"] == [{"id": "m1"}]
    assert stubs["messages"] == (ALICE, "loc1", "c1")


async def test_reconnect_error_message(mock_db, alice, stubs, monkeypatch):
    await _seed(mock_db)

    async def reconnect(*a, **k):
        return {"status": "reconnect", "messages": [], "calls": []}

    monkeypatch.setattr(t, "fetch_lead_messages", reconnect)
    out = await t.get_lead_messages.fn(contact_id="c1")
    assert out == {"error": "GoHighLevel needs reconnecting in Birdy (Settings → Integrations)."}

    async def raises(*a, **k):
        raise MessagingReconnect()

    monkeypatch.setattr(t, "fetch_call_transcript", raises)
    out = await t.get_call_transcript.fn(contact_id="c1", message_id="m1")
    assert out == {"error": "GoHighLevel needs reconnecting in Birdy (Settings → Integrations)."}


async def test_messaging_unavailable_error_dict(mock_db, alice, stubs, monkeypatch):
    await _seed(mock_db)

    async def unavailable(*a, **k):
        return {"status": "unavailable", "messages": [], "calls": []}

    monkeypatch.setattr(t, "fetch_lead_messages", unavailable)
    out = await t.get_lead_messages.fn(contact_id="c1")
    assert set(out) == {"error"} and "GoHighLevel" in out["error"]

    async def raises(*a, **k):
        raise MessagingUnavailable()

    monkeypatch.setattr(t, "fetch_call_transcript", raises)
    out = await t.get_call_transcript.fn(contact_id="c1", message_id="m1")
    assert set(out) == {"error"} and "GoHighLevel" in out["error"]

    async def failed(*a, **k):
        raise MessagingFailed()

    monkeypatch.setattr(t, "fetch_call_transcript", failed)
    out = await t.get_call_transcript.fn(contact_id="c1", message_id="m1")
    assert set(out) == {"error"}


async def test_get_call_transcript(mock_db, alice, stubs):
    await _seed(mock_db)
    out = await t.get_call_transcript.fn(contact_id="c1", message_id="m1")
    assert out == {"transcript": "Hello there"}
    assert stubs["transcript"] == (ALICE, "loc1", "c1", "m1")


async def test_transcript_message_of_other_contact_refused(mock_db, alice, monkeypatch):
    """Real ownership check in services.ghl_messages: the GHL message belongs to another contact."""
    await _seed(mock_db)
    from services import ghl_messages as gm

    async def token_for(db, user_id, location_id):
        return "tok"

    async def get_message(token, message_id):
        return 200, {"contactId": "someone-else", "locationId": "loc1", "messageType": "TYPE_CALL"}

    async def no_fetch(*a, **k):
        raise AssertionError("transcript fetched for a foreign call")

    monkeypatch.setattr(gm, "_token_for", token_for)
    monkeypatch.setattr(gm.ghl_integration, "get_message", get_message)
    monkeypatch.setattr(gm.ghl_integration, "fetch_call_transcription", no_fetch)
    with pytest.raises(ToolError, match="No call"):
        await t.get_call_transcript.fn(contact_id="c1", message_id="m1")


async def test_invalid_message_id_rejected(mock_db, alice, stubs):
    await _seed(mock_db)
    with pytest.raises(ToolError):
        await t.get_call_transcript.fn(contact_id="c1", message_id="../etc")
