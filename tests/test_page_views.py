"""
tests/test_page_views.py
------------------------
Named page views (users.page_views) — the saved presets behind the view picker
on Clients, Leads and Marketing.

The endpoints are exercised directly rather than over HTTP: they are plain async
functions whose only dependency is `get_mongo_client`, so patching that against
mongomock covers the logic that actually matters (default-pointer bookkeeping,
name collisions, the per-page cap) without standing up a test client.
"""

import contextlib

import pytest

import routers.settings as settings
from core.database import DB_NAME
from core.models import (
    CreatePageViewRequest,
    UpdatePageViewRequest,
    DefaultPageViewRequest,
)
from fastapi import HTTPException

USER = "user@example.com"


@pytest.fixture
def views_api(mock_mongo_client, monkeypatch):
    """Point the settings router at the in-memory client."""

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(settings, "get_mongo_client", fake_client)
    return settings


async def _create(name, page="clients", state=None):
    return await settings.create_page_view(
        CreatePageViewRequest(page=page, name=name, state=state or {"columns": ["a"]}),
        current_user=USER,
    )


# ── create ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_view_and_persists_it(views_api, mock_db):
    view = await _create("UK actives", state={"columns": ["name"], "statusFilter": "active"})

    assert view["name"] == "UK actives"
    assert view["state"]["statusFilter"] == "active"
    assert view["id"]

    stored = await mock_db["users"].find_one({"user_id": USER})
    assert stored["page_views"]["clients"]["views"][0]["id"] == view["id"]


@pytest.mark.asyncio
async def test_first_view_on_a_page_becomes_the_default(views_api):
    first = await _create("First")
    second = await _create("Second")

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert bucket["default_view_id"] == first["id"]
    assert bucket["default_view_id"] != second["id"]


@pytest.mark.asyncio
async def test_pages_are_isolated_from_each_other(views_api):
    await _create("Clients view", page="clients")
    await _create("Leads view", page="contacts")

    clients = await settings.get_page_views(page="clients", current_user=USER)
    contacts = await settings.get_page_views(page="contacts", current_user=USER)

    assert [v["name"] for v in clients["views"]] == ["Clients view"]
    assert [v["name"] for v in contacts["views"]] == ["Leads view"]


@pytest.mark.asyncio
async def test_duplicate_name_is_rejected_case_insensitively(views_api):
    await _create("Top spenders")

    with pytest.raises(HTTPException) as exc:
        await _create("TOP SPENDERS")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_blank_name_is_rejected(views_api):
    with pytest.raises(HTTPException) as exc:
        await _create("   ")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_name_is_trimmed(views_api):
    view = await _create("  Padded  ")
    assert view["name"] == "Padded"


@pytest.mark.asyncio
async def test_per_page_cap_is_enforced(views_api):
    for i in range(settings.MAX_VIEWS_PER_PAGE):
        await _create(f"View {i}")

    with pytest.raises(HTTPException) as exc:
        await _create("One too many")
    assert exc.value.status_code == 400
    assert "up to" in exc.value.detail


@pytest.mark.asyncio
async def test_oversized_state_is_rejected(views_api):
    huge = {"blob": "x" * (settings.MAX_VIEW_STATE_BYTES + 1)}

    with pytest.raises(HTTPException) as exc:
        await _create("Huge", state=huge)
    assert exc.value.status_code == 400
    assert "too large" in exc.value.detail


# ── read ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_for_unknown_page_returns_empty_bucket(views_api):
    bucket = await settings.get_page_views(page="nope", current_user=USER)
    assert bucket == {"views": [], "default_view_id": None}


@pytest.mark.asyncio
async def test_get_without_page_returns_every_page(views_api):
    await _create("A", page="clients")
    await _create("B", page="contacts")

    everything = await settings.get_page_views(current_user=USER)
    assert set(everything) == {"clients", "contacts"}


# ── update ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_leaves_state_untouched(views_api):
    view = await _create("Old name", state={"columns": ["a", "b"]})

    updated = await settings.update_page_view(
        view["id"], UpdatePageViewRequest(page="clients", name="New name"), current_user=USER
    )

    assert updated["name"] == "New name"
    assert updated["state"] == {"columns": ["a", "b"]}


@pytest.mark.asyncio
async def test_state_overwrite_leaves_name_untouched(views_api):
    view = await _create("Keep me", state={"columns": ["a"]})

    updated = await settings.update_page_view(
        view["id"],
        UpdatePageViewRequest(page="clients", state={"columns": ["z"], "sort": "desc"}),
        current_user=USER,
    )

    assert updated["name"] == "Keep me"
    assert updated["state"] == {"columns": ["z"], "sort": "desc"}


@pytest.mark.asyncio
async def test_update_with_nothing_to_change_is_rejected(views_api):
    view = await _create("Untouched")

    with pytest.raises(HTTPException) as exc:
        await settings.update_page_view(
            view["id"], UpdatePageViewRequest(page="clients"), current_user=USER
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_rename_onto_another_views_name_is_rejected(views_api):
    await _create("Taken")
    mine = await _create("Mine")

    with pytest.raises(HTTPException) as exc:
        await settings.update_page_view(
            mine["id"], UpdatePageViewRequest(page="clients", name="Taken"), current_user=USER
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_renaming_a_view_to_its_own_name_is_allowed(views_api):
    view = await _create("Same")

    updated = await settings.update_page_view(
        view["id"], UpdatePageViewRequest(page="clients", name="Same"), current_user=USER
    )
    assert updated["name"] == "Same"


@pytest.mark.asyncio
async def test_update_of_a_missing_view_is_404(views_api):
    await _create("Exists")

    with pytest.raises(HTTPException) as exc:
        await settings.update_page_view(
            "nope", UpdatePageViewRequest(page="clients", name="X"), current_user=USER
        )
    assert exc.value.status_code == 404


# ── delete ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_only_the_named_view(views_api):
    keep = await _create("Keep")
    drop = await _create("Drop")

    await settings.delete_page_view(drop["id"], page="clients", current_user=USER)

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert [v["id"] for v in bucket["views"]] == [keep["id"]]


@pytest.mark.asyncio
async def test_deleting_the_default_promotes_a_survivor(views_api):
    """A default pointing at a deleted view would silently drop the user onto an
    unsaved layout, so the next view inherits it."""
    default = await _create("Default")
    other = await _create("Other")

    await settings.delete_page_view(default["id"], page="clients", current_user=USER)

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert bucket["default_view_id"] == other["id"]


@pytest.mark.asyncio
async def test_deleting_the_last_view_clears_the_default(views_api):
    only = await _create("Only")

    await settings.delete_page_view(only["id"], page="clients", current_user=USER)

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert bucket["views"] == []
    assert bucket["default_view_id"] is None


@pytest.mark.asyncio
async def test_deleting_a_non_default_leaves_the_default_alone(views_api):
    default = await _create("Default")
    other = await _create("Other")

    await settings.delete_page_view(other["id"], page="clients", current_user=USER)

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert bucket["default_view_id"] == default["id"]


@pytest.mark.asyncio
async def test_delete_of_a_missing_view_is_404(views_api):
    with pytest.raises(HTTPException) as exc:
        await settings.delete_page_view("nope", page="clients", current_user=USER)
    assert exc.value.status_code == 404


# ── default pointer ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_can_be_moved_to_another_view(views_api):
    first = await _create("First")
    second = await _create("Second")

    await settings.set_default_page_view(
        DefaultPageViewRequest(page="clients", view_id=second["id"]), current_user=USER
    )

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert bucket["default_view_id"] == second["id"] != first["id"]


@pytest.mark.asyncio
async def test_default_can_be_cleared(views_api):
    await _create("Only")

    await settings.set_default_page_view(
        DefaultPageViewRequest(page="clients", view_id=None), current_user=USER
    )

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert bucket["default_view_id"] is None


@pytest.mark.asyncio
async def test_default_cannot_point_at_a_missing_view(views_api):
    await _create("Only")

    with pytest.raises(HTTPException) as exc:
        await settings.set_default_page_view(
            DefaultPageViewRequest(page="clients", view_id="nope"), current_user=USER
        )
    assert exc.value.status_code == 404


# ── isolation from the legacy single-view store ───────────────────────────


@pytest.mark.asyncio
async def test_named_views_do_not_disturb_legacy_saved_views(views_api, mock_db):
    """`saved_views` is the autosaved 'where I left off' layout and the tables
    still write to it. Creating presets must leave it exactly as it was."""
    await mock_db["users"].insert_one(
        {"user_id": USER, "saved_views": {"clients": ["name", "spend"]}}
    )

    await _create("A preset")

    stored = await mock_db["users"].find_one({"user_id": USER})
    assert stored["saved_views"]["clients"] == ["name", "spend"]
    assert len(stored["page_views"]["clients"]["views"]) == 1


# ── concurrency ───────────────────────────────────────────────────────────
# Two saves landing together used to both return 200 while one view silently
# vanished: each read the whole `views` array and wrote its own copy back, so
# the later write erased the earlier one. Observed live on 2026-08-28 as two
# concurrent POSTs both succeeding.


@pytest.mark.asyncio
async def test_concurrent_creates_both_survive(views_api):
    import asyncio

    await asyncio.gather(_create("First"), _create("Second"))

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert sorted(v["name"] for v in bucket["views"]) == ["First", "Second"]


@pytest.mark.asyncio
async def test_a_create_does_not_erase_an_existing_view(views_api, mock_db):
    """The array is appended to, never rewritten from a stale read."""
    existing = await _create("Existing")

    # Something else adds a view behind this request's back.
    await mock_db["users"].update_one(
        {"user_id": USER},
        {"$push": {"page_views.clients.views": {
            "id": "outside", "name": "Added elsewhere", "state": {},
            "created_at": "x", "updated_at": "x",
        }}},
    )

    await _create("Newest")

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    ids = [v["id"] for v in bucket["views"]]
    assert existing["id"] in ids
    assert "outside" in ids
    assert len(ids) == 3


@pytest.mark.asyncio
async def test_update_leaves_sibling_views_alone(views_api, mock_db):
    keep = await _create("Keep", state={"visibleColumns": ["a"]})
    target = await _create("Target", state={"visibleColumns": ["b"]})

    await settings.update_page_view(
        target["id"],
        UpdatePageViewRequest(page="clients", state={"visibleColumns": ["z"]}),
        current_user=USER,
    )

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    by_id = {v["id"]: v for v in bucket["views"]}
    assert by_id[keep["id"]]["state"] == {"visibleColumns": ["a"]}
    assert by_id[target["id"]]["state"] == {"visibleColumns": ["z"]}


@pytest.mark.asyncio
async def test_delete_does_not_erase_a_concurrently_added_view(views_api, mock_db):
    doomed = await _create("Doomed")
    await _create("Survivor")

    await settings.delete_page_view(doomed["id"], page="clients", current_user=USER)

    bucket = await settings.get_page_views(page="clients", current_user=USER)
    assert [v["name"] for v in bucket["views"]] == ["Survivor"]
