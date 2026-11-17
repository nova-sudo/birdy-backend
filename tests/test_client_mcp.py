"""Client context MCP tools (ai/mcp/client_mcp.py)."""

from datetime import datetime

import pytest
from fastmcp.exceptions import ToolError

from ai.mcp import client_mcp as t

ALICE, BOB = "alice@example.com", "bob@example.com"

pytestmark = pytest.mark.asyncio


async def _group(db, gid, name, user=ALICE, **kw):
    doc = {"id": gid, "user_id": user, "name": name, "ad_account_currency": "USD", "targets": {}, **kw}
    await db["client_groups"].insert_one(doc)
    return doc


@pytest.fixture
def alice(set_current_user):
    set_current_user(ALICE)


@pytest.fixture
def bob(set_current_user):
    set_current_user(BOB)


async def test_tool_tags_and_docstrings():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    for name in ("get_client", "get_client_notes"):
        assert tools[name].tags == {"read"}
    for name in ("add_client_note", "delete_client_note", "set_client_target"):
        assert tools[name].tags == {"write"}
    for name in ("get_client", "get_client_notes", "add_client_note", "delete_client_note", "set_client_target"):
        assert len(tools[name].description) > 60


async def test_ambiguous_client_returns_candidates(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental")
    await _group(mock_db, "g2", "Acme Law")
    with pytest.raises(ToolError) as e:
        await t.add_client_note.fn(client="Acme", text="hello")
    assert "Several clients match 'Acme': Acme Dental, Acme Law. Which one?" in str(e.value)
    assert await mock_db["client_notes"].count_documents({}) == 0
    with pytest.raises(ToolError, match="Several clients match"):
        await t.set_client_target.fn(client="Acme", cpl=10)
    assert (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"] == {}
    assert (await mock_db["client_groups"].find_one({"id": "g2"}))["targets"] == {}


async def test_exact_name_beats_substring_matches(mock_db, alice):
    await _group(mock_db, "g1", "Acme")
    await _group(mock_db, "g2", "Acme Law")
    out = await t.get_client.fn(client="acme")
    assert out["id"] == "g1"


async def test_resolve_by_id_and_unique_substring(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental")
    await _group(mock_db, "g2", "Birch Law")
    assert (await t.get_client.fn(client="g2"))["name"] == "Birch Law"
    assert (await t.get_client.fn(client="dental"))["id"] == "g1"
    with pytest.raises(ToolError, match="No client matches 'zzz'"):
        await t.get_client.fn(client="zzz")


async def test_resolve_client_is_importable_and_scoped(mock_db):
    await _group(mock_db, "g1", "Acme Dental")
    assert (await t.resolve_client(mock_db, ALICE, "Acme"))["id"] == "g1"
    with pytest.raises(ToolError):
        await t.resolve_client(mock_db, BOB, "Acme")


async def test_get_client_shape(mock_db, alice):
    refreshed = datetime(2026, 11, 14, 8, 0, 0)
    await _group(mock_db, "g1", "Acme Dental", ad_account_currency="GBP", targets={"cpl": 12.5},
                 meta_ad_account_id="act_1", ghl_location_id="loc1", hotprospector_group_id=None,
                 last_meta_refresh=refreshed, last_ghl_refresh=None)
    for i in range(12):
        await mock_db["client_notes"].insert_one(
            {"id": f"n{i}", "group_id": "g1", "user_id": ALICE, "author": ALICE, "body": f"note {i}",
             "created_at": datetime(2026, 11, 1, 9, i)})
    out = await t.get_client.fn(client="Acme")
    assert out["id"] == "g1" and out["name"] == "Acme Dental"
    assert out["targets"] == {"cpl": 12.5}
    assert out["ad_account_currency"] == "GBP"
    assert out["integrations"] == {"meta": True, "ghl": True, "hotprospector": False}
    assert out["last_refreshed"] == {"meta": refreshed.isoformat(), "ghl": None}
    assert len(out["notes"]) == 10
    assert out["notes"][0]["body"] == "note 11"
    assert "user_id" not in out["notes"][0]


async def test_add_then_get_note(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental")
    added = await t.add_client_note.fn(client="Acme", text="  Client paused spend for a week.  ")
    assert added["id"] and added["body"] == "Client paused spend for a week."
    stored = await mock_db["client_notes"].find_one({"id": added["id"]})
    assert stored["user_id"] == ALICE and stored["group_id"] == "g1" and stored["source"] == "mcp"
    notes = await t.get_client_notes.fn(client="Acme")
    assert [n["id"] for n in notes["notes"]] == [added["id"]]


async def test_empty_note_refused(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental")
    with pytest.raises(ToolError, match="cannot be empty"):
        await t.add_client_note.fn(client="Acme", text="   ")
    assert await mock_db["client_notes"].count_documents({}) == 0


async def test_delete_note(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental")
    added = await t.add_client_note.fn(client="Acme", text="x")
    out = await t.delete_client_note.fn(client="Acme", note_id=added["id"])
    assert out["success"] is True
    assert await mock_db["client_notes"].count_documents({}) == 0
    with pytest.raises(ToolError, match="Note not found"):
        await t.delete_client_note.fn(client="Acme", note_id=added["id"])


async def test_delete_note_of_another_client_refused(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental")
    await _group(mock_db, "g2", "Birch Law")
    other = await t.add_client_note.fn(client="Birch", text="belongs to birch")
    with pytest.raises(ToolError, match="Note not found"):
        await t.delete_client_note.fn(client="Acme", note_id=other["id"])
    assert await mock_db["client_notes"].count_documents({}) == 1


async def test_set_target(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental", targets={"cpl": 20, "monthly_wins": 4})
    out = await t.set_client_target.fn(client="Acme", cpl=15.5, monthly_revenue=9000)
    assert out["targets"] == {"cpl": 15.5, "monthly_wins": 4, "monthly_revenue": 9000}
    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["cpl"] == 15.5 and stored["monthly_revenue"] == 9000 and stored["monthly_wins"] == 4


async def test_set_non_cpl_target_only(mock_db, alice):
    await _group(mock_db, "g1", "Acme Dental", targets={"cpl": 20})
    await t.set_client_target.fn(client="Acme", monthly_revenue=12000, aov=300, monthly_wins=0)
    stored = (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]
    assert stored["cpl"] == 20 and stored["monthly_revenue"] == 12000 and stored["aov"] == 300
    assert stored["monthly_wins"] == 0


@pytest.mark.parametrize("kw", [{"cpl": -1}, {"monthly_revenue": -5}, {"aov": float("nan")},
                                {"cpa": float("inf")}, {"cpl": True}, {}])
async def test_set_target_rejects_negative(mock_db, alice, kw):
    await _group(mock_db, "g1", "Acme Dental", targets={"cpl": 20})
    with pytest.raises(ToolError):
        await t.set_client_target.fn(client="Acme", **kw)
    assert (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"] == {"cpl": 20}


# --- cross-account: bob cannot touch alice's client --------------------------

async def test_other_users_client_not_found(mock_db, bob):
    await _group(mock_db, "g1", "Acme Dental", targets={"cpl": 20})
    await mock_db["client_notes"].insert_one(
        {"id": "n1", "group_id": "g1", "user_id": ALICE, "author": ALICE, "body": "secret", "created_at": datetime.utcnow()})
    for call in (t.get_client.fn(client="Acme"), t.get_client.fn(client="g1"), t.get_client_notes.fn(client="Acme"),
                 t.add_client_note.fn(client="Acme", text="hi"), t.delete_client_note.fn(client="Acme", note_id="n1"),
                 t.set_client_target.fn(client="Acme", cpl=1)):
        with pytest.raises(ToolError, match="No client matches"):
            await call
    assert await mock_db["client_notes"].count_documents({}) == 1
    assert (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"] == {"cpl": 20}


async def test_bob_cannot_delete_alices_note_even_on_his_own_client(mock_db, bob):
    await _group(mock_db, "g1", "Acme Dental", user=ALICE)
    await _group(mock_db, "g9", "Acme Dental", user=BOB)
    await mock_db["client_notes"].insert_one(
        {"id": "n1", "group_id": "g1", "user_id": ALICE, "author": ALICE, "body": "secret", "created_at": datetime.utcnow()})
    with pytest.raises(ToolError, match="Note not found"):
        await t.delete_client_note.fn(client="Acme Dental", note_id="n1")
    assert await mock_db["client_notes"].count_documents({}) == 1


# --- scopes ---------------------------------------------------------------

async def test_read_scope_refused_for_write_tools(mcp_call, mock_db):
    await _group(mock_db, "g1", "Acme Dental")
    for tool, args in [("add_client_note", {"client": "Acme", "text": "x"}),
                       ("delete_client_note", {"client": "Acme", "note_id": "n"}),
                       ("set_client_target", {"client": "Acme", "cpl": 5})]:
        res = await mcp_call(tool, args, user=ALICE, scope="birdy.read", token_type="mcp")
        assert res.is_error and "view-only" in res.text
    assert await mock_db["client_notes"].count_documents({}) == 0
    assert (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"] == {}


async def test_read_scope_can_read_over_the_wire(mcp_call, mock_db):
    await _group(mock_db, "g1", "Acme Dental")
    res = await mcp_call("get_client", {"client": "Acme"}, user=ALICE, scope="birdy.read", token_type="mcp")
    assert not res.is_error and "Acme Dental" in res.text
