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
    if not isinstance(size, int) or size <= 0:
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
                headers={"Authorization": f"Bearer {token}"},
                json={"urls": urls[i:i + DELETE_BATCH]},
            )
            r.raise_for_status()


def _blob_urls(media: dict) -> list[str]:
    return [u for u in (media.get("blob_url"), media.get("thumbnail_url")) if u]


async def cleanup_archived_media(db, now_utc: datetime) -> int:
    """Delete Blob objects of tests archived >= 30 days ago; returns tests cleaned."""
    cutoff = _utc(now_utc) - RETENTION
    tests = await db[TESTS].find({
        "status": ARCHIVED,
        "archived_at": {"$lte": cutoff},
        "creative.media.deleted_at": {"$exists": False},
        "creative.media": {"$exists": True, "$ne": None},
    }).to_list(None)
    due = [t for t in tests if not ((t.get("creative") or {}).get("media") or {}).get("deleted_at")]
    urls = [u for t in due for u in _blob_urls(t["creative"]["media"])]
    try:
        if urls:
            await _delete_blobs(urls)  # one batched call set for every due test
    except Exception as e:  # nothing marked: retried on the next daily tick
        logger.warning("creative media cleanup failed: %s", e)
        return 0
    for test in due:
        await db[TESTS].update_one(
            {"id": test["id"], "user_id": test["user_id"]},
            {"$set": {"creative.media.deleted_at": _utc(now_utc)}},
        )
    return len(due)
