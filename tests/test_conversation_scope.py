"""
tests/test_conversation_scope.py
--------------------------------
Auto-detecting which client a conversation is about.

The Ask Birdy hub badges every thread global or client-scoped. Nothing asks
the user to pick: the model already resolves "Aura" to a group id via
get_client_groups before it can query anything, so the ids it passes to its
tools ARE the answer to "which client is this turn about". This covers the
two halves of that — reading the ids back out of tool arguments, and turning a
thread's tagged turns into one scope.
"""

from datetime import datetime

import pytest

from ai.conversation_log import log_message, tag_latest_user_turn
from ai.orchestrator import _conversation_scope, _group_ids_in_args

USER = "user@example.com"
OTHER = "someone@else.com"


# ── reading group ids out of tool arguments ───────────────────────────────
#
# Four spellings are in use across the tool schemas (group_id, group_ids,
# groups, client_group_id) and a thread is only client-scoped when exactly one
# id shows up, so mis-parsing any of them silently mislabels the thread.


def test_reads_a_single_group_id_string():
    assert _group_ids_in_args({"group_id": "g1"}) == {"g1"}


def test_reads_a_group_ids_array():
    assert _group_ids_in_args({"group_ids": ["g1", "g2"]}) == {"g1", "g2"}


def test_reads_a_comma_separated_groups_string():
    assert _group_ids_in_args({"groups": "g1,g2"}) == {"g1", "g2"}
    assert _group_ids_in_args({"groups": "g1, g2 "}) == {"g1", "g2"}


def test_reads_the_client_group_id_spelling():
    assert _group_ids_in_args({"client_group_id": "g1"}) == {"g1"}


def test_ignores_arguments_that_name_no_group():
    assert _group_ids_in_args({"start_date": "2026-08-01", "limit": 50}) == set()
    assert _group_ids_in_args({}) == set()


def test_ignores_empty_and_null_entries():
    assert _group_ids_in_args({"group_ids": ["g1", None, ""]}) == {"g1"}
    assert _group_ids_in_args({"groups": ""}) == set()


# ── deriving a thread's scope ─────────────────────────────────────────────


async def archive(db, session_id, role, content, *, user_id=USER,
                  client_group_id=None, when=None):
    await log_message(
        db, user_id=user_id, session_id=session_id, source="birdy",
        role=role, content=content, client_group_id=client_group_id,
    )
    if when is not None:
        await db["ai_conversation_log"].update_one(
            {"session_id": session_id, "content": content},
            {"$set": {"created_at": when}},
        )


@pytest.mark.asyncio
async def test_an_untagged_thread_is_global(mock_db):
    await archive(mock_db, "s1", "user", "How is the whole account?")

    assert await _conversation_scope(mock_db, USER, "s1") == {
        "scope": "global", "client_group_id": None, "client_name": None,
    }


@pytest.mark.asyncio
async def test_a_thread_about_one_client_carries_its_name(mock_db):
    await mock_db["client_groups"].insert_one(
        {"id": "g1", "user_id": USER, "name": "Aura"}
    )
    await archive(mock_db, "s1", "user", "How is Aura?", client_group_id="g1")

    assert await _conversation_scope(mock_db, USER, "s1") == {
        "scope": "client", "client_group_id": "g1", "client_name": "Aura",
    }


@pytest.mark.asyncio
async def test_a_second_client_flips_the_thread_to_global(mock_db):
    """Derived per turn rather than stored, so the flip is automatic — a
    thread that started about one client stops claiming to be about it the
    moment another one is asked about."""
    await archive(mock_db, "s1", "user", "How is Aura?", client_group_id="g1")
    await archive(mock_db, "s1", "user", "And Bright Smile?", client_group_id="g2")

    scope = await _conversation_scope(mock_db, USER, "s1")
    assert scope["scope"] == "global"
    assert scope["client_group_id"] is None


@pytest.mark.asyncio
async def test_a_client_scoped_thread_ignores_its_untagged_turns(mock_db):
    """Assistant turns and pre-tagging questions carry no client. They must
    not count as a "second client" and demote the thread."""
    await mock_db["client_groups"].insert_one(
        {"id": "g1", "user_id": USER, "name": "Aura"}
    )
    await archive(mock_db, "s1", "user", "How is Aura?", client_group_id="g1")
    await archive(mock_db, "s1", "assistant", "CPL is £12.40.")

    scope = await _conversation_scope(mock_db, USER, "s1")
    assert scope["scope"] == "client"
    assert scope["client_name"] == "Aura"


@pytest.mark.asyncio
async def test_scope_never_resolves_another_users_client(mock_db):
    await mock_db["client_groups"].insert_one(
        {"id": "g1", "user_id": OTHER, "name": "Their Client"}
    )
    await archive(mock_db, "s1", "user", "About g1", client_group_id="g1")

    scope = await _conversation_scope(mock_db, USER, "s1")
    assert scope["scope"] == "client"
    assert scope["client_name"] is None, "a name is never borrowed across users"


@pytest.mark.asyncio
async def test_scope_reads_only_this_session(mock_db):
    await archive(mock_db, "s1", "user", "About Aura", client_group_id="g1")
    await archive(mock_db, "s2", "user", "About someone else", client_group_id="g2")

    scope = await _conversation_scope(mock_db, USER, "s1")
    assert scope["client_group_id"] == "g1"


# ── tagging the turn after the tool loop ──────────────────────────────────


@pytest.mark.asyncio
async def test_tagging_attaches_the_client_to_the_newest_untagged_question(mock_db):
    """The question is archived before the tool loop runs, so which client it
    was about is only known once the loop is done."""
    await archive(mock_db, "s1", "user", "How is Aura?",
                  when=datetime(2026, 8, 1, 12, 0))

    await tag_latest_user_turn(
        mock_db, session_id="s1", user_id=USER, client_group_id="g1"
    )

    row = await mock_db["ai_conversation_log"].find_one({"content": "How is Aura?"})
    assert row["client_group_id"] == "g1"


@pytest.mark.asyncio
async def test_tagging_leaves_earlier_tagged_turns_alone(mock_db):
    await archive(mock_db, "s1", "user", "First question", client_group_id="g1",
                  when=datetime(2026, 8, 1, 12, 0))
    await archive(mock_db, "s1", "user", "Second question",
                  when=datetime(2026, 8, 1, 12, 5))

    await tag_latest_user_turn(
        mock_db, session_id="s1", user_id=USER, client_group_id="g2"
    )

    first = await mock_db["ai_conversation_log"].find_one({"content": "First question"})
    second = await mock_db["ai_conversation_log"].find_one({"content": "Second question"})
    assert first["client_group_id"] == "g1", "an existing tag is never rewritten"
    assert second["client_group_id"] == "g2"
