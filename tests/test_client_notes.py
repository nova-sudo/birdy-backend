"""
tests/test_client_notes.py
--------------------------
Hand-written notes on a client — the history book composer.

The property that matters most is scoping: a note is attached to a client group
and readable only by the account that wrote it. Agencies share a database, so a
leak here shows one agency another's private commentary about a client.
"""

import contextlib

import pytest
from fastapi import HTTPException

import routers.client_notes as notes
from routers.client_notes import CreateNoteRequest

USER = "user@example.com"
OTHER = "someone@else.com"


@pytest.fixture
def notes_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(notes, "get_mongo_client", fake_client)
    return notes


async def a_group(db, group_id="g1", user_id=USER):
    await db["client_groups"].insert_one(
        {"id": group_id, "user_id": user_id, "name": "Aura"}
    )


async def add(body, group_id="g1", user=USER, author=None):
    return await notes.create_note(
        group_id, CreateNoteRequest(body=body, author=author), current_user=user
    )


# ── writing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_note_is_stored_and_returned(notes_api, mock_db):
    await a_group(mock_db)

    note = await add("Client moved their budget to Q4.")

    assert note["body"] == "Client moved their budget to Q4."
    assert note["id"]
    stored = await mock_db["client_notes"].find_one({"id": note["id"]})
    assert stored["group_id"] == "g1"
    assert stored["user_id"] == USER


@pytest.mark.asyncio
async def test_the_author_defaults_to_the_signed_in_user(notes_api, mock_db):
    await a_group(mock_db)
    note = await add("A note")
    assert note["author"] == USER


@pytest.mark.asyncio
async def test_an_explicit_author_is_kept(notes_api, mock_db):
    await a_group(mock_db)
    note = await add("A note", author="Emma T.")
    assert note["author"] == "Emma T."


@pytest.mark.asyncio
async def test_a_blank_author_falls_back_rather_than_storing_empty(notes_api, mock_db):
    await a_group(mock_db)
    note = await add("A note", author="   ")
    assert note["author"] == USER


@pytest.mark.asyncio
async def test_the_body_is_trimmed(notes_api, mock_db):
    await a_group(mock_db)
    note = await add("   padded   ")
    assert note["body"] == "padded"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
async def test_an_empty_note_is_rejected(notes_api, mock_db, body):
    await a_group(mock_db)

    with pytest.raises(HTTPException) as exc:
        await add(body)
    assert exc.value.status_code == 400
    assert await mock_db["client_notes"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_an_overlong_note_is_rejected(notes_api, mock_db):
    await a_group(mock_db)

    with pytest.raises(HTTPException) as exc:
        await add("x" * (notes.MAX_NOTE_LENGTH + 1))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_a_note_at_the_limit_is_accepted(notes_api, mock_db):
    await a_group(mock_db)
    note = await add("x" * notes.MAX_NOTE_LENGTH)
    assert len(note["body"]) == notes.MAX_NOTE_LENGTH


# ── reading ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notes_come_back_newest_first(notes_api, mock_db):
    from datetime import datetime

    await a_group(mock_db)
    first = await add("First")
    second = await add("Second")

    # Stamped apart deliberately: two notes written in the same microsecond
    # have no defined order, and this is testing the sort, not the clock.
    await mock_db["client_notes"].update_one(
        {"id": first["id"]}, {"$set": {"created_at": datetime(2026, 8, 1, 9, 0)}}
    )
    await mock_db["client_notes"].update_one(
        {"id": second["id"]}, {"$set": {"created_at": datetime(2026, 8, 2, 9, 0)}}
    )

    out = await notes.list_notes("g1", current_user=USER)

    assert [n["id"] for n in out["notes"]] == [second["id"], first["id"]]


@pytest.mark.asyncio
async def test_created_at_is_serialisable(notes_api, mock_db):
    await a_group(mock_db)
    await add("A note")

    out = await notes.list_notes("g1", current_user=USER)
    assert isinstance(out["notes"][0]["created_at"], str)


@pytest.mark.asyncio
async def test_a_client_with_no_notes_returns_an_empty_list(notes_api, mock_db):
    await a_group(mock_db)
    out = await notes.list_notes("g1", current_user=USER)
    assert out["notes"] == []


@pytest.mark.asyncio
async def test_notes_do_not_leak_between_clients(notes_api, mock_db):
    await a_group(mock_db, "g1")
    await a_group(mock_db, "g2")
    await add("About client one", group_id="g1")
    await add("About client two", group_id="g2")

    out = await notes.list_notes("g1", current_user=USER)
    assert [n["body"] for n in out["notes"]] == ["About client one"]


# ── scoping ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notes_cannot_be_read_by_another_account(notes_api, mock_db):
    """Agencies share a database — a leak here shows one agency another's
    private commentary about a client."""
    await a_group(mock_db, user_id=OTHER)
    await add("Their private note", user=OTHER)

    with pytest.raises(HTTPException) as exc:
        await notes.list_notes("g1", current_user=USER)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_note_cannot_be_attached_to_someone_elses_client(notes_api, mock_db):
    await a_group(mock_db, user_id=OTHER)

    with pytest.raises(HTTPException) as exc:
        await add("Sneaking in")
    assert exc.value.status_code == 404
    assert await mock_db["client_notes"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_a_note_cannot_be_attached_to_a_client_that_does_not_exist(notes_api, mock_db):
    with pytest.raises(HTTPException) as exc:
        await add("Nowhere")
    assert exc.value.status_code == 404


# ── deleting ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_note_can_be_deleted(notes_api, mock_db):
    await a_group(mock_db)
    note = await add("Delete me")

    out = await notes.delete_note("g1", note["id"], current_user=USER)

    assert out["success"] is True
    assert await mock_db["client_notes"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_deleting_leaves_the_other_notes(notes_api, mock_db):
    await a_group(mock_db)
    keep = await add("Keep")
    drop = await add("Drop")

    await notes.delete_note("g1", drop["id"], current_user=USER)

    out = await notes.list_notes("g1", current_user=USER)
    assert [n["id"] for n in out["notes"]] == [keep["id"]]


@pytest.mark.asyncio
async def test_another_account_cannot_delete_a_note(notes_api, mock_db):
    await a_group(mock_db)
    note = await add("Mine")

    with pytest.raises(HTTPException) as exc:
        await notes.delete_note("g1", note["id"], current_user=OTHER)
    assert exc.value.status_code == 404
    assert await mock_db["client_notes"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_deleting_a_missing_note_is_404(notes_api, mock_db):
    await a_group(mock_db)

    with pytest.raises(HTTPException) as exc:
        await notes.delete_note("g1", "nope", current_user=USER)
    assert exc.value.status_code == 404
