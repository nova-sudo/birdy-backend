"""
tests/test_ghl_messages.py
--------------------------
services/ghl_messages.py: live GHL message fetch (merge, paginate, status
mapping) and send. GHLIntegration methods are mocked; no network.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import integrations.gohighlevel as gh
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
    ("TYPE_SMS_REVIEW_REQUEST", "SMS"), ("TYPE_SMS_NO_SHOW_REQUEST", "SMS"),
    ("TYPE_CAMPAIGN_FACEBOOK", "Facebook"),
    ("TYPE_GMB", "Other"), ("TYPE_REVIEW", "Other"),
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
    for code in (400, 422, 429):
        send.return_value = (code, {})
        with pytest.raises(gm.MessagingFailed):
            await gm.send_lead_message(db, "u1", LOC, "c1", "SMS", "x")


@pytest.mark.parametrize("code", [0, 500, 502, 504])
async def test_send_unknown_outcome_is_unconfirmed(ghl, mock_mongo_client, code):
    _, send = ghl
    send.return_value = (code, {})
    with pytest.raises(gm.MessagingUnconfirmed):
        await gm.send_lead_message(mock_mongo_client["db"], "u1", LOC, "c1", "SMS", "x")


async def test_email_failure_keeps_default_messages(ghl, mock_mongo_client):
    export, _ = ghl

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 500, {}
        return 200, {"messages": [_msg("s1", "TYPE_SMS", "2026-01-01T09:00:00.000Z")], "nextCursor": None}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert out["status"] == "ok" and out["partial"] is True
    assert [m["id"] for m in out["messages"]] == ["s1"]


async def test_default_failure_keeps_email_messages(ghl, mock_mongo_client):
    export, _ = ghl

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 200, {"messages": [_msg("e1", "TYPE_EMAIL", "2026-01-01T09:00:00.000Z")], "nextCursor": None}
        return 0, {}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert out["status"] == "ok" and out["partial"] is True


async def test_auth_failure_on_one_channel_is_reconnect(ghl, mock_mongo_client):
    export, _ = ghl

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 403, {}
        return 200, {"messages": [_msg("s1", "TYPE_SMS", "2026-01-01T09:00:00.000Z")], "nextCursor": None}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert out == {"status": "reconnect", "messages": []}


async def test_no_token_for_location_means_unavailable(ghl, monkeypatch, mock_mongo_client):
    export, _ = ghl

    async def tokens(user_id, client):
        return {"other": {"access_token": "x"}}

    monkeypatch.setattr(gm, "get_subaccount_tokens", tokens)
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert out == {"status": "unavailable", "messages": []}
    export.assert_not_called()


async def test_newest_messages_survive_the_cap(ghl, mock_mongo_client):
    export, _ = ghl
    # GHL is asked newest-first; 5 pages exist, only 4 are fetched.
    pages = {None: [(f"p{i}", f"2026-01-{10 - i:02d}T09:00:00.000Z") for i in range(5)]}
    n = {"i": 0}

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 200, {"messages": [], "nextCursor": None}
        mid, date = pages[None][n["i"]]
        n["i"] += 1
        return 200, {"messages": [_msg(mid, "TYPE_SMS", date)], "nextCursor": "more"}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    ids = [m["id"] for m in out["messages"]]
    assert ids == ["p3", "p2", "p1", "p0"]  # oldest page (p4) dropped, ascending
    assert out["truncated"] is True


async def test_not_truncated_when_cursor_ends(ghl, mock_mongo_client):
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert out["truncated"] is False and out["partial"] is False


async def test_dates_normalized_to_utc_and_sorted_chronologically(ghl, mock_mongo_client):
    export, _ = ghl

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 200, {"messages": [], "nextCursor": None}
        return 200, {"messages": [
            _msg("a", "TYPE_SMS", "2026-01-01T12:00:00+02:00"),   # 10:00Z
            _msg("b", "TYPE_SMS", "2026-01-01T11:00:00+00:00"),   # 11:00Z
        ], "nextCursor": None}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert [m["id"] for m in out["messages"]] == ["a", "b"]
    assert out["messages"][0]["date"] == "2026-01-01T10:00:00+00:00"


async def test_rows_without_id_and_non_messages_dropped(ghl, mock_mongo_client):
    export, _ = ghl

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        if channel == "Email":
            return 200, {"messages": [], "nextCursor": None}
        return 200, {"messages": [
            _msg(None, "TYPE_SMS", "2026-01-01T09:00:00.000Z"),
            _msg("x1", "TYPE_SMS", "2026-01-01T09:00:00.000Z"),
            _msg("x2", "TYPE_ACTIVITY_OPPORTUNITY", "2026-01-01T09:00:00.000Z"),
            _msg("x3", "TYPE_INTERNAL_COMMENT", "2026-01-01T09:00:00.000Z"),
            _msg("x4", "TYPE_LIVE_CHAT_INFO_MESSAGE", "2026-01-01T09:00:00.000Z"),
            _msg("x5", "TYPE_FORM_SUBMISSION", "2026-01-01T09:00:00.000Z"),
            _msg("x6", "TYPE_SMS_REACTION", "2026-01-01T09:00:00.000Z"),
        ], "nextCursor": None}

    export.side_effect = side
    out = await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert [m["id"] for m in out["messages"]] == ["x1"]


async def test_channels_fetched_concurrently(ghl, mock_mongo_client):
    export, _ = ghl
    running = {"n": 0, "max": 0}

    async def side(location_id, token, contact_id, channel=None, cursor=None):
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        await asyncio.sleep(0.01)
        running["n"] -= 1
        return 200, {"messages": [], "nextCursor": None}

    export.side_effect = side
    await gm.fetch_lead_messages(mock_mongo_client["db"], "u1", LOC, "c1")
    assert running["max"] == 2


async def test_send_token_lookup_error_is_not_a_500(ghl, monkeypatch, mock_mongo_client):
    async def boom(user_id, client):
        raise RuntimeError("db down")

    monkeypatch.setattr(gm, "get_subaccount_tokens", boom)
    with pytest.raises(gm.MessagingFailed):
        await gm.send_lead_message(mock_mongo_client["db"], "u1", LOC, "c1", "SMS", "x")


async def test_send_no_token_for_location_unavailable(ghl, monkeypatch, mock_mongo_client):
    async def tokens(user_id, client):
        return {}

    monkeypatch.setattr(gm, "get_subaccount_tokens", tokens)
    with pytest.raises(gm.MessagingUnavailable):
        await gm.send_lead_message(mock_mongo_client["db"], "u1", LOC, "c1", "SMS", "x")


# --- GHLIntegration HTTP methods (httpx patched) ---------------------------

def _patch_httpx(monkeypatch, response=None, error=None):
    client = MagicMock()
    if error:
        client.get = AsyncMock(side_effect=error)
        client.post = AsyncMock(side_effect=error)
    else:
        client.get = AsyncMock(return_value=response)
        client.post = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    monkeypatch.setattr(gh.httpx, "AsyncClient", factory)
    return factory, client


def _resp(status, json_data=None, bad_json=False):
    r = MagicMock()
    r.status_code = status
    r.text = ""
    if bad_json:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_data
    return r


async def test_export_messages_request_shape(monkeypatch):
    factory, client = _patch_httpx(monkeypatch, _resp(200, {"messages": [], "nextCursor": None}))
    code, body = await gh.GHLIntegration.export_messages(
        gh.ghl_integration, "loc1", "tok", "c1", channel="Email", cursor="cur")
    assert code == 200 and body == {"messages": [], "nextCursor": None}
    assert factory.call_args.kwargs["timeout"] == 10.0
    args, kwargs = client.get.call_args
    assert args[0] == "https://services.leadconnectorhq.com/conversations/messages/export"
    assert kwargs["headers"]["Version"] == "2021-04-15"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["params"] == {
        "locationId": "loc1", "contactId": "c1", "limit": 500, "sortBy": "createdAt",
        "sortOrder": "desc", "channel": "Email", "cursor": "cur"}


async def test_export_messages_status_passthrough_and_network_error(monkeypatch):
    _patch_httpx(monkeypatch, _resp(401))
    assert await gh.ghl_integration.export_messages("l", "t", "c") == (401, {})
    _patch_httpx(monkeypatch, error=httpx.ReadTimeout("slow"))
    assert await gh.ghl_integration.export_messages("l", "t", "c") == (0, {})


async def test_send_message_request_shape_and_non_json_2xx(monkeypatch):
    factory, client = _patch_httpx(monkeypatch, _resp(201, bad_json=True))
    payload = {"type": "SMS", "contactId": "c1", "message": "hi"}
    code, body = await gh.ghl_integration.send_message("tok", payload)
    assert (code, body) == (201, {})  # status kept even when the body is not JSON
    assert factory.call_args.kwargs["timeout"] == 10.0
    args, kwargs = client.post.call_args
    assert args[0] == "https://services.leadconnectorhq.com/conversations/messages"
    assert kwargs["headers"]["Version"] == "2021-04-15"
    assert kwargs["json"] == payload


async def test_send_message_network_error(monkeypatch):
    _patch_httpx(monkeypatch, error=httpx.ConnectError("down"))
    assert await gh.ghl_integration.send_message("t", {}) == (0, {})
