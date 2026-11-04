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


async def test_delete_blobs_http_shape(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(ug.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    urls = [f"https://x.public.blob.vercel-storage.com/{i}.png" for i in range(101)]
    await ug._delete_blobs(urls)
    assert len(seen) == 2
    r = seen[0]
    assert r.method == "POST" and str(r.url) == "https://blob.vercel-storage.com/delete"
    assert r.headers["authorization"] == "Bearer tok"
    assert r.headers["x-api-version"] == "13"
    assert r.headers["content-type"] == "application/json"
    assert json.loads(r.content) == {"urls": urls[:100]}
    assert json.loads(seen[1].content) == {"urls": urls[100:]}


async def test_cleanup_skips_non_blob_urls(mock_db, monkeypatch):
    calls = []

    async def fake_delete(urls):
        calls.append(urls)

    monkeypatch.setattr(ug, "_delete_blobs", fake_delete)
    await _seed(mock_db, "cdn", "archived", NOW - timedelta(days=40),
                {"thumbnail_url": "https://scontent.xx.fbcdn.net/t.jpg"})
    assert await ug.cleanup_archived_media(mock_db, NOW) == 1
    assert calls == []
    assert (await mock_db[TESTS].find_one({"id": "cdn"}))["creative"]["media"]["deleted_at"]


async def test_failing_chunk_does_not_block_next(mock_db, monkeypatch):
    monkeypatch.setattr(ug, "DELETE_BATCH", 1)
    calls = []

    async def flaky(urls):
        calls.append(urls)
        if "bad" in urls[0]:
            raise RuntimeError("boom")

    monkeypatch.setattr(ug, "_delete_blobs", flaky)
    for tid in ("bad", "good"):
        await _seed(mock_db, tid, "archived", NOW - timedelta(days=40),
                    {"blob_url": f"https://x.public.blob.vercel-storage.com/{tid}.png"})
    assert await ug.cleanup_archived_media(mock_db, NOW) == 1
    assert (await mock_db[TESTS].find_one({"id": "bad"}))["creative"]["media"].get("deleted_at") is None
    assert (await mock_db[TESTS].find_one({"id": "good"}))["creative"]["media"]["deleted_at"]


def test_bool_size_rejected():
    with pytest.raises(ValueError):
        ug.issue_grant("u1", "image/png", True, NOW)


@pytest.mark.parametrize("head,kind", [
    (b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00", "video"),
    (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video"),
    (b"\x00\x00\x00\x08wide\x00\x00\x00\x00mdat", "video"),
    (b"\x00\x00\x00\x6cmoov\x00\x00\x00\x00mvhd", "video"),
    (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00", "image"),
    (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image"),
    (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", None),
    (b"<html>not media</html>", None),
    (b"", None),
])
def test_media_kind_comes_from_the_bytes(head, kind):
    assert ug.media_kind_of(head) == kind


async def test_read_media_kind_asks_for_only_the_start_and_ignores_the_name(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(206, content=b"\xff\xd8\xff\xe0" + b"\x00" * 12)

    real = httpx.AsyncClient
    monkeypatch.setattr(ug.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert await ug.read_media_kind("https://x.public.blob.vercel-storage.com/renamed.mp4") == "image"
    assert seen[0].method == "GET" and seen[0].headers["range"] == "bytes=0-15"


async def test_read_media_kind_doesnt_follow_redirects_and_raises_when_it_cant_read(monkeypatch):
    import httpx

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

    real = httpx.AsyncClient
    monkeypatch.setattr(ug.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(httpx.HTTPError):
        await ug.read_media_kind("https://x.public.blob.vercel-storage.com/a.png")
    assert len(calls) == 1

