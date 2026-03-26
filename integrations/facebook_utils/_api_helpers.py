import asyncio
import random
import logging

logger = logging.getLogger(__name__)


async def api_get_with_backoff(client, url, params=None, max_retries=4):
    """
    GET with exponential backoff on Meta rate-limit responses (code 17 / HTTP 429).
    Returns (response_ok: bool, data: dict | None).
    """
    for attempt in range(max_retries):
        try:
            if params is not None:
                resp = await client.get(url, params=params)
            else:
                resp = await client.get(url)

            if resp.status_code == 200:
                return True, resp.json()

            body = resp.text
            is_rate_limit = resp.status_code == 429 or (
                resp.status_code == 400 and ('"code":17' in body or '"code": 17' in body)
            )

            if is_rate_limit and attempt < max_retries - 1:
                wait = (15 * (2 ** attempt)) + random.uniform(0, 3)
                logger.warning(
                    f"⏳ Rate limited (attempt {attempt+1}/{max_retries}). "
                    f"Waiting {wait:.0f}s before retry..."
                )
                await asyncio.sleep(wait)
                continue

            logger.error(f"❌ API error {resp.status_code}: {body[:300]}")
            return False, None

        except Exception as e:
            logger.error(f"❌ Request error: {e}")
            return False, None

    return False, None
