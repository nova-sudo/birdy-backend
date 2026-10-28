"""
tests/test_ghl_messages.py
--------------------------
services/ghl_messages.py: live GHL message fetch (merge, paginate, status
mapping) and send. GHLIntegration methods are mocked; no network.
"""

from unittest.mock import AsyncMock

import pytest

import services.ghl_messages as gm

LOC = "loc1"


@pytest.fixture
def ghl(monkeypatch):
    export = AsyncMock(return_value=(200, {"messages": [], "nextCursor": None}))
    send = AsyncMock(return_value=(201, {"conversationId": "cv1", "messageId": "m-new"}))
    monkeypatch.setattr(gm.ghl_integration, "export_messages", export)
    monkeypatch.setattr(gm.ghl_integration, "send_message", send)

    async def tokens(user_id, client):
        return {LOC: {"access_token": "tok"}}

    monkeypatch.setattr(gm, "get_subaccount_tokens", tokens)
    return export, send


def _msg(id, type_, date, body="hi", direction="outbound", **extra):
    return {"id": id, "messageType": type_, "dateAdded": date, "body": body,
            "direction": direction, "status": "delivered", **extra}


async def test_messages_merged_sorted_and_calls_dropped(ghl, mock_mongo_client):
    export, _ = ghl

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 200, {"messages": [_msg("e1", "TYPE_EMAIL", "2026-01-02T10:00:00.000Z")], "nextCursor": None}
        return 200, {"messages": [
            _msg("s2", "TYPE_SMS", "2026-01-03T10:00:00.000Z"),
            _msg("c1", "TYPE_CALL", "2026-01-01T10:00:00.000Z"),
            _msg("v1", "TYPE_CAMPAIGN_VOICEMAIL", "2026-01-01T11:00:00.000Z"),
            _msg("s1", "TYPE_SMS", "2026-01-01T09:00:00.000Z"),
            _msg("s1", "TYPE_SMS", "2026-01-01T09:00:00.000Z"),
        ], "nextCursor": None}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")

    assert out["status"] == "ok"
    assert [m["id"] for m in out["messages"]] == ["s1", "e1", "s2"]
    assert out["messages"][0]["date"].endswith("+00:00")


async def test_paginates_until_no_cursor_capped_at_four_pages(ghl, mock_mongo_client):
    export, _ = ghl
    n = {"i": 0}

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 200, {"messages": [], "nextCursor": None}
        n["i"] += 1
        return 200, {"messages": [_msg(f"s{n['i']}", "TYPE_SMS", "2026-01-01T09:00:00.000Z")],
                     "nextCursor": f"cur{n['i']}"}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")

    default_calls = [c for c in export.call_args_list if c.kwargs.get("channel") is None]
    assert len(default_calls) == 4
    assert default_calls[1].kwargs["cursor"] == "cur1"
    assert len(out["messages"]) == 4


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_failure_means_reconnect(ghl, mock_mongo_client, code):
    export, _ = ghl
    export.return_value = (code, {})
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert out == {"status": "reconnect", "messages": []}


async def test_no_location_means_unavailable_without_ghl_call(ghl, mock_mongo_client):
    export, _ = ghl
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", None, "c1")
    assert out == {"status": "unavailable", "messages": []}
    export.assert_not_called()


async def test_timeout_means_unavailable(ghl, mock_mongo_client):
    export, _ = ghl
    export.return_value = (0, {})
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert out == {"status": "unavailable", "messages": []}


async def test_email_html_stripped_and_subject_kept(ghl, mock_mongo_client):
    export, _ = ghl

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 200, {"messages": [_msg(
                "e1", "TYPE_EMAIL", "2026-01-02T10:00:00.000Z",
                body="<div>Hello <b>Ada</b></div>\n<p>See   you</p>",
                meta={"email": {"subject": "Quote"}})], "nextCursor": None}
        return 200, {"messages": [], "nextCursor": None}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")

    m = out["messages"][0]
    assert m["channel"] == "Email"
    assert m["subject"] == "Quote"
    assert m["body"] == "Hello Ada See you"


@pytest.mark.parametrize("mtype,channel", [
    ("TYPE_SMS", "SMS"), ("TYPE_CUSTOM_SMS", "SMS"), ("TYPE_CAMPAIGN_SMS", "SMS"),
    ("TYPE_CAMPAIGN_MANUAL_SMS", "SMS"), ("TYPE_CUSTOM_PROVIDER_SMS", "SMS"), ("TYPE_RCS", "SMS"),
    ("TYPE_EMAIL", "Email"), ("TYPE_CAMPAIGN_EMAIL", "Email"), ("TYPE_CUSTOM_PROVIDER_EMAIL", "Email"),
    ("TYPE_WHATSAPP", "WhatsApp"), ("TYPE_INSTAGRAM", "Instagram"), ("TYPE_INSTAGRAM_COMMENT", "Instagram"),
    ("TYPE_FACEBOOK", "Facebook"), ("TYPE_FACEBOOK_COMMENT", "Facebook"),
    ("TYPE_LIVE_CHAT", "Chat"), ("TYPE_WEBCHAT", "Chat"),
    ("TYPE_GMB", "Other"), ("TYPE_ACTIVITY_CONTACT", "Other"),
])
def test_channel_mapping(mtype, channel):
    assert gm.channel_for(mtype) == channel


async def test_send_returns_outbound_message(ghl, mock_mongo_client):
    _, send = ghl
    m = await gm.send_lead_message(mock_mongo_client["db"], "u1", LOC, "c1", "SMS", "Hello")
    send.assert_awaited_once_with("tok", {"type": "SMS", "contactId": "c1", "message": "Hello"})
    assert m["id"] == "m-new" and m["direction"] == "outbound" and m["status"] == "sent"
    assert m["channel"] == "SMS"


async def test_send_email_payload(ghl, mock_mongo_client):
    _, send = ghl
    await gm.send_lead_message(mock_mongo_client["db"], "u1", LOC, "c1", "Email", "a < b", subject="Hi")
    payload = send.call_args.args[1]
    assert payload["type"] == "Email" and payload["subject"] == "Hi"
    assert payload["html"] == "a &lt; b"


async def test_send_errors(ghl, mock_mongo_client):
    _, send = ghl
    db = mock_mongo_client["db"]
    with pytest.raises(gm.MessagingUnavailable):
        await gm.send_lead_message(db, "u1", None, "c1", "SMS", "x")
    send.return_value = (403, {})
    with pytest.raises(gm.MessagingReconnect):
        await gm.send_lead_message(db, "u1", LOC, "c1", "SMS", "x")
    send.return_value = (500, {})
    with pytest.raises(gm.MessagingFailed):
        await gm.send_lead_message(db, "u1", LOC, "c1", "SMS", "x")
    send.return_value = (0, {})
    with pytest.raises(gm.MessagingFailed):
        await gm.send_lead_message(db, "u1", LOC, "c1", "SMS", "x")
