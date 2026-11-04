"""
tests/test_network_guard.py
---------------------------
The socket guard in conftest.py: nothing in the suite reaches past this machine unless it's marked `live`.
"""

import os
import socket
import subprocess
import sys
import tempfile

import pytest

import conftest


@pytest.fixture
def expect_blocked():
    """These tests trip the guard on purpose, so what they record isn't held against them."""
    before = len(conftest.BLOCKED)
    yield
    assert len(conftest.BLOCKED) > before, "the guard didn't see it"
    del conftest.BLOCKED[before:]


def test_connect_outward_is_blocked(expect_blocked):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(conftest.OutboundConnectionBlocked):
        s.connect(("93.184.216.34", 443))


def test_connect_ex_outward_is_blocked(expect_blocked):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(conftest.OutboundConnectionBlocked):
        s.connect_ex(("93.184.216.34", 443))


def test_udp_outward_is_blocked(expect_blocked):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(conftest.OutboundConnectionBlocked):
        s.sendto(b"x", ("8.8.8.8", 53))


def test_a_name_lookup_outward_is_blocked(expect_blocked):
    with pytest.raises(conftest.OutboundConnectionBlocked):
        socket.getaddrinfo("api.openai.com", 443)


def test_swallowing_the_error_doesnt_hide_it(expect_blocked):
    """Code here that catches every Exception and falls back is exactly what the guard is for."""
    try:
        socket.getaddrinfo("api.openai.com", 443)
    except Exception:
        pass


if os.environ.get("GUARD_OUTSIDE_A_TEST"):  # runs at collection, in the real pytest run below
    try:
        socket.getaddrinfo("api.openai.com", 443)
    except Exception:
        pass


@pytest.mark.skipif(not os.environ.get("GUARD_SELF_TEST"), reason="only run by the test below, which expects it to fail")
def test_inner_swallows_a_blocked_lookup():
    try:
        socket.getaddrinfo("api.openai.com", 443)
    except Exception:
        pass


def test_a_test_that_swallows_the_error_still_fails():
    """Through a real pytest run: the point of the guard is that app code catching the error can't hide the attempt."""
    done = subprocess.run([sys.executable, "-m", "pytest", f"{__file__}::test_inner_swallows_a_blocked_lookup", "-q", "-p", "no:cacheprovider"],
                          env={**os.environ, "GUARD_SELF_TEST": "1"}, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "tried to leave this machine" in done.stdout


def test_loopback_is_allowed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(server.getsockname())
    assert socket.getaddrinfo("localhost", 80)


def test_a_unix_socket_is_allowed():
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/s"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)


@pytest.mark.parametrize("host,local", [("localhost", True), ("127.0.0.1", True), ("::1", True), (None, True), ("", True),
                                        ("10.0.0.1", False), ("slack.com", False), (b"localhost", True)])
def test_what_counts_as_local(host, local):
    assert conftest._is_local(host) is local


def test_an_allowed_host_only_counts_where_its_entry_says(monkeypatch, expect_blocked):
    monkeypatch.setattr(conftest, "ALLOWED_OUTBOUND", [{"name": "the guard's own test", "host": "api.example.test", "tests": "tests/somewhere_else.py"}])
    with pytest.raises(conftest.OutboundConnectionBlocked):
        conftest._check("api.example.test", "look up")


def test_an_allowed_suffix_covers_a_clusters_shards_and_no_other_host(monkeypatch, expect_blocked):
    monkeypatch.setattr(conftest, "ALLOWED_OUTBOUND", [{"name": "the guard's own test", "host": ".abc.mongodb.net", "tests": ("tests/test_network_guard.py",)}])
    conftest._check("ac-1-shard-00-00.abc.mongodb.net", "look up")
    with pytest.raises(conftest.OutboundConnectionBlocked):
        conftest._check("evil.mongodb.net", "look up")


def test_a_dns_query_counts_by_the_name_it_asks_for(monkeypatch, expect_blocked):
    monkeypatch.setattr(conftest, "ALLOWED_OUTBOUND", [{"name": "the guard's own test", "host": ".abc.mongodb.net", "tests": ("tests/test_network_guard.py",)}])
    query = bytes(12) + b"\x08_mongodb\x04_tcp\x04prod\x03abc\x07mongodb\x03net\x00\x00\x21\x00\x01"
    assert conftest._dns_question(query) == "_mongodb._tcp.prod.abc.mongodb.net"
    assert conftest._allowance(conftest._dns_question(query))
    other = bytes(12) + b"\x06openai\x03com\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(conftest.OutboundConnectionBlocked):
        s.sendto(other, ("100.95.0.251", 53))


def test_a_live_test_may_leave(monkeypatch):
    monkeypatch.setitem(conftest._current, "live", True)
    conftest._check("api.openai.com", "look up")


def _inner_run(test: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", f"{__file__}::{test}", "-q", "-p", "no:cacheprovider"],
                          env={**os.environ, **env}, capture_output=True, text=True, timeout=60)


def test_an_attempt_outside_any_test_fails_the_run():
    done = _inner_run("test_a_live_test_may_leave", GUARD_OUTSIDE_A_TEST="1")
    assert done.returncode != 0 and "1 passed" in done.stdout and "attempts outside any test" in done.stdout


def test_sendmsg_with_an_address_is_blocked(expect_blocked):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(conftest.OutboundConnectionBlocked):
        s.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))


def test_sendmsg_without_an_address_still_works():
    a, b = socket.socketpair()
    with a, b:
        assert a.sendmsg([b"x"]) == 1 and b.recv(1) == b"x"


def test_the_cluster_is_only_allowed_for_the_tests_named(monkeypatch):
    entry = next(e for e in conftest.ALLOWED_OUTBOUND if "Mongo" in e["name"])
    monkeypatch.setitem(entry, "host", ".abc.mongodb.net")
    named = entry["tests"]
    assert isinstance(named, tuple) and len(named) == 5 and all("::" in t for t in named)
    assert conftest._allowance("ac-1.abc.mongodb.net") is None
    monkeypatch.setitem(conftest._current, "test", named[0])
    assert conftest._allowance("ac-1.abc.mongodb.net") is entry


@pytest.mark.skipif(not os.environ.get("GUARD_SELF_TEST_MONGO"), reason="only run by the test below, which expects it to fail")
async def test_inner_a_new_test_opens_a_real_client():
    from motor.motor_asyncio import AsyncIOMotorClient
    try:
        await AsyncIOMotorClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=500).admin.command("ping")
    except Exception:
        pass


@pytest.mark.skipif(not (os.environ.get("MONGODB_URI") or "").startswith("mongodb+srv:"), reason="needs a cluster to be refused")
def test_a_new_test_that_reaches_the_dev_cluster_fails():
    done = _inner_run("test_inner_a_new_test_opens_a_real_client", GUARD_SELF_TEST_MONGO="1")
    assert done.returncode != 0 and "tried to leave this machine" in done.stdout


def test_a_closing_mongo_monitor_is_refused_without_failing_the_test_it_lands_in():
    import threading
    out = []

    def monitor():
        try:
            socket.getaddrinfo("ac-1.abc.mongodb.net", 27017)
        except conftest.OutboundConnectionBlocked:
            out.append("refused")

    before = len(conftest.BLOCKED)
    thread = threading.Thread(target=monitor, name="pymongo_server_monitor_thread")
    thread.start()
    thread.join()
    assert out == ["refused"] and len(conftest.BLOCKED) == before
