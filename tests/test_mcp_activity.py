"""The MCP activity log: one row per external tool call, charged calls linked to their credits."""

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

import credits
from ai.mcp import mcp
from ai.mcp.access import LIVE, READ, _app_name
from ai.mcp.activity_labels import LABELS
from ai.mcp.confirm import two_step
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import mcp_activity
from services.mcp_activity import app_kind_for, link_for, redact, summarize

USER = "alice@example.com"
RW = "birdy.read birdy.write"


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    credits._settings_cache["data"] = None
    yield
    credits._settings_cache["data"] = None


async def _rows(db, **query):
    return await db["mcp_activity"].find(query).sort("created_at", 1).to_list(None)


@pytest.fixture
def temp_tools():
    state = {"v": 1}

    @mcp.tool(name="zz_live_two_step", **LIVE)
    async def zz_live_two_step(confirm_code: str | None = None) -> dict:
        """temp"""
        user_id = get_access_token().claims["sub"]
        db = get_db(get_shared_mongo_client())

        async def preview():
            return {"action": "go", "phone": "+1 555 123 4821"}

        async def execute():
            return {"ok": True, "done": True}

        return await two_step(db, user_id, "zz_live_two_step", {}, confirm_code, preview, execute, state=dict(state))

    @mcp.tool(name="zz_error_result", **READ)
    async def zz_error_result() -> dict:
        """temp"""
        return {"error": "nope"}

    @mcp.tool(name="zz_raises", **READ)
    async def zz_raises() -> dict:
        """temp"""
        raise ToolError("Client not found.")

    yield state
    for name in ("zz_live_two_step", "zz_error_result", "zz_raises"):
        mcp.remove_tool(name)


@pytest.mark.asyncio
async def test_every_registered_tool_has_a_label():
    tools = await mcp.get_tools()
    missing = [n for n in tools if not (LABELS.get(n) or {}).get("label")]
    assert not missing, missing
    assert not set(LABELS) - set(tools), "labels for tools that no longer exist"


def test_link_templates_fill_from_args_or_give_none():
    assert link_for("post_creative_test", {"test_id": "t1"}) == "/campaigns/creatives/t1/edit"
    assert link_for("post_creative_test", {}) is None
    assert link_for("get_client", {"client": "x"}) is None
    assert link_for("get_alerts", {}) == "/alerts"


def test_app_kind_matches_the_frontend_logo_mapping():
    assert app_kind_for("Claude") == "claude"
    assert app_kind_for("Claude Code") == "claude-code"
    assert app_kind_for("ChatGPT") == "chatgpt"
    assert app_kind_for("Cursor") == "cursor"
    assert app_kind_for("My script") == "other"


@pytest.mark.asyncio
async def test_lookup_logged_and_charged(mcp_call, mock_db):
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert not res.is_error
    [row] = await _rows(mock_db)
    ledger = await mock_db["ai_usage"].find_one({"source": "mcp"})
    assert row["kind"] == "lookup" and row["tool"] == "get_alerts" and row["user_id"] == USER
    assert row["result_status"] == "ok" and row["refusal_reason"] is None
    assert row["summary"] == "Checked alerts" and row["link"] == "/alerts"
    assert row["credits"]["total"] == ledger["credits"] > 0
    assert row["grant_id"] == ledger["grant_id"] and row["response_chars"] > 0
    assert row["access"] == "write"  # no scope claim on a pre-scope manual token = full access
    assert row["id"] and row["confirmed_after"] is None
    assert isinstance(row["created_at"], datetime)


@pytest.mark.asyncio
async def test_app_name_from_claim_else_token_record(mock_db):
    assert await _app_name(mock_db, {"client_name": "Claude"}) == "Claude"
    await mock_db["mcp_tokens"].insert_one({"jti": "j1", "name": "My laptop", "user_id": USER})
    assert await _app_name(mock_db, {"jti": "j1"}) == "My laptop"
    assert await _app_name(mock_db, {"jti": "gone"}) == "Unknown app"


@pytest.mark.asyncio
async def test_refused_view_only_logged_free(mcp_call, mock_db):
    res = await mcp_call("create_alert", {"name": "x", "metric": "spend", "operator": "gt", "value": 1},
                         user=USER, scope="birdy.read", token_type="mcp")
    assert res.is_error
    [row] = await _rows(mock_db)
    assert row["kind"] == "refused" and row["refusal_reason"] == "view_only"
    assert row["credits"]["total"] == 0 and row["access"] == "read"
    assert await mock_db["ai_usage"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_no_subscription_refusal_logged(mcp_call, mock_db, no_subscription):
    await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    [row] = await _rows(mock_db)
    assert row["kind"] == "refused" and row["refusal_reason"] == "no_subscription"


@pytest.mark.asyncio
async def test_bad_code_logged_free_then_valid_execute_links_preview(mcp_call, mock_db, temp_tools):
    pre = await mcp_call("zz_live_two_step", {}, user=USER, scope=RW, token_type="mcp")
    code = json.loads(pre.text)["confirm_code"]
    bad = await mcp_call("zz_live_two_step", {"confirm_code": "wrong"}, user=USER, scope=RW, token_type="mcp")
    assert "error" in bad.text
    done = await mcp_call("zz_live_two_step", {"confirm_code": code}, user=USER, scope=RW, token_type="mcp")
    assert not done.is_error
    preview, refused, live = await _rows(mock_db)
    assert preview["kind"] == "preview" and refused["kind"] == "refused" and live["kind"] == "live"
    assert refused["refusal_reason"] == "bad_code" and refused["credits"]["total"] == 0
    assert live["confirmed_after"] == preview["id"]
    assert live["credits"]["live"] > 0 and preview["credits"]["live"] == 0


@pytest.mark.asyncio
async def test_state_changed_logged_free(mcp_call, mock_db, temp_tools):
    pre = await mcp_call("zz_live_two_step", {}, user=USER, scope=RW, token_type="mcp")
    code = json.loads(pre.text)["confirm_code"]
    temp_tools["v"] = 2
    await mcp_call("zz_live_two_step", {"confirm_code": code}, user=USER, scope=RW, token_type="mcp")
    _, row = await _rows(mock_db)
    assert row["kind"] == "refused" and row["refusal_reason"] == "state_changed" and row["credits"]["total"] == 0


def test_redaction_masks_extensions_and_bare_international_and_spares_datetimes_and_decimals():
    out = redact({"body": "ring +1-555-123-4821x22 or UK 447911123456 today",
                  "when": "2026-11-15T10:42:00Z", "when2": "2026-11-15 12:00:30+00:00",
                  "n": "1234.56", "ctr": "0.0123456789", "spend": "spend 12345.678901", "ad": "120210000000000"})
    assert "555" not in out["body"] and "447911" not in out["body"]
    assert "•••• 4821" in out["body"] and "•••• 3456" in out["body"]
    assert out["when"] == "2026-11-15T10:42:00Z" and out["when2"] == "2026-11-15 12:00:30+00:00"
    assert out["n"] == "1234.56" and out["ctr"] == "0.0123456789" and out["spend"] == "spend 12345.678901"
    assert out["ad"] == "120210000000000"


def test_redaction_masks_phone_email_and_confirm_code():
    out = redact({
        "confirm_code": "abc", "body": "Hi, call me on +1 (555) 123-4821 or j.doe@acme.com",
        "phone": "555-123-4821", "email": "jane@acme.com", "ad_id": "120210000000000123",
        "nested": [{"confirm_code": "x", "contact": {"phone_number": 5551234821, "note": "ok 2026-11-15"}}],
    })
    blob = json.dumps(out)
    assert "confirm_code" not in blob and "abc" not in blob
    assert "4821" in blob and "555" not in blob and "j.doe" not in blob and "jane" not in blob
    assert "•••• 4821" in out["body"] and "j•••@acme.com" in out["body"] and out["body"].startswith("Hi, call me on")
    assert out["ad_id"] == "120210000000000123" and "2026-11-15" in blob


@pytest.mark.asyncio
async def test_preview_result_excerpt_is_redacted(mcp_call, mock_db, temp_tools):
    pre = await mcp_call("zz_live_two_step", {}, user=USER, scope=RW, token_type="mcp")
    code = json.loads(pre.text)["confirm_code"]
    [row] = await _rows(mock_db)
    assert code not in row["result_excerpt"] and '"confirm_code"' not in row["result_excerpt"]
    assert "555" not in row["result_excerpt"] and "•••• 4821" in row["result_excerpt"]
    assert len(row["result_excerpt"]) <= 2000


@pytest.mark.asyncio
async def test_args_redacted_in_row(mcp_call, mock_db):
    await mcp_call("add_client_note", {"client": "Nobody", "text": "ring 555 123 4821"}, user=USER, scope=RW, token_type="mcp")
    [row] = await _rows(mock_db)
    assert "555" not in json.dumps(row["args"]) and "•••• 4821" in row["args"]["text"]


@pytest.mark.asyncio
async def test_rate_limited_collapses_to_one_row_per_minute(mcp_call, mock_db, monkeypatch):
    monkeypatch.setattr("ai.mcp.access.MCP_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr("ai.mcp.access._minute", lambda: 28_333_333)
    monkeypatch.setattr("services.mcp_activity._now", lambda: datetime(2026, 11, 15, 12, 0, 30, tzinfo=timezone.utc))
    for _ in range(5):
        await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    limited = await _rows(mock_db, refusal_reason="rate_limit")
    assert len(limited) == 1 and limited[0]["count"] == 3 and limited[0]["kind"] == "refused"
    assert len(await _rows(mock_db, kind="lookup")) == 2
    assert await mock_db["ai_usage"].count_documents({"source": "mcp"}) == 2


@pytest.mark.asyncio
async def test_slow_db_does_not_block_result(mcp_call, mock_db, monkeypatch):
    async def slow(*a, **k):
        await asyncio.sleep(2)
    monkeypatch.setattr(mcp_activity, "record_call", slow)
    started = time.monotonic()
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert not res.is_error and time.monotonic() - started < 1.0
    for task in list(__import__("ai.mcp.access", fromlist=["_background"])._background):
        task.cancel()


@pytest.mark.asyncio
async def test_timeout_does_not_split_deduction_from_ledger_row(mcp_call, mock_db, monkeypatch):
    """A cancel landing between the balance write and its ledger row would lose the row: the work is shielded."""
    coll = mock_db["ai_usage"]
    real_insert = type(coll).insert_one

    async def slow_insert(self, *a, **k):
        await asyncio.sleep(0.2)
        return await real_insert(self, *a, **k)

    monkeypatch.setattr("ai.mcp.access._CHARGE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(type(coll), "insert_one", slow_insert)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert not res.is_error
    await asyncio.sleep(0.6)
    monkeypatch.undo()
    assert await mock_db["ai_usage"].count_documents({"source": "mcp"}) == 1


@pytest.mark.asyncio
async def test_error_result_free_and_status_error(mcp_call, mock_db, temp_tools):
    await mcp_call("zz_error_result", {}, user=USER, token_type="mcp")
    [row] = await _rows(mock_db)
    assert row["result_status"] == "error" and row["kind"] == "lookup" and row["credits"]["total"] == 0
    assert await mock_db["ai_usage"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_tool_that_raises_is_logged_free_as_error_and_reraised(mcp_call, mock_db, temp_tools):
    res = await mcp_call("zz_raises", {}, user=USER, token_type="mcp")
    assert res.is_error and "Client not found" in res.text
    [row] = await _rows(mock_db)
    assert row["result_status"] == "error" and row["kind"] == "lookup" and row["credits"]["total"] == 0
    assert "Client not found" in row["result_excerpt"]
    assert await mock_db["ai_usage"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_internal_call_not_logged(mcp_call, mock_db):
    res = await mcp_call("get_alerts", {}, user=USER, token_type="access")
    assert not res.is_error
    assert await mock_db["mcp_activity"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_clients_resolved_from_args_and_named_in_summary(mcp_call, mock_db):
    await mock_db["client_groups"].insert_one({"id": "g1", "user_id": USER, "name": "Acme Dental"})
    await mock_db["client_groups"].insert_one({"id": "g2", "user_id": "bob@example.com", "name": "Not Mine"})
    await mcp_call("get_client", {"client": "g1"}, user=USER, token_type="mcp")
    [row] = await _rows(mock_db)
    assert row["client_group_ids"] == ["g1"] and row["client_names"] == ["Acme Dental"]
    assert row["summary"] == "Looked at a client for Acme Dental"
    clients = await mcp_activity.resolve_clients(mock_db, USER, {"group_id": "g2"})
    assert clients == []  # never someone else's client


def test_summaries_for_previews_and_refusals():
    assert summarize("apply_card", {}, [{"id": "g", "name": "Acme"}], {"kind": "preview"}).startswith("Previewed: applied an agent card for Acme")
    assert summarize("apply_card", {}, [], {"kind": "refused"}).startswith("Could not do this")
