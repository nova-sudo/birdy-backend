"""
tests/conftest.py
-------------------
Shared fixtures for the MCP tool-calling test suite.

All tests here run against an in-memory mongomock database — never the real
MONGODB_URI. `core.mongo_client.get_shared_mongo_client()` is a bare module-
level singleton (`_client`), so seeding `core.mongo_client._client` with a
mock before a test runs makes every `ai/mcp/*.py` tool (all of which call
get_shared_mongo_client() themselves) transparently use the mock — no need to
patch each module's own imported reference individually.
"""

import types

import pytest

import core.mongo_client
from mongomock_motor import AsyncMongoMockClient
from core.database import DB_NAME


@pytest.fixture
def mock_mongo_client():
    """A fresh in-memory Mongo client, wired up as the shared singleton for the duration of the test."""
    client = AsyncMongoMockClient()
    core.mongo_client._client = client
    yield client
    core.mongo_client._client = None


@pytest.fixture
def mock_db(mock_mongo_client):
    """The (mock) application database, matching core.database.get_db()'s DB_NAME."""
    return mock_mongo_client[DB_NAME]


@pytest.fixture
def set_current_user(monkeypatch):
    """Factory fixture: set_current_user("user@example.com") makes every MCP tool's
    _current_user_id() return that value, as if an authenticated MCP request came in.
    """

    def _set(user_id: str):
        fake_token = types.SimpleNamespace(claims={"sub": user_id})
        monkeypatch.setattr("ai.mcp.server.get_access_token", lambda: fake_token)

    return _set


@pytest.fixture
def no_current_user(monkeypatch):
    """Simulate an unauthenticated MCP request (no/invalid bearer token)."""
    monkeypatch.setattr("ai.mcp.server.get_access_token", lambda: None)
