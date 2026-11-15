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


# ---------------------------------------------------------------------------
# send_lead_message (LIVE, two steps): a real SMS/email goes out through GoHighLevel
# ---------------------------------------------------------------------------

from services.ghl_messages import MessagingUnconfirmed  # noqa: E402


async def _seed_sendable(db, user=ALICE, contact_id="c1", group="g1", phone="+15550004821", email="ada@acme.com"):
    await db["client_groups"].insert_one({"id": group, "user_id": user, "name": "Acme"})
    cd = {"id": contact_id, "firstName": "Ada", "lastName": "Lovelace"}
    if phone is not None:
        cd["phone"] = phone
    if email is not None:
        cd["email"] = email
    await db["ghl_contacts"].insert_one({"user_id": user, "client_group_id": group, "location_id": "loc1", "contact_data": cd})


@pytest.fixture
def sender(monkeypatch):
    """The send service and the token check, mocked at the service boundary. sent = every real send attempt."""
    state = {"sent": [], "token": "tok", "raises": None}

    async def send(db, user_id, location_id, contact_id, channel, message, subject=None):
        state["sent"].append((user_id, location_id, contact_id, channel, message, subject))
        if state["raises"]:
            raise state["raises"]
        return {"id": "m9", "channel": channel, "direction": "outbound", "body": message, "subject": subject, "status": "sent"}

    async def token(db, user_id, location_id):
        return state["token"]

    monkeypatch.setattr(t, "send_lead_message_service", send)
    monkeypatch.setattr(t, "safe_token_for", token)
    return state


async def test_send_tool_tags_and_docstring():
    from ai.mcp import mcp
    tool = (await mcp.get_tools())["send_lead_message"]
    assert tool.tags == {"write", "live"}
    assert "Show the preview to the user and only call again with confirm_code after they say yes." in tool.description


async def test_send_needs_confirmation_and_sends_exact_body(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    body = "Hi Ada,\n  thanks for your interest!  "
    first = await t.send_lead_message.fn(contact_id="c1", channel="sms", body=body)
    assert first["needs_confirmation"] and first["confirm_code"]
    assert sender["sent"] == []
    p = first["preview"]
    assert p["to"] == "Ada Lovelace" and p["channel"] == "sms"
    assert p["phone"] == "•••• 4821" and "5550004821" not in str(first)
    assert p["body"] == body.strip()
    out = await t.send_lead_message.fn(contact_id="c1", channel="sms", body=body, confirm_code=first["confirm_code"])
    assert out["ok"] is True and out["message"]["id"] == "m9"
    # the same text the preview showed is what went out (GHL channel name, nothing re-normalized)
    assert sender["sent"] == [(ALICE, "loc1", "c1", "SMS", p["body"], None)]


async def test_email_preview_masks_address_and_sends_subject(mock_db, alice, sender):
    await _seed_sendable(mock_db, email="jane@acme.com")
    first = await t.send_lead_message.fn(contact_id="c1", channel="email", body="Hello", subject=" Quick question ")
    assert first["preview"]["email"] == "j•••@acme.com" and "jane" not in str(first)
    assert first["preview"]["subject"] == "Quick question"
    await t.send_lead_message.fn(contact_id="c1", channel="email", body="Hello", subject=" Quick question ",
                                 confirm_code=first["confirm_code"])
    assert sender["sent"] == [(ALICE, "loc1", "c1", "Email", "Hello", "Quick question")]


async def test_email_needs_subject_and_address(mock_db, alice, sender):
    await _seed_sendable(mock_db, email=None)
    for subject in (None, "   "):
        out = await t.send_lead_message.fn(contact_id="c1", channel="email", body="Hi", subject=subject)
        assert "error" in out and "confirm_code" not in out
    assert "no email" in (await t.send_lead_message.fn(contact_id="c1", channel="email", body="Hi", subject="S"))["error"].lower()
    assert sender["sent"] == []


async def test_changed_body_invalidates_code(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    code = (await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello"))["confirm_code"]
    out = await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello!", confirm_code=code)
    assert "error" in out and sender["sent"] == []
    # even an invisible difference is a different message
    out = await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello ", confirm_code=code)
    assert "error" in out and sender["sent"] == []


async def test_code_for_other_contact_or_channel_or_subject_refused(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    await _seed_sendable(mock_db, contact_id="c2", group="g2")
    code = (await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello"))["confirm_code"]
    assert "error" in await t.send_lead_message.fn(contact_id="c2", channel="sms", body="Hello", confirm_code=code)
    assert "error" in await t.send_lead_message.fn(contact_id="c1", channel="email", body="Hello", subject="S", confirm_code=code)
    assert "error" in await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello", subject="S", confirm_code=code)
    assert sender["sent"] == []


async def test_code_cannot_be_used_twice(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    code = (await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello"))["confirm_code"]
    assert (await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello", confirm_code=code))["ok"]
    again = await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hello", confirm_code=code)
    assert "error" in again and len(sender["sent"]) == 1


async def test_empty_body_rejected(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    for body in ("", "   \n"):
        out = await t.send_lead_message.fn(contact_id="c1", channel="sms", body=body)
        assert "error" in out and "confirm_code" not in out
    assert sender["sent"] == []


async def test_too_long_rejected(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    assert "confirm_code" in await t.send_lead_message.fn(contact_id="c1", channel="sms", body="x" * 1600)
    out = await t.send_lead_message.fn(contact_id="c1", channel="sms", body="x" * 1601)
    assert "error" in out and "confirm_code" not in out
    out = await t.send_lead_message.fn(contact_id="c1", channel="email", body="x" * 1601, subject="S")
    assert "error" in out and "confirm_code" not in out
    out = await t.send_lead_message.fn(contact_id="c1", channel="email", body="x", subject="s" * 301)
    assert "error" in out and "confirm_code" not in out


async def test_unknown_channel_rejected(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    out = await t.send_lead_message.fn(contact_id="c1", channel="whatsapp", body="Hi")
    assert "error" in out and "confirm_code" not in out


async def test_lead_without_phone_gets_error_and_no_code(mock_db, alice, sender):
    await _seed_sendable(mock_db, phone=None)
    out = await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hi")
    assert "no phone" in out["error"].lower() and "confirm_code" not in out
    assert sender["sent"] == []


async def test_ghl_needs_reconnect_gives_no_code(mock_db, alice, sender):
    await _seed_sendable(mock_db)
    sender["token"] = None
    out = await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hi")
    assert out == {"error": "GoHighLevel needs reconnecting in Birdy (Settings → Integrations)."}
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0


async def test_send_errors_come_back_as_dicts(mock_db, alice, sender):
    await _seed_sendable(mock_db)

    async def run(exc):
        sender["raises"] = exc
        code = (await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hi"))["confirm_code"]
        return await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hi", confirm_code=code)

    out = await run(MessagingUnconfirmed())
    assert out["ok"] is False and "may still have been sent" in out["error"].lower()
    out = await run(MessagingFailed())
    assert out["ok"] is False and "rejected" in out["error"].lower()
    out = await run(MessagingReconnect())
    assert out["error"].startswith("GoHighLevel needs reconnecting")
    out = await run(MessagingUnavailable())
    assert out["error"].startswith("GoHighLevel needs reconnecting")


async def test_other_users_lead_cannot_be_previewed_or_sent(mock_db, set_current_user, sender):
    await _seed_sendable(mock_db, user=ALICE)
    set_current_user(ALICE)
    code = (await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hi"))["confirm_code"]
    set_current_user(BOB)
    with pytest.raises(ToolError, match="Lead not found"):
        await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hi")
    with pytest.raises(ToolError, match="Lead not found"):
        await t.send_lead_message.fn(contact_id="c1", channel="sms", body="Hi", confirm_code=code)
    assert sender["sent"] == []


async def test_read_scope_cannot_send(mcp_call, mock_db, alice, sender):
    await _seed_sendable(mock_db)
    args = {"contact_id": "c1", "channel": "sms", "body": "Hi"}
    res = await mcp_call("send_lead_message", args, scope="birdy.read", token_type="mcp")
    assert res.is_error and "view-only" in res.text
    res = await mcp_call("send_lead_message", args, scope="birdy.write", token_type="mcp")
    assert not res.is_error and "confirm_code" in res.text
    assert sender["sent"] == []


async def test_route_and_tool_share_the_same_checks():
    """The HTTP route delegates its lead checks to the service the tool uses."""
    from services import ghl_messages as gm
    assert gm.check_sendable({"phone": "1"}, "SMS", None) is None
    with pytest.raises(gm.MessageInvalid, match="no phone"):
        gm.check_sendable({}, "SMS", None)
    with pytest.raises(gm.MessageInvalid, match="Subject is required"):
        gm.check_sendable({"email": "a@b.co"}, "Email", " ")
    with pytest.raises(gm.MessageInvalid, match="no email"):
        gm.check_sendable({}, "Email", "S")
    assert gm.check_sendable({"email": "a@b.co"}, "Email", " S ") == "S"
