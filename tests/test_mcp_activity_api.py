"""GET /api/mcp/activity*: list, filters, cursor, totals, detail, apps, CSV."""

import contextlib
import csv
import io
import time
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import routers.mcp_activity as activity_router
from core.config import JWT_SECRET, JWT_ALGORITHM
from main import app

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def api(mock_mongo_client, mock_db, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(activity_router, "get_mongo_client", fake_client)
    client = TestClient(app)
    client.db = mock_db
    token = pyjwt.encode({"sub": ALICE, "exp": int(time.time()) + 600, "type": "access"}, JWT_SECRET,
                         algorithm=JWT_ALGORITHM)
    client.cookies.set("auth_token", token)
    return client


def _ago(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) - timedelta(**delta)


def row(id, created_at, user=ALICE, grant="g1", app_name="Claude", kind="live", credits=0.5, **extra):
    return {
        "id": id, "user_id": user, "grant_id": grant, "app_name": app_name, "app_kind": "claude", "access": "write",
        "tool": "t", "kind": kind, "refusal_reason": None, "client_group_ids": [], "client_names": [],
        "summary": f"Summary {id}", "args": {"secret_arg": "x"}, "result_status": "ok", "result_excerpt": "EXCERPT",
        "confirmed_after": None, "link": None, "credits": {"request": credits, "data": 0, "live": 0, "total": credits},
        "response_chars": 10, "duration_ms": 5, "created_at": created_at, **extra,
    }


async def seed(db, *rows):
    await db["mcp_activity"].insert_many(list(rows))


@pytest.mark.asyncio
async def test_list_filters_and_cursor(api):
    await seed(
        api.db,
        *[row(f"a{i}", _ago(hours=i + 1)) for i in range(5)],
        row("other-app", _ago(hours=1), grant="g2", app_name="Cursor"),
        row("preview", _ago(hours=2), kind="preview"),
        row("old", _ago(days=30)),
        row("bobs", _ago(hours=1), user=BOB),
        row("acme", _ago(hours=3), client_group_ids=["cg1"], client_names=["Acme"]),
    )
    r = api.get("/api/mcp/activity", params={"app": "g2"}).json()
    assert [i["id"] for i in r["items"]] == ["other-app"]
    assert [i["id"] for i in api.get("/api/mcp/activity", params={"kind": "preview"}).json()["items"]] == ["preview"]
    hit = api.get("/api/mcp/activity", params={"client_group_id": "cg1"}).json()["items"]
    assert hit[0]["clients"] == [{"id": "cg1", "name": "Acme"}]
    assert hit[0]["credits"] == {"total": 0.5}
    assert hit[0]["refusal_reason"] is None

    ids = [i["id"] for i in api.get("/api/mcp/activity").json()["items"]]
    assert "old" not in ids and "bobs" not in ids  # default 7 days, own rows only

    seen, cursor = [], None
    while True:
        page = api.get("/api/mcp/activity", params={"limit": 3, **({"cursor": cursor} if cursor else {})}).json()
        assert len(page["items"]) <= 3
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ids and len(set(seen)) == len(seen)

    wide = api.get("/api/mcp/activity", params={"from": (_ago(days=40)).date().isoformat()}).json()["items"]
    assert "old" in [i["id"] for i in wide]
    assert api.get("/api/mcp/activity", params={"from": "nope"}).status_code == 400
    assert api.get("/api/mcp/activity", params={"cursor": "###"}).status_code == 400


@pytest.mark.asyncio
async def test_cursor_ties_are_stable_and_to_is_inclusive(api):
    same = _ago(hours=2)
    await seed(api.db, *[row(f"t{i}", same) for i in range(4)])
    first = api.get("/api/mcp/activity", params={"limit": 2}).json()
    second = api.get("/api/mcp/activity", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    got = [i["id"] for i in first["items"] + second["items"]]
    assert sorted(got) == ["t0", "t1", "t2", "t3"] and second["next_cursor"] is None
    await seed(api.db, row("late", datetime(2026, 11, 10, 23, 59, 59)))
    day = api.get("/api/mcp/activity", params={"from": "2026-11-10", "to": "2026-11-10"}).json()
    assert [i["id"] for i in day["items"]] == ["late"]


@pytest.mark.asyncio
async def test_totals_first_page_only(api):
    await seed(
        api.db,
        row("a", _ago(hours=1), credits=0.5), row("b", _ago(hours=2), credits=1.25), row("c", _ago(hours=3), credits=0.25),
        row("rl", _ago(hours=4), kind="refused", credits=0, count=7),
    )
    first = api.get("/api/mcp/activity", params={"limit": 2}).json()
    assert first["totals"] == {"requests": 10, "credits": 2.0}  # whole filter: 3 calls + 7 rate-limited
    second = api.get("/api/mcp/activity", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert second["totals"] is None
    only_live = api.get("/api/mcp/activity", params={"kind": "live"}).json()
    assert only_live["totals"] == {"requests": 3, "credits": 2.0}


@pytest.mark.asyncio
async def test_detail_owner_only_404(api):
    await seed(api.db, row("mine", _ago(hours=1), tool="get_alerts", kind="refused", refusal_reason="view_only"),
               row("bobs", _ago(hours=1), user=BOB))
    assert api.get("/api/mcp/activity").json()["items"][0]["refusal_reason"] == "view_only"
    ok = api.get("/api/mcp/activity/mine")
    assert ok.status_code == 200
    body = ok.json()
    assert body["args"] == {"secret_arg": "x"} and body["result_excerpt"] == "EXCERPT"
    assert "user_id" not in body and "_id" not in body
    assert body["tool_label"] == "Checked alerts"
    assert api.get("/api/mcp/activity/bobs").status_code == 404
    assert api.get("/api/mcp/activity/nope").status_code == 404


@pytest.mark.asyncio
async def test_csv_columns_and_no_args(api):
    await seed(api.db, row("a", _ago(hours=1), client_names=["Acme", "Beta"], client_group_ids=["1", "2"]),
               row("b", _ago(hours=2), summary="=HYPERLINK(1)"), row("x", _ago(hours=1), user=BOB))
    r = api.get("/api/mcp/activity.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="birdy-ai-activity.csv"' in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["time", "app", "kind", "summary", "client", "result", "credits"]
    assert len(rows) == 3
    assert rows[1][4] == "Acme; Beta" and rows[1][5] == "ok" and rows[1][6] == "0.5"
    assert rows[2][3] == "'=HYPERLINK(1)"
    assert "secret_arg" not in r.text and "EXCERPT" not in r.text


@pytest.mark.asyncio
async def test_revoked_app_still_listed(api):
    now = _ago(hours=1)
    await api.db["oauth_grants"].insert_many([
        {"id": "g1", "user_id": ALICE, "client_name": "Claude", "revoked": True},
        {"id": "g2", "user_id": ALICE, "client_name": "Cursor"},
    ])
    await api.db["mcp_tokens"].insert_many([
        {"id": "m1", "user_id": ALICE, "kind": "manual", "name": "Laptop", "revoked": True},
        {"id": "m2", "user_id": ALICE, "kind": "manual", "name": "Desk"},
    ])
    await seed(api.db, row("r1", now, grant="g1"), row("r2", now, grant="g2", app_name="Cursor"),
               row("r3", now, grant="m1", app_name="Laptop"), row("r4", now, grant="m2", app_name="Desk"))
    items = {i["id"]: i for i in api.get("/api/mcp/activity").json()["items"]}
    assert [items[k]["revoked"] for k in ("r1", "r2", "r3", "r4")] == [True, False, True, False]
    assert items["r1"]["app_name"] == "Claude"
    apps = {a["grant_id"]: a for a in api.get("/api/mcp/activity/apps").json()}
    assert apps["g1"] == {"grant_id": "g1", "app_name": "Claude", "app_kind": "claude", "revoked": True}
    assert apps["g2"]["revoked"] is False and apps["m1"]["revoked"] is True and len(apps) == 4
    assert api.get("/api/mcp/activity/r1").json()["revoked"] is True


@pytest.mark.asyncio
async def test_confirmed_after_reverse_lookup(api):
    await seed(api.db, row("p1", _ago(hours=2), kind="preview"),
               row("l1", _ago(hours=1), confirmed_after="p1"), row("l2", _ago(hours=1), confirmed_after="p0"))
    items = api.get("/api/mcp/activity", params={"confirmed_after": "p1"}).json()["items"]
    assert [i["id"] for i in items] == ["l1"]


@pytest.mark.asyncio
async def test_other_users_never_leak_into_totals_or_apps(api):
    await seed(api.db, row("mine", _ago(hours=1), credits=0.5),
               row("b1", _ago(hours=1), user=BOB, grant="gb", app_name="BobApp", credits=3),
               row("b2", _ago(hours=2), user=BOB, grant="gb", app_name="BobApp", kind="refused", count=9))
    first = api.get("/api/mcp/activity").json()
    assert first["totals"] == {"requests": 1, "credits": 0.5}
    apps = api.get("/api/mcp/activity/apps").json()
    assert [a["grant_id"] for a in apps] == ["g1"]


@pytest.mark.asyncio
async def test_apps_newest_name_wins_one_entry_per_grant(api):
    await seed(api.db, row("o", _ago(days=2), app_name="Old name"), row("n", _ago(hours=1), app_name="New name"),
               row("z", _ago(hours=3), grant="g2", app_name="Cursor"))
    apps = api.get("/api/mcp/activity/apps").json()
    assert [(a["grant_id"], a["app_name"]) for a in apps] == [("g1", "New name"), ("g2", "Cursor")]
