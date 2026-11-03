import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from services.creatives import upload_grant as ug
from services.creatives.store import TESTS

SECRET = "test-secret"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CREATIVE_UPLOAD_SECRET", SECRET)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "tok")


def _decode(grant):
    payload, sig = grant.split(".")
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return payload, sig, json.loads(raw)


def test_grant_round_trip_verifies_with_same_secret():
    grant = ug.issue_grant("u1", "image/png", 5 * MB, NOW)
    payload, sig, data = _decode(grant)
    assert data == {"u": "u1", "m": "image/png", "s": 5 * MB, "exp": int((NOW + timedelta(minutes=10)).timestamp())}
    assert ug.verify_grant(grant, SECRET, NOW) == data
    assert ug.verify_grant(grant, "other", NOW) is None
    assert ug.verify_grant(grant, SECRET, NOW + timedelta(minutes=11)) is None


def test_tampered_payload_fails():
    grant = ug.issue_grant("u1", "image/png", 5 * MB, NOW)
    _, sig, data = _decode(grant)
    data["u"] = "attacker"
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    assert ug.verify_grant(f"{forged}.{sig}", SECRET, NOW) is None
    assert ug.verify_grant("garbage", SECRET, NOW) is None


def test_limits():
    with pytest.raises(ValueError):
        ug.issue_grant("u1", "image/png", 31 * MB, NOW)
    with pytest.raises(ValueError):
        ug.issue_grant("u1", "video/mp4", int(1.1 * 1024 * MB), NOW)
    with pytest.raises(ValueError):
        ug.issue_grant("u1", "image/gif", 1, NOW)
    with pytest.raises(ValueError):
        ug.issue_grant("u1", "image/png", 0, NOW)
    assert ug.issue_grant("u1", "video/quicktime", 1024 * MB, NOW)


async def _seed(db, tid, status, archived_at, media):
    await db[TESTS].insert_one({
        "id": tid, "user_id": "u1", "status": status, "archived_at": archived_at,
        "creative": {"media": media},
    })


async def test_cleanup_only_old_archived_and_not_repeated(mock_db, monkeypatch):
    calls = []

    async def fake_delete(urls):
        calls.append(sorted(urls))

    monkeypatch.setattr(ug, "_delete_blobs", fake_delete)
    await _seed(mock_db, "old", "archived", NOW - timedelta(days=31),
                {"blob_url": "https://x.public.blob.vercel-storage.com/a.mp4",
                 "thumbnail_url": "https://x.public.blob.vercel-storage.com/a.jpg"})
    await _seed(mock_db, "edge", "archived", NOW - timedelta(days=30),
                {"blob_url": "https://x.public.blob.vercel-storage.com/e.png"})
    await _seed(mock_db, "new", "archived", NOW - timedelta(days=29),
                {"blob_url": "https://x.public.blob.vercel-storage.com/n.png"})
    await _seed(mock_db, "live", "live", None,
                {"blob_url": "https://x.public.blob.vercel-storage.com/l.png"})
    await _seed(mock_db, "done", "archived", NOW - timedelta(days=90),
                {"blob_url": "https://x.public.blob.vercel-storage.com/d.png", "deleted_at": NOW})
    await _seed(mock_db, "nomedia", "archived", NOW - timedelta(days=90), None)

    assert await ug.cleanup_archived_media(mock_db, NOW) == 2
    assert calls == [[
        "https://x.public.blob.vercel-storage.com/a.jpg",
        "https://x.public.blob.vercel-storage.com/a.mp4",
        "https://x.public.blob.vercel-storage.com/e.png",
    ]]
    old = await mock_db[TESTS].find_one({"id": "old"})
    assert old["creative"]["media"]["deleted_at"] is not None
    assert (await mock_db[TESTS].find_one({"id": "new"}))["creative"]["media"].get("deleted_at") is None
    assert await ug.cleanup_archived_media(mock_db, NOW) == 0
    assert len(calls) == 1


async def test_cleanup_failure_leaves_deleted_at_unset(mock_db, monkeypatch):
    async def boom(urls):
        raise RuntimeError("blob down")

    monkeypatch.setattr(ug, "_delete_blobs", boom)
    await _seed(mock_db, "old", "archived", NOW - timedelta(days=31),
                {"blob_url": "https://x.public.blob.vercel-storage.com/a.mp4"})
    assert await ug.cleanup_archived_media(mock_db, NOW) == 0
    assert (await mock_db[TESTS].find_one({"id": "old"}))["creative"]["media"].get("deleted_at") is None
