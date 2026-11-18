"""Report designer MCP tools (ai/mcp/report_designer_mcp.py)."""

from contextlib import asynccontextmanager

import pytest

from ai.mcp import report_designer_mcp as t
from ai.tools import report_designer_tools as tools
from services import report_drafts

ALICE, BOB = "alice@example.com", "bob@example.com"
TEMPLATE = {"frequency": "daily", "time": "9:00 AM",
            "blocks": [{"type": "header", "text": "Alice wrap"}, {"type": "section", "text": "Spend {{spend}}"}]}

pytestmark = pytest.mark.asyncio


@pytest.fixture
def alice(set_current_user):
    set_current_user(ALICE)


@pytest.fixture
def bob(set_current_user):
    set_current_user(BOB)


async def test_tool_tags():
    from ai.mcp import mcp
    tools_ = await mcp.get_tools()
    for n in ("list_report_clients", "list_report_metrics", "get_report_draft", "preview_report"):
        assert tools_[n].tags == {"read"}, n
    assert tools_["propose_report"].tags == {"write"}
    assert "does not change until they save it" in tools_["propose_report"].description


@pytest.mark.parametrize("tool,target,args,expected", [
    ("list_report_clients", "list_report_clients", {}, {}),
    ("list_report_metrics", "list_report_metrics", {}, {}),
    ("get_report_draft", "get_report_draft", {}, {}),
    ("propose_report", "propose_report", {"template": TEMPLATE}, {"template": TEMPLATE}),
])
async def test_tools_delegate(mock_db, alice, monkeypatch, tool, target, args, expected):
    seen = {}

    async def fake(db, user_id, **kw):
        seen.update(user_id=user_id, kw=kw)
        return {"delegated": target}

    monkeypatch.setattr(tools, target, fake)
    assert await getattr(t, tool).fn(**args) == {"delegated": target}
    assert seen == {"user_id": ALICE, "kw": expected}


async def test_list_report_clients_real(mock_db, alice):
    """Undelegated: the tool must run end to end (it once crashed on a missing re-export)."""
    await mock_db["client_groups"].insert_many([
        {"id": "g1", "user_id": ALICE, "name": "Acme", "meta_ad_account_id": "act_1",
         "ghl_location_id": "loc1", "targets": {"cpl": 20}},
        {"id": "g2", "user_id": ALICE, "name": "Gone", "client_status": "Inactive", "meta_ad_account_id": "act_2"},
        {"id": "g3", "user_id": BOB, "name": "Bobs", "meta_ad_account_id": "act_3"},
    ])
    out = await t.list_report_clients.fn()
    assert out["total"] == 1
    row = out["clients"][0]
    assert (row["id"], row["name"], row["has_targets"]) == ("g1", "Acme", True)
    assert "Meta Ads" in row["connected"] and "CRM pipeline" in row["connected"]


async def test_preview_delegates(mock_db, alice, monkeypatch):
    seen = {}

    async def fake(db, user_id, source):
        seen.update(user_id=user_id, source=source)
        return {"delegated": True}

    monkeypatch.setattr(report_drafts, "preview_stored", fake)
    assert await t.preview_report.fn() == {"delegated": True}
    assert seen == {"user_id": ALICE, "source": "draft"}
    await t.preview_report.fn(source="saved")
    assert seen["source"] == "saved"


async def test_draft_visible_in_router_state(mock_db, mock_mongo_client, alice, monkeypatch):
    import routers.report_designer as router

    @asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(router, "get_mongo_client", fake_client)
    out = await t.propose_report.fn(template=TEMPLATE)
    assert out["ok"] is True
    state = await router.state(current_user=ALICE)
    assert state["draft"] is not None
    assert state["draft"]["blocks"][0]["text"]["text"] == "Alice wrap"
    assert state["draft"] == await router.load_draft(mock_db, ALICE)
    assert (await t.get_report_draft.fn())["draft"] == state["draft"]


async def test_users_never_see_each_others_drafts(mock_db, set_current_user):
    set_current_user(ALICE)
    assert (await t.propose_report.fn(template=TEMPLATE))["ok"] is True
    set_current_user(BOB)
    assert (await t.get_report_draft.fn())["draft"] is None
    bobs = {**TEMPLATE, "blocks": [{"type": "header", "text": "Bob wrap"}]}
    assert (await t.propose_report.fn(template=bobs))["ok"] is True
    assert (await t.get_report_draft.fn())["draft"]["blocks"][0]["text"]["text"] == "Bob wrap"
    set_current_user(ALICE)
    assert (await t.get_report_draft.fn())["draft"]["blocks"][0]["text"]["text"] == "Alice wrap"
    assert await mock_db["report_drafts"].count_documents({}) == 2


async def test_invalid_template_returns_errors_and_saves_nothing(mock_db, alice):
    out = await t.propose_report.fn(template={**TEMPLATE, "blocks": [{"type": "section", "text": "{{nope}}"}]})
    assert out["ok"] is False and out["errors"]
    assert await mock_db["report_drafts"].count_documents({}) == 0
    assert (await t.get_report_draft.fn())["draft"] is None


async def test_preview_saved_without_a_brief_is_empty(mock_db, alice):
    assert await t.preview_report.fn(source="saved") == {"blocks": [], "text": "", "sample": False, "template": None}


async def test_preview_draft_renders_the_draft(mock_db, alice):
    await t.propose_report.fn(template=TEMPLATE)
    out = await t.preview_report.fn()
    assert out["blocks"] and "Alice wrap" in out["text"]
