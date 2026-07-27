"""
Unit tests for services/dashboard_service reversal logic.

No DB or live Meta: a tiny in-memory Mongo fake plus a fake Meta client are
injected by monkeypatching the module-level helpers. Runs via `pytest` with no
async plugin required (each test drives its own event loop with asyncio.run).
"""

import asyncio

from services import dashboard_service as svc


# ── Fakes ────────────────────────────────────────────────────────────────────

def _match(doc, query):
    for k, v in query.items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_a, **_k):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, _n=None):
        return list(self._docs)


class FakeCollection:
    def __init__(self):
        self.docs = []
        self._id = 0

    async def find_one(self, query):
        return next((d for d in self.docs if _match(d, query)), None)

    def find(self, query):
        return FakeCursor([d for d in self.docs if _match(d, query)])

    async def insert_one(self, doc):
        self._id += 1
        doc["_id"] = self._id
        self.docs.append(doc)
        return type("R", (), {"inserted_id": self._id})()

    async def update_one(self, query, update):
        for d in self.docs:
            if _match(d, query):
                d.update(update.get("$set", {}))
                break

    async def create_index(self, *_a, **_k):
        return None


class FakeDB:
    def __init__(self):
        self._c = {}
        self.client = object()

    def __getitem__(self, name):
        return self._c.setdefault(name, FakeCollection())


class FakeMeta:
    """In-memory Meta: ad_id -> configured status, with an optional fail set."""

    def __init__(self, statuses, fail=()):
        self.status = dict(statuses)
        self.fail = set(fail)
        self.set_calls = []

    async def get_ad_status(self, ad_id, _token):
        return self.status.get(ad_id)

    async def set_ad_status(self, ad_id, status, _token):
        self.set_calls.append((ad_id, status))
        if ad_id in self.fail:
            return False, "boom"
        self.status[ad_id] = status
        return True, None


def _wire(monkeypatch, meta):
    monkeypatch.setattr(svc, "get_ad_status", meta.get_ad_status)
    monkeypatch.setattr(svc, "set_ad_status", meta.set_ad_status)

    async def _token(_user, _client):
        return {"access_token": "T"}

    monkeypatch.setattr(svc, "get_facebook_token", _token)


def _seed(db, ad_ids):
    db["dashboard_suggestions"].docs.append({
        "suggestion_id": "s1",
        "user_id": "u",
        "status": "open",
        "target_ad_ids": ad_ids,
        "title": "Pause underperformers",
        "client": "Acme",
        "severity": "HIGH",
    })


# ── Tests ────────────────────────────────────────────────────────────────────

def test_apply_pauses_and_records_prior_status(monkeypatch):
    db = FakeDB()
    _seed(db, ["a1", "a2"])
    meta = FakeMeta({"a1": "ACTIVE", "a2": "ACTIVE"})
    _wire(monkeypatch, meta)

    res = asyncio.run(svc.apply_suggestion(db, "u", "s1"))

    assert res["ok"] and sorted(res["succeeded"]) == ["a1", "a2"] and res["failed"] == []
    assert meta.status == {"a1": "PAUSED", "a2": "PAUSED"}
    rec = db["suggestion_actions"].docs[0]
    assert rec["state"] == "applied"
    assert {t["ad_id"]: t["prior_status"] for t in rec["targets"]} == {"a1": "ACTIVE", "a2": "ACTIVE"}
    # suggestion flipped to applied
    assert db["dashboard_suggestions"].docs[0]["status"] == "applied"


def test_apply_is_idempotent(monkeypatch):
    db = FakeDB()
    _seed(db, ["a1"])
    meta = FakeMeta({"a1": "ACTIVE"})
    _wire(monkeypatch, meta)

    asyncio.run(svc.apply_suggestion(db, "u", "s1"))
    calls_after_first = len(meta.set_calls)
    res2 = asyncio.run(svc.apply_suggestion(db, "u", "s1"))

    assert res2.get("idempotent") is True
    assert len(meta.set_calls) == calls_after_first  # no second pause
    assert len(db["suggestion_actions"].docs) == 1  # no duplicate record


def test_undo_restores_prior_status(monkeypatch):
    db = FakeDB()
    _seed(db, ["a1", "a2"])
    meta = FakeMeta({"a1": "ACTIVE", "a2": "ACTIVE"})
    _wire(monkeypatch, meta)

    asyncio.run(svc.apply_suggestion(db, "u", "s1"))
    res = asyncio.run(svc.undo_suggestion(db, "u", "s1"))

    assert res["ok"] and sorted(res["restored"]) == ["a1", "a2"]
    assert meta.status == {"a1": "ACTIVE", "a2": "ACTIVE"}
    assert db["suggestion_actions"].docs[0]["state"] == "undone"
    assert db["dashboard_suggestions"].docs[0]["status"] == "open"


def test_undo_is_idempotent(monkeypatch):
    db = FakeDB()
    _seed(db, ["a1"])
    meta = FakeMeta({"a1": "ACTIVE"})
    _wire(monkeypatch, meta)

    asyncio.run(svc.apply_suggestion(db, "u", "s1"))
    asyncio.run(svc.undo_suggestion(db, "u", "s1"))
    res2 = asyncio.run(svc.undo_suggestion(db, "u", "s1"))

    assert res2["ok"] and res2["restored"] == [] and res2.get("idempotent") is True


def test_partial_failure_is_recoverable(monkeypatch):
    db = FakeDB()
    _seed(db, ["a1", "a2"])
    meta = FakeMeta({"a1": "ACTIVE", "a2": "ACTIVE"}, fail={"a2"})
    _wire(monkeypatch, meta)

    res = asyncio.run(svc.apply_suggestion(db, "u", "s1"))
    assert res["succeeded"] == ["a1"] and [f["ad_id"] for f in res["failed"]] == ["a2"]
    assert meta.status["a1"] == "PAUSED"  # a1 paused, a2 never changed

    # undo restores only the ad that was actually paused
    meta.set_calls.clear()
    res2 = asyncio.run(svc.undo_suggestion(db, "u", "s1"))
    assert res2["restored"] == ["a1"]
    assert meta.set_calls == [("a1", "ACTIVE")]


def test_apply_unknown_suggestion_raises(monkeypatch):
    db = FakeDB()
    meta = FakeMeta({})
    _wire(monkeypatch, meta)
    try:
        asyncio.run(svc.apply_suggestion(db, "u", "missing"))
        assert False, "expected SuggestionNotFound"
    except svc.SuggestionNotFound:
        pass
