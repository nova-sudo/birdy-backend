"""Task 19: 7-day outcomes, the agent activity feed and the monthly impact API."""

import contextlib
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import routers.agents as agents_router
from ai.agents import outcomes
from ai.agents.store import create_agent
from ai.suggestions import store as sstore
from dependencies import get_current_user
from main import app

U = "u1@example.com"
OTHER = "u2@example.com"


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    state = {"user": U}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def _group(db, gid="g1", user=U, currency="GBP", refreshed=datetime(2026, 12, 1)):
    await db["client_groups"].insert_one(
        {"id": gid, "user_id": user, "name": f"Client {gid}", "ad_account_currency": currency,
         "last_meta_refresh": refreshed})


async def _insights(db, ad_id, start: date, days: int, spend: float, leads: int,
                    gid="g1", adset_id="as1", campaign_id="c1"):
    for i in range(days):
        d = start + timedelta(days=i)
        await db["facebook_ad_insights"].insert_one({
            "user_id": U, "client_group_id": gid, "ad_id": ad_id, "adset_id": adset_id,
            "campaign_id": campaign_id, "date_start": d.isoformat(),
            "insight_data": {"spend": str(spend),
                             "actions": [{"action_type": "lead", "value": str(leads)}] if leads else []},
        })


async def _suggestion(db, sid, *, atype, targets, agent_id="ag1", gid="g1", status="applied",
                      decided_actor="birdy", applied_at=None, undone_at=None):
    doc = {"_id": sid, "id": sid, "user_id": U, "agent_id": agent_id, "rule_id": "r1",
           "client_group_id": gid, "client_name": f"Client {gid}", "title": f"Do {sid}",
           "action": {"type": atype, "targets": targets} if atype else None, "status": status}
    if status == "applied":
        doc.update(applied_at=applied_at or datetime.utcnow(), applied_targets=targets,
                   applied_action_type=atype, decided_by={"actor": decided_actor})
    if undone_at:
        doc["undone_at"] = undone_at
    await db[sstore.SUGGESTIONS].insert_one(doc)
    return doc


async def _row(db, sid, *, kind, actor, at: datetime, agent_id="ag1", gid="g1",
               atype=None, title=None, **kw):
    row = await sstore.log_activity(
        db, U, actor=actor, kind=kind, title=title or f"Do {sid}", client_name=f"Client {gid}",
        client_group_id=gid, ref_id=sid, agent_id=agent_id, rule_id="r1",
        applied_action_type=atype, **kw)
    await db[sstore.ACTIVITY].update_one({"_id": row["_id"]}, {"$set": {"created_at": at}})
    row["created_at"] = at
    return row


AD = lambda oid: {"object_id": oid, "object_type": "ad", "name": oid}  # noqa: E731


async def test_pause_outcome_account_cpl_before_after(mock_db):
    await _group(mock_db)
    # Before (Oct 3-9): a1 100/day + a2 50/day, 2 leads/day -> 1050 / 14 = 75.
    await _insights(mock_db, "a1", date(2026, 10, 3), 7, 100, 1)
    await _insights(mock_db, "a2", date(2026, 10, 3), 14, 50, 1)  # runs through Oct 16
    await _insights(mock_db, "a2", date(2026, 10, 17), 1, 50, 1)  # Oct 17
    await _suggestion(mock_db, "s1", atype="pause_ads", targets=[AD("a1")])
    row = await _row(mock_db, "s1", kind=sstore.KIND_ACTION_APPLIED, actor="birdy",
                     at=datetime(2026, 10, 10, 7, 0), atype="pause_ads")

    out = await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 20))
    assert out["metric"] == "account_cpl"
    assert out["before"] == pytest.approx(75.0)
    assert out["after"] == pytest.approx(50.0)  # Oct 11-17: a2 only, 50 per lead
    assert out["currency"] == "GBP"

    # Too early: the after-window (Oct 11-17) needs a day's buffer, so not before Oct 19.
    assert await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 16)) is None
    assert await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 18)) is None
    assert (await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 19)))["after"] == pytest.approx(50.0)

    # Not fresh: the client's Meta data was last refreshed before Oct 18 -> retried later.
    await mock_db["client_groups"].update_one({"id": "g1"}, {"$set": {"last_meta_refresh": datetime(2026, 10, 17, 23, 0)}})
    assert await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 20)) is None
    await mock_db["client_groups"].update_one({"id": "g1"}, {"$unset": {"last_meta_refresh": ""}})
    assert await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 20)) is None
    await mock_db["client_groups"].update_one({"id": "g1"}, {"$set": {"last_meta_refresh": datetime(2026, 10, 18, 4, 0)}})
    assert await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 20)) is not None


async def test_revive_outcome(mock_db):
    await _group(mock_db, currency="USD")
    await _insights(mock_db, "a3", date(2026, 10, 11), 7, 30, 0)
    await mock_db["facebook_ad_insights"].update_many(
        {"ad_id": "a3", "date_start": {"$in": ["2026-10-12", "2026-10-14", "2026-10-16"]}},
        {"$set": {"insight_data.actions": [{"action_type": "lead", "value": "1"}]}})
    await _suggestion(mock_db, "s2", atype="enable_ads", targets=[AD("a3")])
    row = await _row(mock_db, "s2", kind=sstore.KIND_ACTION_APPLIED, actor="user",
                     at=datetime(2026, 10, 10, 7, 0), atype="enable_ads")

    out = await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 20))
    assert out["metric"] == "ad_cpl"
    assert out["leads"] == 3
    assert out["after"] == pytest.approx(70.0)  # 210 / 3
    assert out["currency"] == "USD"


async def test_outcome_skips_insufficient_data(mock_db):
    await _group(mock_db)
    await _suggestion(mock_db, "s1", atype="pause_ads", targets=[AD("a1")])
    row = await _row(mock_db, "s1", kind=sstore.KIND_ACTION_APPLIED, actor="birdy",
                     at=datetime(2026, 10, 10, 7, 0), atype="pause_ads")
    # No insights at all.
    assert await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 20)) is None
    # Only the before window.
    await _insights(mock_db, "a1", date(2026, 10, 3), 7, 100, 1)
    assert await outcomes.compute_outcome(mock_db, row, today=date(2026, 10, 20)) is None
    # A revive with no spend afterwards.
    await _suggestion(mock_db, "s2", atype="enable_ads", targets=[AD("a9")])
    rev = await _row(mock_db, "s2", kind=sstore.KIND_ACTION_APPLIED, actor="birdy",
                     at=datetime(2026, 10, 10, 7, 0), atype="enable_ads")
    assert await outcomes.compute_outcome(mock_db, rev, today=date(2026, 10, 20)) is None
    # An acknowledgement (nothing written) never gets an outcome.
    ack = await _row(mock_db, "s3", kind=sstore.KIND_ACTION_APPLIED, actor="user",
                     at=datetime(2026, 10, 10, 7, 0))
    assert await outcomes.compute_outcome(mock_db, ack, today=date(2026, 10, 20)) is None

    # The daily job stores what it can and leaves the rest for a later day.
    await _insights(mock_db, "a1", date(2026, 10, 11), 7, 40, 1)
    res = await outcomes.run_outcomes_db(mock_db, now_utc=datetime(2026, 10, 20, 5, 0))
    assert res["measured"] == 1
    stored = await mock_db[sstore.ACTIVITY].find_one({"ref_id": "s1"})
    assert stored["outcome_7d"]["metric"] == "account_cpl"
    assert (await mock_db[sstore.ACTIVITY].find_one({"ref_id": "s2"})).get("outcome_7d") is None


async def test_impact_wasted_spend_math(mock_db, api):
    await _group(mock_db)
    agent = await create_agent(mock_db, U)
    aid = agent["_id"]
    # a1 averages 100/day before its Oct 10 pause; a2 averages 50/day before Oct 25.
    await _insights(mock_db, "a1", date(2026, 10, 3), 7, 100, 1)
    await _insights(mock_db, "a2", date(2026, 10, 18), 7, 50, 1)

    await _suggestion(mock_db, "s1", agent_id=aid, atype="pause_ads", targets=[AD("a1")])
    await _row(mock_db, "s1", agent_id=aid, kind=sstore.KIND_ACTION_APPLIED, actor="birdy",
               at=datetime(2026, 10, 10, 7, 0), atype="pause_ads")
    # Approved by the user on Oct 25, undone by the user on Oct 27.
    await _suggestion(mock_db, "s2", agent_id=aid, atype="pause_ads", targets=[AD("a2")],
                      status="open", undone_at=datetime(2026, 10, 27, 9, 0))
    await _row(mock_db, "s2", agent_id=aid, kind=sstore.KIND_ACTION_APPLIED, actor="user",
               at=datetime(2026, 10, 25, 7, 0), atype="pause_ads")
    await _row(mock_db, "s2", agent_id=aid, kind=sstore.KIND_ACTION_UNDONE, actor="user",
               at=datetime(2026, 10, 27, 9, 0))
    # A denial, and a flag acknowledgement that is not a decision.
    await _suggestion(mock_db, "s3", agent_id=aid, atype="pause_ads", targets=[AD("a3")], status="dismissed")
    await _row(mock_db, "s3", agent_id=aid, kind=sstore.KIND_SUGGESTION_DISMISSED, actor="user",
               at=datetime(2026, 10, 12, 7, 0), reason_code="still_learning")
    # The same item dismissed again from Slack: still one denial.
    await _row(mock_db, "s3", agent_id=aid, kind=sstore.KIND_SUGGESTION_DISMISSED, actor="user",
               at=datetime(2026, 10, 12, 7, 5))
    await _suggestion(mock_db, "s4", agent_id=aid, atype=None, targets=[])
    await _row(mock_db, "s4", agent_id=aid, kind=sstore.KIND_ACTION_APPLIED, actor="user",
               at=datetime(2026, 10, 13, 7, 0))
    # Last month: one auto action.
    await _suggestion(mock_db, "s5", agent_id=aid, atype="pause_ads", targets=[AD("a5")])
    await _row(mock_db, "s5", agent_id=aid, kind=sstore.KIND_ACTION_APPLIED, actor="birdy",
               at=datetime(2026, 9, 20, 7, 0), atype="pause_ads")

    imp = await outcomes.compute_impact(mock_db, agent, month="2026-10", now=datetime(2026, 11, 5))
    # a1: 100/day from Oct 10 07:00 to month end (Nov 1 00:00 London = UTC) = 21d17h;
    # a2: 50/day from Oct 25 07:00 until its undo on Oct 27 09:00 = 2d2h.
    assert imp["wasted_spend_stopped"] == pytest.approx(100 * (21 + 17 / 24) + 50 * (2 + 2 / 24), abs=0.01)
    assert imp["actions"] == 2 and imp["auto"] == 1
    # The approved-then-undone item is only undone_user (as in confidence.py); s3 is one denial.
    assert imp["approval_counts"] == {"approved": 0, "denied": 1, "undone_user": 1}
    assert imp["approval_rate"] == 0.0
    assert imp["vs_prev_month"]["actions"] == 1
    assert imp["currency"] == "GBP" and imp["month"] == "2026-10"

    # Mid-month, days paused stop at today.
    mid = await outcomes.compute_impact(mock_db, agent, month="2026-10", now=datetime(2026, 10, 20, 12, 0))
    assert mid["wasted_spend_stopped"] == pytest.approx(100 * (10 + 5 / 24), abs=0.01)  # s2 not yet

    r = api.get(f"/api/agents/{aid}/impact", params={"month": "2026-10"})
    assert r.status_code == 200, r.text
    assert set(r.json()) >= {"actions", "auto", "wasted_spend_stopped", "approval_rate", "vs_prev_month"}
    for bad in ("2026-13", "2026-1", "oct", "2026-10-01"):
        assert api.get(f"/api/agents/{aid}/impact", params={"month": bad}).status_code == 422, bad
    api.state["user"] = OTHER
    assert api.get(f"/api/agents/{aid}/impact", params={"month": "2026-10"}).status_code == 404


async def test_activity_can_undo_only_auto_under_24h(mock_db, api):
    await _group(mock_db)
    agent = await create_agent(mock_db, U)
    aid = agent["_id"]
    now = datetime.utcnow()
    cases = [  # sid, decided actor, age, undone
        ("fresh_auto", "birdy", timedelta(hours=1), False),
        ("old_auto", "birdy", timedelta(hours=25), False),
        ("fresh_user", "user", timedelta(hours=1), False),
        ("undone_auto", "birdy", timedelta(hours=2), True),
    ]
    for i, (sid, actor, age, undone) in enumerate(cases):
        at = now - age
        await _suggestion(mock_db, sid, agent_id=aid, atype="pause_ads", targets=[AD(sid)],
                          decided_actor=actor, applied_at=at,
                          status="open" if undone else "applied",
                          undone_at=now - timedelta(minutes=30) if undone else None)
        await _row(mock_db, sid, agent_id=aid, kind=sstore.KIND_ACTION_APPLIED, actor=actor,
                   at=at, atype="pause_ads")
    await _row(mock_db, "undone_auto", agent_id=aid, kind=sstore.KIND_ACTION_UNDONE, actor="user",
               at=now - timedelta(minutes=30), title="Undid: Do undone_auto")
    await _row(mock_db, "den", agent_id=aid, kind=sstore.KIND_SUGGESTION_DISMISSED, actor="user",
               at=now - timedelta(minutes=10), title="Dismissed: Do den",
               reason_code="client_wants_on", note="Client asked to keep it")
    # Not decisions, and another agent's row: never in the feed.
    await _row(mock_db, "c1", agent_id=aid, kind=sstore.KIND_SUGGESTION_CREATED, actor="birdy", at=now)
    await _row(mock_db, "x1", agent_id="other", kind=sstore.KIND_ACTION_APPLIED, actor="birdy",
               at=now, atype="pause_ads")
    await mock_db[sstore.ACTIVITY].update_one(
        {"ref_id": "old_auto"}, {"$set": {"outcome_7d": {"metric": "account_cpl", "before": 80.0, "after": 60.0}}})

    r = api.get(f"/api/agents/{aid}/activity")
    assert r.status_code == 200, r.text
    body = r.json()
    rows = {(x["suggestion_id"], x["decided"]): x for x in body["items"]}
    assert len(body["items"]) == 6
    assert rows[("fresh_auto", "auto")]["can_undo"] is True
    assert rows[("old_auto", "auto")]["can_undo"] is False
    assert rows[("fresh_user", "approved")]["can_undo"] is False
    assert rows[("undone_auto", "auto")]["can_undo"] is False
    assert rows[("undone_auto", "undone")]["title"] == "Do undone_auto"
    den = rows[("den", "denied")]
    assert den["reason_code"] == "client_wants_on" and den["note"] == "Client asked to keep it"
    assert den["title"] == "Do den" and den["client_name"] == "Client g1"
    assert rows[("old_auto", "auto")]["outcome_7d"]["after"] == 60.0
    assert rows[("fresh_auto", "auto")]["currency"] == "GBP"
    assert [x["at"] for x in body["items"]] == sorted((x["at"] for x in body["items"]), reverse=True)

    # Cursor pagination on `at`.
    p1 = api.get(f"/api/agents/{aid}/activity", params={"limit": 2}).json()
    assert len(p1["items"]) == 2 and p1["next_before"] == p1["items"][-1]["at"]
    p2 = api.get(f"/api/agents/{aid}/activity", params={"limit": 50, "before": p1["next_before"]}).json()
    assert len(p2["items"]) == 4 and p2["next_before"] is None
    assert api.get(f"/api/agents/{aid}/activity", params={"before": "yesterday"}).status_code == 422

    # The opaque cursor never skips rows that share a timestamp (fresh_auto / fresh_user).
    seen, cur = [], None
    for _ in range(10):
        params = {"limit": 1, **({"cursor": cur} if cur else {})}
        page = api.get(f"/api/agents/{aid}/activity", params=params).json()
        seen += [x["id"] for x in page["items"]]
        cur = page["next_cursor"]
        if not cur:
            break
    assert len(seen) == 6 and len(set(seen)) == 6
    assert api.get(f"/api/agents/{aid}/activity", params={"cursor": "%%%"}).status_code == 422

    api.state["user"] = OTHER
    assert api.get(f"/api/agents/{aid}/activity").status_code == 404


async def test_agent_activity_ttl_13_months(mock_db, mock_mongo_client):
    agent_row = await sstore.log_activity(mock_db, U, actor="birdy", kind="action_applied",
                                          title="t", agent_id="ag1")
    plain_row = await sstore.log_activity(mock_db, U, actor="birdy", kind="action_applied", title="t")
    assert agent_row["expires_at"] - agent_row["created_at"] >= timedelta(days=395)
    assert plain_row["expires_at"] - plain_row["created_at"] == timedelta(days=60)

    await sstore.create_suggestion_indexes(mock_mongo_client)
    info = await mock_db[sstore.ACTIVITY].index_information()
    ttl = [v for v in info.values() if v.get("key") == [("expires_at", 1)]]
    assert ttl and ttl[0].get("expireAfterSeconds") == 0
    # The feed and the outcomes job read agent rows by (agent_id, created_at).
    assert any(v.get("key") == [("agent_id", 1), ("created_at", -1)] for v in info.values())


def test_outcomes_cron_wired(monkeypatch):
    import json
    from pathlib import Path

    import jobs.agent_jobs as agent_jobs

    crons = json.loads((Path(__file__).resolve().parent.parent / "vercel.json").read_text())["crons"]
    assert {"path": "/api/cron/agents-outcomes", "schedule": "0 5 * * *"} in crons

    calls = []

    async def fake():
        calls.append(1)
        return {"checked": 0, "measured": 0, "errors": 0}

    monkeypatch.setattr(agent_jobs, "run_agents_outcomes", fake)
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    client = TestClient(app)
    assert client.get("/api/cron/agents-outcomes").status_code == 401
    r = client.get("/api/cron/agents-outcomes", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json()["result"]["measured"] == 0 and calls
