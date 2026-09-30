"""
tests/test_scheduled_cron_e2e.py
--------------------------------
End-to-end test of Birdy's scheduled jobs through the real cron HTTP endpoints:
cron request -> job -> suggestion pass -> Mongo -> Slack message.
In-memory Mongo, Slack API intercepted, template composer (no LLM).
"""
import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import core.mongo_client
from mongomock_motor import AsyncMongoMockClient
from core.database import DB_NAME
from core.crypto import encrypt
import routers.cron as cron
import ai.suggestions.orchestrator as orch

CRON_SECRET = "test-cron-secret"
AUTH = {"Authorization": f"Bearer {CRON_SECRET}"}


def _ad(i, name, spend, results):
    return {"id": i, "name": name, "status": "ACTIVE", "spend": spend, "results": results,
            "clicks": 50, "impressions": 5000, "reach": 3000}

BAD = [_ad("ad_zero", "Zero Lead Ad", 312, 0), _ad("ad_exp", "Expensive Ad", 96, 2),
       _ad("ad_g1", "Good 1", 100, 10), _ad("ad_g2", "Good 2", 120, 10)]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", CRON_SECRET)
    client = AsyncMongoMockClient()
    core.mongo_client._client = client
    db = client[DB_NAME]

    async def no_llm(user_id, db):
        return None
    monkeypatch.setattr(orch, "get_composer_provider", no_llm)

    posts, updates = [], []
    from slack_sdk.web.async_client import AsyncWebClient

    async def fake_post(self, **kw):
        posts.append({"token": self.token, **kw})
        return {"ok": True, "ts": f"1700000000.{len(posts):06d}"}

    async def fake_update(self, **kw):
        updates.append(kw)
        return {"ok": True}
    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    monkeypatch.setattr(AsyncWebClient, "chat_update", fake_update)

    app = FastAPI()
    app.include_router(cron.router)
    yield TestClient(app), db, client, posts, updates
    core.mongo_client._client = None


async def _seed(db, *, slack=True, last7=BAD, last30=None, status=None, user="agency@x.com", gid="g1"):
    u = {"user_id": user, "email": user}
    if slack:
        u["integrations"] = {"slack_bot": {"bot_token_encrypted": encrypt("xoxb-test"),
                                           "notify_channel_id": "C123"}}
    if not await db["users"].find_one({"user_id": user}):
        await db["users"].insert_one(u)
    g = {"id": gid, "user_id": user, "name": f"Client {gid}", "ad_account_currency": "GBP",
         "facebook_cache": {}}
    if last7: g["facebook_cache"]["last_7d"] = {"ads": last7}
    if last30: g["facebook_cache"]["last_30d"] = {"ads": last30}
    if status: g["client_status"] = status
    await db["client_groups"].insert_one(g)


def test_cron_rejects_bad_auth(env):
    tc, *_ = env
    for path in ("suggestions-weekly", "suggestions-monthly", "health-weekly"):
        assert tc.get(f"/api/cron/{path}").status_code in (401, 403)
        assert tc.get(f"/api/cron/{path}", headers={"Authorization": "Bearer nope"}).status_code in (401, 403)


async def test_weekly_posts_to_slack_once(env):
    tc, db, _, posts, updates = env
    await _seed(db)
    r = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()
    res = r["result"]
    assert res["users"] == 1 and res["analyzed"] == 1 and res["created"] >= 1, r
    sugs = await db["ai_suggestions"].find({"user_id": "agency@x.com"}).to_list(None)
    assert len(sugs) == res["created"]
    assert len(posts) == res["created"]
    assert all(p["channel"] == "C123" and p["token"] == "xoxb-test" and p["blocks"] for p in posts)
    assert all(s.get("slack_ts") and s.get("slack_channel") == "C123" for s in sugs), sugs
    kinds = {a["kind"] for a in await db["ai_activity"].find({}).to_list(None)}
    assert {"analysis_pass", "suggestion_created"} <= kinds, kinds

    # second run in the same week must not re-post
    r2 = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    assert r2["created"] == 0, r2
    assert len(posts) == res["created"], "duplicate Slack post on re-run"


async def test_monthly_uses_30d_window(env):
    tc, db, _, posts, _ = env
    await _seed(db, last7=None, last30=BAD)
    res = tc.get("/api/cron/suggestions-monthly", headers=AUTH).json()["result"]
    assert res["analyzed"] == 1 and res["created"] >= 1, res
    assert len(posts) == res["created"]
    # weekly has no 7d data → nothing analyzed
    res_w = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    assert res_w["analyzed"] == 0, res_w


async def test_no_slack_still_creates_suggestions(env):
    tc, db, _, posts, _ = env
    await _seed(db, slack=False)
    res = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    assert res["created"] >= 1 and posts == [], (res, posts)


async def test_inactive_client_skipped(env):
    tc, db, _, posts, _ = env
    await _seed(db, status="Inactive")
    res = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    assert res["clients"] == 0 and posts == [], res


async def test_healthy_ads_no_messages(env):
    tc, db, _, posts, _ = env
    good = [_ad(f"a{i}", f"Ad {i}", 100, 10) for i in range(4)]
    await _seed(db, last7=good)
    res = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    assert res["analyzed"] == 1 and res["created"] == 0 and posts == [], res


async def test_slack_failure_does_not_break_pass(env, monkeypatch):
    tc, db, _, posts, _ = env
    from slack_sdk.web.async_client import AsyncWebClient
    async def boom(self, **kw): raise RuntimeError("channel_not_found")
    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", boom)
    await _seed(db)
    r = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()
    assert r["ok"] and r["result"]["created"] >= 1, r
    assert await db["ai_suggestions"].count_documents({}) == r["result"]["created"]


async def test_one_user_failing_does_not_stop_others(env, monkeypatch):
    tc, db, _, posts, _ = env
    await _seed(db, user="a@x.com", gid="g1")
    await _seed(db, user="b@x.com", gid="g2")
    real = orch.run_pass_for_user
    async def flaky(db_, user_id, window, **kw):
        if user_id == "a@x.com": raise RuntimeError("boom")
        return await real(db_, user_id, window, **kw)
    monkeypatch.setattr(orch, "run_pass_for_user", flaky)
    res = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    assert res["users"] == 1 and res["total_users"] == 2 and res["created"] >= 1, res


async def test_health_weekly_runs(env):
    tc, db, *_ = env
    await _seed(db)
    r = tc.get("/api/cron/health-weekly", headers=AUTH).json()
    assert r["ok"] and "error" not in (r["result"] or {}), r


import types as _t

class _FakeProvider:
    model = "fake-model"
    def __init__(self, content=None, exc=None): self.content, self.exc, self.calls = content, exc, 0
    async def chat_completion(self, **kw):
        self.calls += 1
        if self.exc: raise self.exc
        return _t.SimpleNamespace(content=self.content, usage=_t.SimpleNamespace(input_tokens=100, output_tokens=40))


@pytest.mark.parametrize("content,exc,expect", [
    ('```json\n{"title":"Pause Zero Lead Ad","description":"Spent £312, no leads."}\n```', None, "llm"),
    ("Sorry, I can't help with that.", None, "template"),
    ('{"title":"only title"}', None, "template"),
    (None, RuntimeError("429 rate limited"), "template"),
])
async def test_llm_composer_paths_in_scheduled_pass(env, monkeypatch, content, exc, expect):
    tc, db, _, posts, _ = env
    prov = _FakeProvider(content, exc)
    async def with_llm(user_id, db): return prov
    monkeypatch.setattr(orch, "get_composer_provider", with_llm)
    billed = []
    import credits
    async def rec(db, user_id, **kw): billed.append(kw)
    monkeypatch.setattr(credits, "record_usage", rec)
    async def not_blocked(db, user_id): return False
    monkeypatch.setattr(credits, "is_blocked", not_blocked)
    await _seed(db)
    res = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    sugs = await db["ai_suggestions"].find({}).to_list(None)
    assert res["created"] >= 1 and len(posts) == res["created"]
    assert {s.get("composer") for s in sugs} == {expect}, [s.get("composer") for s in sugs]
    if expect == "llm":
        assert posts[0]["text"].startswith("Birdy suggestion: Pause Zero Lead Ad"), posts[0]["text"]
    # billing: tokens billed whenever the model actually answered
    if exc is None:
        assert billed and billed[0]["source"] == "cron" and billed[0]["feature"] == "suggestions", billed
    else:
        assert not billed


async def test_out_of_credits_user_gets_template_no_llm(env, monkeypatch):
    tc, db, *_ = env
    prov = _FakeProvider('{"title":"x","description":"y"}')
    async def with_llm(user_id, db): return prov
    monkeypatch.setattr(orch, "get_composer_provider", with_llm)
    import credits
    async def blocked(db, user_id): return True
    monkeypatch.setattr(credits, "is_blocked", blocked)
    await _seed(db)
    res = tc.get("/api/cron/suggestions-weekly", headers=AUTH).json()["result"]
    assert res["created"] >= 1 and prov.calls == 0
