"""Birdy's flavour in AI apps (ai/mcp/voice.py): titles, icon, sign-off line, voiced refusals, first hello, prompts."""

import pytest
from fastapi.testclient import TestClient

import credits
from ai.mcp import mcp, voice
from ai.mcp.activity_labels import LABELS
from core.config import FRONTEND_URL

USER = "alice@example.com"
RW = "birdy.read birdy.write"


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    credits._settings_cache["data"] = None
    yield
    credits._settings_cache["data"] = None


async def _give(db, topup):
    c, _ = credits._effective_credits({})
    c.update(topup_balance=topup)
    await db["users"].update_one({"user_id": USER}, {"$set": {"credits": c}}, upsert=True)


def test_tool_title_is_the_label_in_the_present():
    assert voice.tool_title("Looked at campaign results") == "Looking at campaign results"
    assert voice.tool_title("Undid a change") == "Undoing a change"
    assert voice.tool_title("Read client notes") == "Reading client notes"


def test_every_label_verb_has_a_present_form():
    missing = {lbl["label"].split()[0] for lbl in LABELS.values()} - set(voice._ING)
    assert not missing, f"add these to voice._ING: {sorted(missing)}"


@pytest.mark.asyncio
async def test_every_tool_has_a_plain_english_title():
    tools = await mcp.get_tools()
    untitled = [n for n, t in tools.items() if not t.title]
    assert not untitled, untitled
    assert tools["get_alerts"].title == "Checking alerts"


def test_icon_is_served_as_png(mock_mongo_client):
    from main import app
    res = TestClient(app).get("/birdy-mcp-icon.png")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png"
    assert res.content[:4] == b"\x89PNG"


def test_signoff_line():
    assert voice.signoff("/alerts", 0.24, 412.13) == (
        f"Birdy: see it in Birdy {FRONTEND_URL}/alerts · this request cost 0.24 credits (412.1 left)")
    assert voice.signoff(None, 0.2, None) == "Birdy: this request cost 0.2 credits"
    assert voice.signoff(None, None, None) is None


@pytest.mark.asyncio
async def test_external_result_ends_with_signoff(mcp_call, mock_db):
    await _give(mock_db, 50)
    res = await mcp_call("get_alerts", {}, user=USER, scope=RW, token_type="mcp")
    assert not res.is_error
    assert res.signoff.startswith(f"Birdy: see it in Birdy {FRONTEND_URL}/alerts · this request cost ")
    assert "left)" in res.signoff and "Birdy:" not in res.text
    assert voice.STYLE_REMINDER in res.signoff  # a look-up carries the answer-style reminder


@pytest.mark.asyncio
async def test_internal_and_refused_calls_have_no_signoff(mcp_call, mock_db):
    assert (await mcp_call("get_alerts", {}, user=USER)).signoff is None  # internal token
    refused = await mcp_call("create_alert", {"name": "x", "metric": "spend", "operator": "gt", "value": 1},
                             user=USER, scope="birdy.read", token_type="mcp")
    assert refused.is_error and refused.signoff is None
    assert "Birdy can only look right now" in refused.text and "No credits were used." in refused.text


@pytest.mark.asyncio
async def test_signoff_failure_never_loses_the_result(mcp_call, mock_db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad")
    monkeypatch.setattr(voice, "closing_block", boom)
    res = await mcp_call("get_alerts", {}, user=USER, scope=RW, token_type="mcp")
    assert not res.is_error and res.signoff is None


@pytest.mark.asyncio
async def test_get_started_lists_active_clients_and_access(mcp_call, mock_db):
    import json
    await mock_db["client_groups"].insert_many([
        {"id": "g1", "user_id": USER, "name": "Smile Studio"},
        {"id": "g2", "user_id": USER, "name": "Acme Dental"},
        {"id": "g3", "user_id": USER, "name": "Old Client", "client_status": "Inactive"},
        {"id": "g4", "user_id": "bob@example.com", "name": "Not Mine"},
    ])
    view = json.loads((await mcp_call("get_started", {}, user=USER, scope="birdy.read", token_type="mcp")).text)
    assert view["clients"] == 2 and view["client_names"] == ["Acme Dental", "Smile Studio"]
    assert view["access"] == "view only" and any("view-only" in c for c in view["can_do"])
    rw = json.loads((await mcp_call("get_started", {}, user=USER, scope=RW, token_type="mcp")).text)
    assert rw["access"] == "view and make changes" and rw["try_asking"]


@pytest.mark.asyncio
async def test_prompt_menu_titles():
    prompts = await mcp.get_prompts()
    assert {p.title for p in prompts.values()} == {
        "Monday review", "What changed?", "Call prep", "Agent recap", "Daily check-in", "Fix my worst client"}


def test_closing_block_reminds_style_on_lookups_only():
    assert voice.closing_block("/alerts", 0.2, None, lookup=True).endswith(voice.STYLE_REMINDER)
    assert voice.STYLE_REMINDER not in voice.closing_block("/alerts", 0.2, None, lookup=False)
    assert voice.closing_block(None, None, None, lookup=True) is None
