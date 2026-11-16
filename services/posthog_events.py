"""Server-side PostHog events (fire-and-forget).

capture() never blocks or raises: it schedules one POST to {POSTHOG_HOST}/i/v0/e/ on the running loop and
returns. Skipped without POSTHOG_API_KEY or without a running loop. distinct_id is the Birdy user_id.
Callers pass counts and names only: no arguments, message text or client names.
"""

import asyncio
import logging

import httpx

from core import config

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 2.0
_client: httpx.AsyncClient | None = None
_tasks: set = set()  # strong refs so in-flight sends aren't garbage-collected


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    return _client


async def _send(payload: dict) -> None:
    try:
        await _get_client().post(f"{config.POSTHOG_HOST}/i/v0/e/", json=payload)
    except Exception as exc:
        logger.warning("PostHog capture failed for %s: %r", payload.get("event"), exc)


def capture(user_id: str, event: str, properties: dict) -> None:
    try:
        if not config.POSTHOG_API_KEY or not user_id:
            return
        loop = asyncio.get_running_loop()
        payload = {
            "api_key": config.POSTHOG_API_KEY,
            "event": event,
            "distinct_id": user_id,
            "properties": {**properties, "$lib": "birdy-backend"},
        }
        task = loop.create_task(_send(payload))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as exc:  # no running loop, or anything else: analytics never costs the caller
        logger.debug("PostHog capture skipped for %s: %r", event, exc)
