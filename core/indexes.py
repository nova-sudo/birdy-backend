"""
core/indexes.py
---------------
Every index the app relies on, in one place, behind one call.

This used to run inside main.py's lifespan, which meant ~40 createIndex
round-trips before a cold container could serve its first request. createIndex
is idempotent, but "the index already exists" is still a round-trip to find
out, and on Vercel a lifespan runs on every cold start — so the whole set was
re-confirmed constantly and nobody was waiting on the answer except the user.

It now runs where nothing is blocked on it:

  * `python -m scripts.ensure_indexes` — after a deploy that adds an index,
    or any time you want them applied now.
  * `GET /api/cron/ensure-indexes` — daily, so a newly declared index lands
    within a day even if the script is forgotten.
  * a local (non-Vercel) app start, where one process serves everything and
    the convenience is worth the few seconds.

Adding an index means adding its creator to INDEX_CREATORS below. Nothing else
picks them up by discovery.
"""

import logging

logger = logging.getLogger(__name__)


def _load_creators():
    """
    Import the creators lazily.

    They are scattered across integrations/, services/, ai/ and routers/, and
    several of those modules pull in heavy third-party clients at import time.
    Importing them at module scope would drag that cost into anything that
    merely mentions this module — including the request path, which is the
    thing this file exists to keep clear.
    """
    from ai.conversation_log import create_conversation_log_indexes
    from ai.session_store import create_ai_session_indexes
    from ai.suggestions.store import create_suggestion_indexes
    from credits import create_ai_usage_indexes
    from integrations.facebook_utils.facebook_ads import create_ad_insights_indexes
    from integrations.facebook_utils.facebook_adsets import create_adset_insights_indexes
    from integrations.facebook_utils.facebook_campaigns import create_campaign_insights_indexes
    from integrations.facebook_utils.facebook_leads import create_facebook_leads_indexes
    from routers.client_notes import create_note_indexes
    from services.call_logs_service import create_call_logs_indexes
    from services.mcp_token_service import create_mcp_tokens_indexes
    from services.slack_bot_service import create_slack_bot_indexes
    from services.slack_interaction_store import create_slack_ui_interaction_indexes
    from utils.cache_helpers import create_performance_indexes

    return [
        create_performance_indexes,
        create_facebook_leads_indexes,
        create_campaign_insights_indexes,
        create_adset_insights_indexes,
        create_ad_insights_indexes,
        create_call_logs_indexes,
        create_mcp_tokens_indexes,
        create_slack_bot_indexes,
        create_slack_ui_interaction_indexes,
        create_ai_session_indexes,
        create_suggestion_indexes,
        create_conversation_log_indexes,
        create_note_indexes,
        create_ai_usage_indexes,
    ]


async def ensure_indexes(client) -> dict:
    """
    Create every declared index that doesn't exist yet.

    One creator failing doesn't stop the rest: they cover unrelated
    collections, and a partial pass is strictly better than none. Failures are
    logged and returned so a caller (the cron endpoint) can surface them.
    """
    created, failed = [], {}

    for creator in _load_creators():
        name = creator.__name__
        try:
            await creator(client)
            created.append(name)
        except Exception as e:
            logger.error(f"[indexes] {name} failed: {e}", exc_info=True)
            failed[name] = str(e)

    logger.info(f"[indexes] {len(created)} ok, {len(failed)} failed")
    return {"ok": len(created), "failed": failed}
