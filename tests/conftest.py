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

import ipaddress
import os
import socket
import traceback
import types
from urllib.parse import urlsplit

import pytest
from dotenv import load_dotenv

load_dotenv()  # core/crypto.py and other modules read env vars at import time

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


# ---------------------------------------------------------------------------
# The network guard
# ---------------------------------------------------------------------------
# Nothing in the suite reaches past this machine unless it's marked `live`: be-test.sh loads real credentials, and app
# code swallows errors, so a real OpenAI or Slack call can pass unnoticed. The attempt is refused, and recorded so the
# test fails at teardown even when the code under test catches the error. Attempts outside any test (a background
# thread, collection) are listed at the end of the session.

BLOCKED: list[tuple[str | None, str]] = []  # (the test's id, what it tried)
_current: dict = {"test": None, "live": False}
_real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex
_real_sendto, _real_getaddrinfo = socket.socket.sendto, socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname
_resolved: dict[str, dict] = {}  # an allowed name's addresses, as the connect that follows its lookup only knows those


class OutboundConnectionBlocked(ConnectionError):
    """A test tried to leave this machine."""


def _is_local(host) -> bool:
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode(errors="replace")
    if not host or host == "localhost" or str(host).endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(str(host).split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified or getattr(getattr(address, "ipv4_mapped", None), "is_loopback", False)


def _mongo_cluster_suffix() -> str | None:
    """`.abc.mongodb.net` of a mongodb+srv:// URI: its shards are named from it. The host alone otherwise."""
    uri = os.environ.get("MONGODB_URI") or ""
    host = (urlsplit(uri).hostname or "") if "://" in uri and "," not in uri else ""
    if not host or _is_local(host):
        return None
    return "." + host.split(".", 1)[1] if uri.startswith("mongodb+srv:") and "." in host else host


# What already reached out before this guard, and still does. Nothing new goes here: it gets a fake instead.
ALLOWED_OUTBOUND: list[dict] = [
    # utils/currency_exchange.py builds its own Motor client from MONGODB_URI, so test_meta_campaign_filtering,
    # test_meta_campaign_scope and test_meta_daily_spend read the real dev database. Any test: its monitors run on threads.
    {"name": "real Mongo from CurrencyService", "host": _mongo_cluster_suffix(), "tests": None},
    # ai/suggestions/composer.py uses the provider factory, which since "Run chat on Birdy's own OpenAI account" finds the
    # key in .env. This file only pops ANTHROPIC_API_KEY, so it isn't hermetic as its docstring says.
    {"name": "real OpenAI from the suggestion composer", "host": "api.openai.com", "tests": "tests/test_suggestion_flow.py"},
]
ALLOWED_USED: set[str] = set()


def _app_frame() -> str:
    """The deepest of this repo's own code on the stack, so a failure says where to look."""
    frames = [f for f in traceback.extract_stack() if "site-packages" not in f.filename and "/lib/python" not in f.filename
              and f.filename != __file__]
    return f"{os.path.relpath(frames[-1].filename)}:{frames[-1].lineno}" if frames else "unknown"


def _allowance(name: str) -> dict | None:
    """The allowlist entry that covers this host, for the test running now."""
    entries = [_resolved[name]] if name in _resolved else ALLOWED_OUTBOUND
    for entry in entries:
        host, tests = entry["host"], entry["tests"]
        named = host and (name == host or (host.startswith(".") and name.endswith(host)))
        if (named or name in _resolved) and (tests is None or (_current["test"] or "").startswith(tests)):
            ALLOWED_USED.add(entry["name"])
            return entry
    return None


def _dns_question(data) -> str | None:
    """The name a DNS query asks for. A Mongo SRV lookup goes to this machine's resolver, whose address says nothing."""
    try:
        labels, at = [], 12
        while data[at]:
            labels.append(bytes(data[at + 1:at + 1 + data[at]]).decode())
            at += 1 + data[at]
        return ".".join(labels)
    except (IndexError, UnicodeDecodeError, TypeError):
        return None


def _check(host, what: str) -> None:
    if _current["live"] or _is_local(host):
        return
    name = host.decode(errors="replace") if isinstance(host, (bytes, bytearray)) else str(host)
    if _allowance(name):
        return
    BLOCKED.append((_current["test"], f"{what} {name} from {_app_frame()}"))
    raise OutboundConnectionBlocked(f"{_current['test'] or 'outside any test'}: {what} {name}. Mark it `live` or use a fake.")


def _check_socket(sock, address, what: str) -> None:
    if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
        _check(address[0], what)


def _guarded_connect(self, address):
    _check_socket(self, address, "connect to")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_socket(self, address, "connect to")
    return _real_connect_ex(self, address)


def _guarded_sendto(self, data, *args):
    address = args[-1]
    question = _dns_question(data) if isinstance(address, tuple) and address[1] == 53 else None
    if not (question and _allowance(question)):
        _check_socket(self, address, "send to")
    return _real_sendto(self, data, *args)


def _guarded_getaddrinfo(host, *args, **kwargs):
    _check(host, "look up")
    results = _real_getaddrinfo(host, *args, **kwargs)
    name = host.decode(errors="replace") if isinstance(host, (bytes, bytearray)) else host
    entry = _allowance(name) if name and not _is_local(name) and not _current["live"] else None
    if entry:
        _resolved.update({r[4][0]: entry for r in results})
    return results


def _guarded_gethostbyname(host):
    _check(host, "look up")
    return _real_gethostbyname(host)


def pytest_configure(config):
    socket.socket.connect, socket.socket.connect_ex = _guarded_connect, _guarded_connect_ex
    socket.socket.sendto = _guarded_sendto
    socket.getaddrinfo, socket.gethostbyname = _guarded_getaddrinfo, _guarded_gethostbyname


@pytest.fixture(autouse=True)
def _no_outbound_connections(request):
    start = len(BLOCKED)
    _current.update(test=request.node.nodeid, live=request.node.get_closest_marker("live") is not None)
    yield
    _current.update(test=None, live=False)
    tried = sorted({what for _, what in BLOCKED[start:]})
    if tried:
        pytest.fail(f"tried to leave this machine: {tried}", pytrace=False)


def pytest_terminal_summary(terminalreporter):
    if ALLOWED_USED:
        terminalreporter.section("network guard: allowlisted, and reached this run")
        for name in sorted(ALLOWED_USED):
            terminalreporter.write_line(name)
    outside = sorted({what for test, what in BLOCKED if test is None})
    if outside:
        terminalreporter.section("network guard: attempts outside any test")
        for what in outside:
            terminalreporter.write_line(what)
