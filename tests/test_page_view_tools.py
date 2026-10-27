"""
Birdy's saved-view tools: list_view_columns / create_saved_view, and run_chat
handing the new view back to the page as page_view_ops.
"""

import json

import pytest

from ai.orchestrator import _PAGE_TOOLS, run_chat
from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall
from ai.tools.page_view_tools import (
    PAGE_VIEW_TOOL_NAMES,
    create_saved_view,
    list_view_columns,
    register_page_view_tools,
)
from ai.tools.registry import ToolRegistry
from core.models import ChatRequest, ChatResponse
from services.page_views_service import get_page_bucket

USER = "alice@example.com"

CATALOGUES = {
    "contacts": {
        "label": "Leads Hub",
        "path": "/contacts",
        "columns": [
            {"id": "name", "label": "Name", "source": "meta"},
            {"id": "phone", "label": "Phone", "source": "meta"},
            {"id": "ghl_stage", "label": "Pipeline stage", "source": "ghl"},
            {"id": "tag_hot", "label": "hot", "source": "tags"},
        ],
    },
    "mktg_campaigns": {"label": "Marketing Hub · Campaigns", "path": "/campaigns",
                       "columns": [{"id": "spend", "label": "Spend"}]},
}


async def test_list_pages_then_columns_with_search(mock_db):
    pages = await list_view_columns(mock_db, USER, view_catalogues=CATALOGUES)
    assert pages["pages"] == [
        {"page": "contacts", "label": "Leads Hub", "column_count": 4},
        {"page": "mktg_campaigns", "label": "Marketing Hub · Campaigns", "column_count": 1},
    ]
    found = await list_view_columns(mock_db, USER, view_catalogues=CATALOGUES, page="contacts", search="STAGE")
    assert [c["id"] for c in found["columns"]] == ["ghl_stage"]


async def test_unknown_page_names_the_known_ones(mock_db):
    out = await create_saved_view(mock_db, USER, view_catalogues=CATALOGUES, page="cc_leads",
                                  name="x", column_ids=["name"])
    assert out["ok"] is False
    assert "Leads Hub" in out["error"] and "open (or reload) that hub" in out["error"]
    out = await list_view_columns(mock_db, USER, view_catalogues=None, page="contacts")
    assert out["ok"] is False and "none yet" in out["error"]


async def test_create_saves_ordered_columns_and_emits_op(mock_db):
    sink = []
    out = await create_saved_view(
        mock_db, USER, view_catalogues=CATALOGUES, page_view_ops_sink=sink,
        page="contacts", name="Hot leads", column_ids=["tag_hot", "name", "tag_hot", "phone"],
    )
    assert out["ok"] is True
    assert out["columns"] == ["hot", "Name", "Phone"]
    bucket = await get_page_bucket(mock_db, USER, "contacts")
    view = bucket["views"][0]
    assert view["name"] == "Hot leads"
    assert view["state"] == {"visibleColumns": ["tag_hot", "name", "phone"], "source": "all"}
    # first view on a page becomes the default, as from the Views menu
    assert bucket["default_view_id"] == view["id"]
    assert sink == [{"type": "view_created", "page": "contacts", "page_label": "Leads Hub",
                     "path": "/contacts", "view": view, "made_default": False}]


async def test_create_rejects_bad_columns_and_duplicates(mock_db):
    sink = []
    kw = dict(view_catalogues=CATALOGUES, page_view_ops_sink=sink, page="contacts")
    out = await create_saved_view(mock_db, USER, name="A", column_ids=["name", "nope"], **kw)
    assert out["ok"] is False and "nope" in out["error"]
    assert (await create_saved_view(mock_db, USER, name="A", column_ids=[], **kw))["ok"] is False
    assert (await create_saved_view(mock_db, USER, name="A", column_ids=["name"], **kw))["ok"] is True
    dup = await create_saved_view(mock_db, USER, name="a", column_ids=["phone"], **kw)
    assert dup == {"ok": False, "error": 'A view named "a" already exists'}
    blank = await create_saved_view(mock_db, USER, name="  ", column_ids=["phone"], **kw)
    assert blank == {"ok": False, "error": "View name is required"}
    assert len(sink) == 1


async def test_make_default_moves_the_default(mock_db):
    kw = dict(view_catalogues=CATALOGUES, page="contacts")
    first = await create_saved_view(mock_db, USER, name="One", column_ids=["name"], **kw)
    second = await create_saved_view(mock_db, USER, name="Two", column_ids=["phone"], make_default=True, **kw)
    bucket = await get_page_bucket(mock_db, USER, "contacts")
    assert bucket["default_view_id"] == second["view_id"] != first["view_id"]


# ── run_chat ────────────────────────────────────────────────────────────────


class ScriptedProvider(BaseLLMProvider):
    def __init__(self, responses):
        self._responses = list(responses)
        self.model = "stub-model"
        self.calls = []

    async def chat_completion(self, messages, tools=None, tool_choice="auto",
                              temperature=0.3, max_tokens=2048):
        self.calls.append({"messages": list(messages), "tools": tools})
        return self._responses.pop(0)


def _call(name, arguments, call_id):
    return ProviderResponse(content=None, finish_reason="tool_calls",
                            tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))])


@pytest.fixture
def view_registry(monkeypatch):
    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    reg = ToolRegistry()
    register_page_view_tools(reg)
    return reg


async def test_run_chat_injects_catalogues_and_returns_ops(mock_db, view_registry):
    provider = ScriptedProvider([
        # the model cannot pass someone else's catalogue or a sink of its own
        _call("create_saved_view", {"page": "contacts", "name": "Hot", "column_ids": ["name"],
                                    "view_catalogues": {}, "page_view_ops_sink": None}, "tc-1"),
        ProviderResponse(content="Saved it."),
    ])
    result = await run_chat(provider=provider, tool_registry=view_registry, db=mock_db, user_id=USER,
                            message="make a view", page="leads", view_catalogues=CATALOGUES)
    assert [op["view"]["name"] for op in result["page_view_ops"]] == ["Hot"]
    assert PAGE_VIEW_TOOL_NAMES <= {t["function"]["name"] for t in provider.calls[0]["tools"]}


def test_hub_pages_offer_the_tools_and_models_carry_the_fields():
    for page in ("dashboard", "campaigns", "leads", "opportunities", "call_center", "client_detail"):
        assert PAGE_VIEW_TOOL_NAMES <= set(_PAGE_TOOLS[page])
    assert not PAGE_VIEW_TOOL_NAMES & set(_PAGE_TOOLS["dashboards"])
    assert ChatRequest(message="hi", view_catalogues=CATALOGUES).view_catalogues == CATALOGUES
    assert ChatResponse(reply="ok").model_dump()["page_view_ops"] == []


# ── column lists saved to the account ───────────────────────────────────────


async def test_saved_catalogues_work_without_the_browser_sending_any(mock_db):
    from services.page_views_service import save_view_catalogue
    await save_view_catalogue(mock_db, USER, "clients", {
        "label": "Clients Hub", "path": "/clients",
        "columns": [{"id": "spend", "label": "Spend", "source": "meta"}, {"id": "cpl", "label": "CPL"}],
    })
    pages = await list_view_columns(mock_db, USER, view_catalogues=None)
    assert pages["pages"] == [{"page": "clients", "label": "Clients Hub", "column_count": 2}]
    out = await create_saved_view(mock_db, USER, page="clients", name="Media buyer", column_ids=["spend", "cpl"])
    assert out["ok"] is True and out["columns"] == ["Spend", "CPL"]


async def test_sent_catalogues_win_over_saved_ones(mock_db):
    from services.page_views_service import save_view_catalogue
    await save_view_catalogue(mock_db, USER, "contacts", {"label": "Old", "columns": [{"id": "old"}]})
    out = await list_view_columns(mock_db, USER, view_catalogues=CATALOGUES, page="contacts")
    assert out["label"] == "Leads Hub" and len(out["columns"]) == 4


async def test_pages_resolve_by_hub_name(mock_db):
    cats = {**CATALOGUES, "mktg_ads": {"label": "Marketing Hub · Ads", "columns": [{"id": "spend"}]}}
    out = await list_view_columns(mock_db, USER, view_catalogues=cats, page="Leads hub")
    assert out["page"] == "contacts"
    out = await list_view_columns(mock_db, USER, view_catalogues=cats, page="marketing hub")
    assert out["ok"] is False and "mktg_campaigns" in out["error"] and "mktg_ads" in out["error"]
    out = await create_saved_view(mock_db, USER, view_catalogues=cats, page="Marketing Hub · Campaigns",
                                  name="MB", column_ids=["spend"])
    assert out["ok"] is True and out["page_label"] == "Marketing Hub · Campaigns"


async def test_catalogue_validation(mock_db):
    from fastapi import HTTPException
    from services.page_views_service import clean_view_catalogue
    for page, cols in (("bad.page", [{"id": "a"}]), ("clients", []), ("clients", [{"label": "x"}])):
        with pytest.raises(HTTPException):
            clean_view_catalogue(page, "L", None, cols)
    many = clean_view_catalogue("clients", None, None, [{"id": f"c{i}"} for i in range(500)])
    assert len(many["columns"]) == 400 and many["label"] == "clients"
