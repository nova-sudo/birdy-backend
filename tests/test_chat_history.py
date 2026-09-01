"""
tests/test_chat_history.py
--------------------------
Conversation history: resuming an expired session from the durable archive,
and the endpoints that let a user read their own past chats.

The resume path matters more than it looks. Working memory expires after an
hour; before this, reopening an older conversation silently handed the model an
EMPTY history while still showing the user their old transcript, so follow-up
questions were answered with no grounding at all — the exact failure
ai/session_store.py's docstring was written about.
"""

import contextlib
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import routers.chat as chat_router
from ai import session_store
from ai.conversation_log import log_message

USER = "user@example.com"
OTHER = "someone@else.com"


@pytest.fixture
def chat_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    return chat_router


async def archive(db, *, session_id, role, content, user_id=USER,
                  source="birdy", when=None, client_group_id=None):
    await log_message(
        db, user_id=user_id, session_id=session_id, source=source,
        role=role, content=content, client_group_id=client_group_id,
    )
    if when is not None:
        await db["ai_conversation_log"].update_one(
            {"session_id": session_id, "content": content},
            {"$set": {"created_at": when}},
        )


async def a_conversation(db, session_id, turns, user_id=USER, source="birdy",
                         start=None, client_group_id=None):
    """Archive an alternating user/assistant exchange."""
    base = start or datetime(2026, 8, 1, 12, 0, 0)
    for i, text in enumerate(turns):
        await archive(
            db, session_id=session_id, user_id=user_id, source=source,
            role="user" if i % 2 == 0 else "assistant", content=text,
            when=base + timedelta(minutes=i), client_group_id=client_group_id,
        )


# ── resuming an expired session ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_session_resumes_from_the_archive(mock_db):
    await a_conversation(mock_db, "chat_old", [
        "How many leads this week?", "412 leads.",
        "And last week?", "377 leads.",
    ])

    # Nothing in working memory — an hour of silence has passed.
    session_id, history = await session_store.get_or_create(mock_db, "chat_old", USER)

    assert session_id == "chat_old", "resumed conversations keep their id"
    assert [m["content"] for m in history] == [
        "How many leads this week?", "412 leads.",
        "And last week?", "377 leads.",
    ]


@pytest.mark.asyncio
async def test_resume_writes_working_memory_back(mock_db):
    await a_conversation(mock_db, "chat_old", ["Question?", "Answer."])

    await session_store.get_or_create(mock_db, "chat_old", USER)

    doc = await mock_db["ai_chat_sessions"].find_one({"_id": "chat_old"})
    assert doc is not None
    assert len(doc["messages"]) == 2
    assert doc["user_id"] == USER


@pytest.mark.asyncio
async def test_live_session_is_preferred_over_the_archive(mock_db):
    """Working memory carries tool calls the archive never had; don't clobber it."""
    sid = await session_store.create_session(mock_db, USER)
    await session_store.save_messages(mock_db, sid, [
        {"role": "user", "content": "live question"},
    ])
    await a_conversation(mock_db, sid, ["stale archived", "stale reply"])

    _, history = await session_store.get_or_create(mock_db, sid, USER)
    assert [m["content"] for m in history] == ["live question"]


@pytest.mark.asyncio
async def test_another_users_session_is_never_resumed(mock_db):
    await a_conversation(mock_db, "chat_theirs", ["Their secret", "Their reply"],
                         user_id=OTHER)

    session_id, history = await session_store.get_or_create(
        mock_db, "chat_theirs", USER
    )

    assert session_id != "chat_theirs"
    assert history == []


@pytest.mark.asyncio
async def test_unknown_session_still_starts_fresh(mock_db):
    session_id, history = await session_store.get_or_create(mock_db, "chat_nope", USER)
    assert session_id != "chat_nope"
    assert history == []


@pytest.mark.asyncio
async def test_resume_keeps_the_most_recent_turns_within_the_cap(mock_db):
    turns = [f"turn {i}" for i in range(session_store.MAX_MESSAGES + 20)]
    await a_conversation(mock_db, "chat_long", turns)

    _, history = await session_store.get_or_create(mock_db, "chat_long", USER)

    assert len(history) == session_store.MAX_MESSAGES
    # The tail, in order — an out-of-order replay would confuse the model more
    # than no history at all.
    assert history[-1]["content"] == turns[-1]
    assert history[0]["content"] == turns[-session_store.MAX_MESSAGES]


@pytest.mark.asyncio
async def test_resumed_history_is_chronological(mock_db):
    await a_conversation(mock_db, "chat_order", ["first", "second", "third", "fourth"])

    _, history = await session_store.get_or_create(mock_db, "chat_order", USER)
    assert [m["content"] for m in history] == ["first", "second", "third", "fourth"]


# ── listing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_titles_each_chat_from_its_opening_question(chat_api, mock_db):
    await a_conversation(mock_db, "s1", ["What is my CPL?", "£12.40."])

    out = await chat_router.list_conversations(current_user=USER)

    assert len(out["conversations"]) == 1
    assert out["conversations"][0]["title"] == "What is my CPL?"
    assert out["conversations"][0]["session_id"] == "s1"
    assert out["conversations"][0]["message_count"] == 2


@pytest.mark.asyncio
async def test_list_is_most_recently_active_first(chat_api, mock_db):
    await a_conversation(mock_db, "older", ["Older chat", "reply"],
                         start=datetime(2026, 8, 1, 9, 0))
    await a_conversation(mock_db, "newer", ["Newer chat", "reply"],
                         start=datetime(2026, 8, 20, 9, 0))

    out = await chat_router.list_conversations(current_user=USER)
    assert [c["session_id"] for c in out["conversations"]] == ["newer", "older"]


@pytest.mark.asyncio
async def test_list_excludes_other_users(chat_api, mock_db):
    await a_conversation(mock_db, "mine", ["Mine", "reply"])
    await a_conversation(mock_db, "theirs", ["Theirs", "reply"], user_id=OTHER)

    out = await chat_router.list_conversations(current_user=USER)
    assert [c["session_id"] for c in out["conversations"]] == ["mine"]


@pytest.mark.asyncio
async def test_list_excludes_slack_threads(chat_api, mock_db):
    """The same archive holds the Slack bot; those aren't web conversations."""
    await a_conversation(mock_db, "web", ["From the app", "reply"])
    await a_conversation(mock_db, "slack", ["From Slack", "reply"], source="slack")

    out = await chat_router.list_conversations(current_user=USER)
    assert [c["session_id"] for c in out["conversations"]] == ["web"]


@pytest.mark.asyncio
async def test_list_skips_protocol_noise_when_titling(chat_api, mock_db):
    await archive(mock_db, session_id="s1", role="user",
                  content='[UI_RESPONSE] {"choice":"yes"}',
                  when=datetime(2026, 8, 1, 12, 0))
    await archive(mock_db, session_id="s1", role="user",
                  content="The real question",
                  when=datetime(2026, 8, 1, 12, 1))

    out = await chat_router.list_conversations(current_user=USER)
    assert out["conversations"][0]["title"] == "The real question"


@pytest.mark.asyncio
async def test_long_titles_are_truncated(chat_api, mock_db):
    long_q = "x" * 200
    await a_conversation(mock_db, "s1", [long_q, "reply"])

    out = await chat_router.list_conversations(current_user=USER)
    title = out["conversations"][0]["title"]
    assert len(title) == chat_router.TITLE_LENGTH + 1  # + the ellipsis
    assert title.endswith("…")


@pytest.mark.asyncio
async def test_list_is_empty_for_a_user_with_no_history(chat_api, mock_db):
    out = await chat_router.list_conversations(current_user=USER)
    assert out["conversations"] == []


# ── reading one conversation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_the_full_exchange_in_order(chat_api, mock_db):
    await a_conversation(mock_db, "s1", ["Q1", "A1", "Q2", "A2"])

    out = await chat_router.get_conversation("s1", current_user=USER)

    assert [m["content"] for m in out["messages"]] == ["Q1", "A1", "Q2", "A2"]
    assert [m["role"] for m in out["messages"]] == [
        "user", "assistant", "user", "assistant",
    ]


@pytest.mark.asyncio
async def test_get_refuses_another_users_conversation(chat_api, mock_db):
    await a_conversation(mock_db, "theirs", ["Their secret", "reply"], user_id=OTHER)

    with pytest.raises(HTTPException) as exc:
        await chat_router.get_conversation("theirs", current_user=USER)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_of_a_missing_conversation_is_404(chat_api, mock_db):
    with pytest.raises(HTTPException) as exc:
        await chat_router.get_conversation("nope", current_user=USER)
    assert exc.value.status_code == 404


# ── deleting ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_the_archive_and_working_memory(chat_api, mock_db):
    await a_conversation(mock_db, "s1", ["Q", "A"])
    await mock_db["ai_chat_sessions"].insert_one(
        {"_id": "s1", "user_id": USER, "messages": [{"role": "user", "content": "Q"}]}
    )

    out = await chat_router.delete_conversation("s1", current_user=USER)

    assert out["deleted"] == 2
    assert await mock_db["ai_conversation_log"].count_documents({"session_id": "s1"}) == 0
    assert await mock_db["ai_chat_sessions"].find_one({"_id": "s1"}) is None


@pytest.mark.asyncio
async def test_delete_leaves_other_conversations_alone(chat_api, mock_db):
    await a_conversation(mock_db, "keep", ["Keep", "reply"])
    await a_conversation(mock_db, "drop", ["Drop", "reply"])

    await chat_router.delete_conversation("drop", current_user=USER)

    out = await chat_router.list_conversations(current_user=USER)
    assert [c["session_id"] for c in out["conversations"]] == ["keep"]


@pytest.mark.asyncio
async def test_delete_refuses_another_users_conversation(chat_api, mock_db):
    await a_conversation(mock_db, "theirs", ["Theirs", "reply"], user_id=OTHER)

    with pytest.raises(HTTPException) as exc:
        await chat_router.delete_conversation("theirs", current_user=USER)
    assert exc.value.status_code == 404
    assert await mock_db["ai_conversation_log"].count_documents(
        {"session_id": "theirs"}
    ) == 2


# ── scoping a thread list to one client ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_scoped_list_only_shows_that_client(chat_api, mock_db):
    """Asking about one client must never surface what was asked about
    another — the Client Detail rail is per client."""
    await a_conversation(mock_db, "aura", ["About Aura", "reply"], client_group_id="g1")
    await a_conversation(mock_db, "other", ["About someone else", "reply"], client_group_id="g2")

    out = await chat_router.list_conversations(client_group_id="g1", current_user=USER)
    assert [c["session_id"] for c in out["conversations"]] == ["aura"]


@pytest.mark.asyncio
async def test_a_scoped_list_excludes_unscoped_threads(chat_api, mock_db):
    """Threads logged before the field existed carry no client. They stay out
    of a scoped list rather than being attributed to whichever client is open."""
    await a_conversation(mock_db, "general", ["No client attached", "reply"])
    await a_conversation(mock_db, "aura", ["About Aura", "reply"], client_group_id="g1")

    out = await chat_router.list_conversations(client_group_id="g1", current_user=USER)
    assert [c["session_id"] for c in out["conversations"]] == ["aura"]


@pytest.mark.asyncio
async def test_the_unscoped_list_still_shows_everything(chat_api, mock_db):
    await a_conversation(mock_db, "general", ["No client", "reply"],
                         start=datetime(2026, 8, 1, 9, 0))
    await a_conversation(mock_db, "aura", ["About Aura", "reply"],
                         start=datetime(2026, 8, 2, 9, 0), client_group_id="g1")

    out = await chat_router.list_conversations(current_user=USER)
    assert sorted(c["session_id"] for c in out["conversations"]) == ["aura", "general"]


@pytest.mark.asyncio
async def test_a_scoped_list_still_excludes_other_users(chat_api, mock_db):
    await a_conversation(mock_db, "theirs", ["Their Aura thread", "reply"],
                         user_id=OTHER, client_group_id="g1")

    out = await chat_router.list_conversations(client_group_id="g1", current_user=USER)
    assert out["conversations"] == []


# ── global vs client scope, as the Ask Birdy sidebar reads it ─────────────
#
# Scope is DERIVED, never stored on the thread: exactly one tagged client
# means a client conversation, anything else is global. The rule lives in two
# places that must agree — the orchestrator (which badges the reply as it
# lands) and the list endpoint (which badges the sidebar) — so both are
# tested against the same cases.


async def a_client_group(db, group_id, name, user_id=USER):
    await db["client_groups"].insert_one(
        {"id": group_id, "user_id": user_id, "name": name}
    )


@pytest.mark.asyncio
async def test_an_untagged_thread_lists_as_global(chat_api, mock_db):
    await a_conversation(mock_db, "s1", ["How is the whole account?", "reply"])

    convo = (await chat_router.list_conversations(current_user=USER))["conversations"][0]
    assert convo["scope"] == "global"
    assert convo["client_group_id"] is None
    assert convo["client_name"] is None


@pytest.mark.asyncio
async def test_a_single_client_thread_lists_with_that_clients_name(chat_api, mock_db):
    await a_client_group(mock_db, "g1", "Aura")
    await a_conversation(mock_db, "s1", ["How is Aura doing?", "reply"],
                         client_group_id="g1")

    convo = (await chat_router.list_conversations(current_user=USER))["conversations"][0]
    assert convo["scope"] == "client"
    assert convo["client_group_id"] == "g1"
    assert convo["client_name"] == "Aura"


@pytest.mark.asyncio
async def test_a_thread_that_moves_to_a_second_client_reads_as_global(chat_api, mock_db):
    """The badge follows the thread, not its opening question: once a second
    client is in the transcript, no single client's name describes it."""
    await a_client_group(mock_db, "g1", "Aura")
    await a_client_group(mock_db, "g2", "Bright Smile")
    await archive(mock_db, session_id="s1", role="user", content="How is Aura?",
                  client_group_id="g1", when=datetime(2026, 8, 1, 12, 0))
    await archive(mock_db, session_id="s1", role="user", content="And Bright Smile?",
                  client_group_id="g2", when=datetime(2026, 8, 1, 12, 5))

    convo = (await chat_router.list_conversations(current_user=USER))["conversations"][0]
    assert convo["scope"] == "global"
    assert convo["client_name"] is None


@pytest.mark.asyncio
async def test_a_thread_whose_client_was_deleted_reads_as_global(chat_api, mock_db):
    """A dangling tag would otherwise badge the row with a blank client name."""
    await a_conversation(mock_db, "s1", ["About a since-deleted client", "reply"],
                         client_group_id="gone")

    convo = (await chat_router.list_conversations(current_user=USER))["conversations"][0]
    assert convo["scope"] == "global"
    assert convo["client_name"] is None


@pytest.mark.asyncio
async def test_a_thread_never_borrows_another_users_client_name(chat_api, mock_db):
    """Group ids are only resolved within the caller's own groups."""
    await a_client_group(mock_db, "g1", "Their Client", user_id=OTHER)
    await a_conversation(mock_db, "s1", ["About g1", "reply"], client_group_id="g1")

    convo = (await chat_router.list_conversations(current_user=USER))["conversations"][0]
    assert convo["scope"] == "global"
    assert convo["client_name"] is None
