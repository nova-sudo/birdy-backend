"""Signed, short-lived upload grants for direct-to-Vercel-Blob creative media, plus Blob cleanup.

The auth cookie lives on the backend domain, so the Next upload route cannot see the user. The
backend signs a grant; the Next route verifies it (same secret) before issuing a Blob client token.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

import httpx

from services.creatives.store import ARCHIVED, TESTS

logger = logging.getLogger(__name__)

MB = 1024 * 1024
IMAGE_MIMES = ("image/jpeg", "image/png")
VIDEO_MIMES = ("video/mp4", "video/quicktime")
MAX_IMAGE_BYTES = 30 * MB
MAX_VIDEO_BYTES = 1024 * MB
GRANT_TTL = timedelta(minutes=10)
RETENTION = timedelta(days=30)
BLOB_DELETE_URL = "https://blob.vercel-storage.com/delete"
DELETE_BATCH = 100
BLOB_API_VERSION = "13"
PUBLIC_BLOB_HOST_SUFFIX = ".public.blob.vercel-storage.com"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _secret() -> str:
    secret = os.environ.get("CREATIVE_UPLOAD_SECRET")
    if not secret:
        raise RuntimeError("CREATIVE_UPLOAD_SECRET is not configured")
    return secret


def issue_grant(user_id: str, mime: str, size: int, now: datetime) -> str:
    if mime in IMAGE_MIMES:
        limit = MAX_IMAGE_BYTES
    elif mime in VIDEO_MIMES:
        limit = MAX_VIDEO_BYTES
    else:
        raise ValueError(f"Unsupported media type: {mime}")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("File size must be positive")
    if size > limit:
        raise ValueError(f"File too large: {mime} is limited to {limit // MB} MB")
    exp = int((_utc(now) + GRANT_TTL).timestamp())
    payload = _b64(json.dumps({"u": user_id, "m": mime, "s": size, "exp": exp},
                              separators=(",", ":")).encode())
    return f"{payload}.{_sign(_secret(), payload)}"


def verify_grant(grant: str, secret: str, now: datetime):
    """Return the grant dict, or None when malformed, tampered or expired (never raises)."""
    try:
        payload, sig = grant.split(".")
        if not hmac.compare_digest(_sign(secret, payload), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if int(data["exp"]) < _utc(now).timestamp():
            return None
        return data
    except Exception:
        return None


async def _delete_blobs(urls: list[str]) -> None:
    token = os.environ.get("BLOB_READ_WRITE_TOKEN")
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not configured")
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(urls), DELETE_BATCH):
            r = await client.post(
                BLOB_DELETE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-api-version": BLOB_API_VERSION,
                    "content-type": "application/json",
                },
                json={"urls": urls[i:i + DELETE_BATCH]},
            )
            r.raise_for_status()


def is_public_blob_url(url) -> bool:
    """https URL whose parsed hostname ends with the public Blob suffix (never a substring match)."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except (ValueError, AttributeError):
        return False
    return parsed.scheme == "https" and host.endswith(PUBLIC_BLOB_HOST_SUFFIX)


def owner_prefix(user_id: str) -> str:
    """Where a user's upload goes when it must be provable as theirs later. Only the backend can work
    it out for a user, so a URL under it is that user's. It doesn't show who that is."""
    return f"creatives/u-{_sign(_secret(), f'blob-owner:{user_id}')[:32]}/"


def _own_store_id() -> str | None:
    """The id in vercel_blob_rw_<store id>_<secret>. The public hostname is shared with every other
    Vercel customer's Blob store, so only this says a URL is on ours."""
    parts = (os.environ.get("BLOB_READ_WRITE_TOKEN") or "").split("_")
    return parts[3].lower() if len(parts) > 4 and parts[3] else None


def is_own_blob_url(url, user_id: str) -> bool:
    """A public URL on this app's own Blob store, under this user's owner_prefix, and nothing else."""
    store = _own_store_id()
    if not store or not is_public_blob_url(url):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if port is not None or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.hostname != f"{store}{PUBLIC_BLOB_HOST_SUFFIX}" or ".." in parsed.path or "%" in parsed.path:
        return False
    try:
        return parsed.path.startswith(f"/{owner_prefix(user_id)}")
    except RuntimeError:  # no secret: nothing can be proved
        return False


def media_limit(mime) -> int | None:
    if mime in IMAGE_MIMES:
        return MAX_IMAGE_BYTES
    if mime in VIDEO_MIMES:
        return MAX_VIDEO_BYTES
    return None


_MEDIA_URL_FIELDS = ("creative.media.blob_url", "creative.media.thumbnail_url",
                     "creative.blob_url", "creative.thumbnail_url")


def _blob_urls(media: dict) -> list[str]:
    return [u for u in (media.get("blob_url"), media.get("thumbnail_url")) if u and is_public_blob_url(u)]


async def _still_referenced(db, urls: set[str], cutoff: datetime) -> set[str]:
    """URLs a test still uses: one not archived (a scaled or duplicated test shares its
    source's upload), or one archived less than the retention period ago."""
    if not urls:
        return set()
    keep_tests = {"$or": [{"status": {"$ne": ARCHIVED}}, {"archived_at": {"$gt": cutoff}}]}
    uses = {"$or": [{f: {"$in": list(urls)}} for f in _MEDIA_URL_FIELDS]}
    found: set[str] = set()
    async for t in db[TESTS].find({"$and": [keep_tests, uses]}, {"creative": 1}):
        creative = t.get("creative") or {}
        media = creative.get("media") or {}
        for u in (media.get("blob_url"), media.get("thumbnail_url"),
                  creative.get("blob_url"), creative.get("thumbnail_url")):
            if u in urls:
                found.add(u)
    return found


async def cleanup_archived_media(db, now_utc: datetime) -> int:
    """Delete Blob objects of tests archived >= 30 days ago; returns tests cleaned.

    Tests are processed in chunks of at most DELETE_BATCH urls and marked per successful chunk,
    so one failing chunk never blocks the others (it is retried on the next daily tick).
    """
    cutoff = _utc(now_utc) - RETENTION
    tests = await db[TESTS].find({
        "status": ARCHIVED,
        "archived_at": {"$lte": cutoff},
        "creative.media.deleted_at": {"$exists": False},
        "creative.media": {"$exists": True, "$ne": None},
    }).to_list(None)

    candidates = {u for t in tests for u in _blob_urls((t.get("creative") or {}).get("media") or {})}
    in_use = await _still_referenced(db, candidates, cutoff)

    chunks, current, count = [], [], 0
    for test in tests:
        media = (test.get("creative") or {}).get("media") or {}
        if media.get("deleted_at"):
            continue
        urls = [u for u in _blob_urls(media) if u not in in_use]
        if current and count + len(urls) > DELETE_BATCH:
            chunks.append(current)
            current, count = [], 0
        current.append((test, urls))
        count += len(urls)
    if current:
        chunks.append(current)

    cleaned = 0
    for chunk in chunks:
        urls = [u for _, us in chunk for u in us]
        try:
            if urls:
                await _delete_blobs(urls)
        except Exception as e:
            logger.warning("creative media cleanup failed: %s", e)
            continue
        for test, _ in chunk:
            await db[TESTS].update_one(
                {"id": test["id"], "user_id": test["user_id"]},
                {"$set": {"creative.media.deleted_at": _utc(now_utc)}},
            )
            cleaned += 1
    return cleaned
