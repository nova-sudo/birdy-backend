"""
The ETag layer over the rollup reads.

The bandwidth win is easy to see and easy to get subtly wrong, so what's
pinned here is mostly the ways it could break something that already worked:
dropping a Set-Cookie, caching a write, or handing one account's 304 to
another.
"""

import json

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from middleware.etag import ETagMiddleware


BIG = {"client_groups": [{"id": f"g{i}", "name": "x" * 200} for i in range(50)]}


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(ETagMiddleware)
    state = {"payload": BIG}

    @app.get("/api/client-groups")
    async def groups():
        return state["payload"]

    @app.get("/api/client-groups/daily")
    async def daily():
        return {"series": {}}

    @app.post("/api/client-groups")
    async def create():
        return {"ok": True}

    @app.get("/api/settings/not-a-rollup")
    async def other():
        return {"ok": True}

    @app.get("/api/with-cookies")
    async def with_cookies(response: Response):
        # What TokenRefreshMiddleware does when an access token has expired.
        response.set_cookie("auth_token", "new-access")
        response.set_cookie("refresh_token", "new-refresh")
        return {"ok": True}

    c = TestClient(app)
    c.state = state
    return c


def test_tags_a_rollup_and_asks_the_browser_to_revalidate(client):
    r = client.get("/api/client-groups")
    assert r.status_code == 200
    assert r.headers["etag"]
    # no-cache means "store it, but check with me first" — not "don't store".
    assert r.headers["cache-control"] == "private, no-cache"
    assert "Cookie" in r.headers["vary"]


def test_unchanged_body_costs_no_body(client):
    first = client.get("/api/client-groups")
    again = client.get("/api/client-groups", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == first.headers["etag"]
    # The saving, stated plainly.
    assert len(first.content) > 10_000


def test_changed_body_is_sent_in_full(client):
    first = client.get("/api/client-groups")
    client.state["payload"] = {"client_groups": [{"id": "g1", "name": "renamed"}]}

    again = client.get("/api/client-groups", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 200
    assert again.headers["etag"] != first.headers["etag"]
    assert json.loads(again.content)["client_groups"][0]["name"] == "renamed"


def test_a_stale_etag_from_another_account_is_not_honoured(client):
    # Cache-Control is private and Vary names Cookie, but the real defence is
    # that the tag is a hash of this response: someone else's tag simply
    # doesn't match, so they get a full body rather than a wrong 304.
    r = client.get("/api/client-groups", headers={"If-None-Match": '"somebodyelsestag"'})
    assert r.status_code == 200


def test_writes_are_left_alone(client):
    r = client.post("/api/client-groups")
    assert "etag" not in r.headers
    assert "cache-control" not in r.headers


def test_endpoints_outside_the_list_are_left_alone(client):
    r = client.get("/api/settings/not-a-rollup")
    assert "etag" not in r.headers


def test_every_set_cookie_survives(client):
    # The bug this guards: rebuilding headers through a dict keeps only the
    # last Set-Cookie, so a response that refreshed both tokens would arrive
    # with one of them missing and log the user out on the next request.
    r = client.get("/api/with-cookies")

    set_cookies = [v for k, v in r.headers.raw if k.lower() == b"set-cookie"]
    assert len(set_cookies) == 2
    assert any(b"auth_token=" in c for c in set_cookies)
    assert any(b"refresh_token=" in c for c in set_cookies)


def test_content_type_and_length_are_right(client):
    r = client.get("/api/client-groups")
    assert r.headers["content-type"].startswith("application/json")
    assert int(r.headers["content-length"]) == len(r.content)
