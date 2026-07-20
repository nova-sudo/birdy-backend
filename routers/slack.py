"""
routers/slack.py
-------------------
Slack bot installation: OAuth install flow + status/remove for the
authenticated Birdy user. One Slack workspace installation maps to exactly
one Birdy user account (see services/slack_bot_service.py's docstring).

The bot token itself never reaches the browser — unlike the existing GHL/Meta
OAuth callbacks (routers/ghl.py, routers/meta.py), which echo their tokens
into a cookie for the frontend to read, a Slack bot token is a standing
credential that can post to the whole workspace and has no reason to ever
leave the server.
"""

import logging
import os
import time
import urllib.parse

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.config import JWT_SECRET, JWT_ALGORITHM
from core.database import get_db
from core.models import (
    SlackBotStatusResponse,
    SlackChannel,
    SlackChannelsResponse,
    SetSlackChannelRequest,
)
from dependencies import get_current_user, get_mongo_client
from services.slack_bot_service import (
    save_slack_bot_installation,
    get_slack_bot_status,
    remove_slack_bot_installation,
    get_decrypted_bot_token_for_user,
    set_notify_channel,
    SlackTeamAlreadyLinkedError,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")
SLACK_REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI")

# chat:write.public lets the bot post to any public channel without being
# invited; channels:read/groups:read let us list channels for the picker.
# Adding scopes requires existing installs to re-authorize (re-install).
_BOT_SCOPES = "app_mentions:read,chat:write,chat:write.public,channels:read,groups:read,im:history,im:read"
_STATE_TTL_SECONDS = 600
_FRONTEND_SETTINGS_URL = "https://birdy-beta.vercel.app/settings?tab=integrations"


def _mint_install_state(user_id: str) -> str:
    # A distinct "purpose" claim (not "type": "access") documents intent, but
    # the real security boundary is the short TTL — any correctly-signed JWT
    # with a "sub" claim would pass get_current_user()'s cookie check
    # regardless of claim shape (a pre-existing property of this codebase's
    # single-shared-secret scheme, e.g. ai/mcp_client.py's internal token).
    exp = int(time.time()) + _STATE_TTL_SECONDS
    return pyjwt.encode(
        {"sub": user_id, "purpose": "slack_install", "exp": exp},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def _verify_install_state(state: str) -> str:
    payload = pyjwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get("purpose") != "slack_install":
        raise ValueError("Wrong token purpose")
    return payload["sub"]


@router.get("/api/connect/slack")
async def connect_slack(current_user: str = Depends(get_current_user)):
    if not SLACK_CLIENT_ID or not SLACK_REDIRECT_URI:
        raise HTTPException(status_code=503, detail="Slack integration not configured")

    state = _mint_install_state(current_user)
    params = {
        "client_id": SLACK_CLIENT_ID,
        "scope": _BOT_SCOPES,
        "redirect_uri": SLACK_REDIRECT_URI,
        "state": state,
    }
    auth_url = f"https://slack.com/oauth/v2/authorize?{urllib.parse.urlencode(params)}"
    logger.info(f"Generated Slack install URL for user: {current_user}")
    return {"auth_url": auth_url}


@router.api_route("/api/connect/slack/callback", methods=["GET", "POST"])
async def slack_callback(request: Request, response: Response):
    error = request.query_params.get("error")
    if error:
        redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=error&error={urllib.parse.quote(error)}"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=error&error=missing_code_or_state"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}

    try:
        user_id = _verify_install_state(state)
    except Exception as e:
        logger.warning(f"Slack install state verification failed: {e}")
        redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=error&error=invalid_state"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}

    try:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient()
        oauth_response = await client.oauth_v2_access(
            client_id=SLACK_CLIENT_ID,
            client_secret=SLACK_CLIENT_SECRET,
            code=code,
            redirect_uri=SLACK_REDIRECT_URI,
        )
        team = oauth_response.get("team") or {}

        async with get_mongo_client() as mongo_client:
            db = get_db(mongo_client)
            await save_slack_bot_installation(
                db,
                user_id,
                team_id=team.get("id"),
                bot_token=oauth_response["access_token"],
                bot_user_id=oauth_response.get("bot_user_id"),
                team_name=team.get("name"),
                scope=oauth_response.get("scope"),
            )
    except SlackTeamAlreadyLinkedError as e:
        redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=error&error={urllib.parse.quote(str(e))}"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}
    except Exception as e:
        logger.error(f"Slack OAuth callback error for {user_id}: {e}", exc_info=True)
        redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=error&error={urllib.parse.quote(str(e))}"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}

    logger.info(f"Slack bot installed (team {team.get('id')}) for user: {user_id}")
    redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=success"
    response.headers["Location"] = redirect_url
    response.status_code = 302
    return {"redirect_url": redirect_url}


@router.get("/api/integrations/slack/status", response_model=SlackBotStatusResponse)
async def slack_status(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        status = await get_slack_bot_status(db, current_user)
        if not status:
            return SlackBotStatusResponse(installed=False)
        return SlackBotStatusResponse(installed=True, **status)


@router.delete("/api/integrations/slack/remove")
async def slack_remove(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        removed = await remove_slack_bot_installation(db, current_user)
        if not removed:
            raise HTTPException(status_code=404, detail="User not found")
        logger.info(f"Removed Slack bot installation for user: {current_user}")
        return {"success": True, "message": "Slack bot removed successfully"}


# ── Suggestion channel (where Birdy posts AI suggestions) ─────────────────────

@router.get("/api/integrations/slack/channels", response_model=SlackChannelsResponse)
async def slack_channels(current_user: str = Depends(get_current_user)):
    """List channels the bot can see, for the settings picker. Requires the
    channels:read/groups:read scopes (added 2026-07) — an older install must
    reconnect Slack to grant them."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        bot_token = await get_decrypted_bot_token_for_user(db, current_user)
        if not bot_token:
            raise HTTPException(status_code=404, detail="Slack bot not connected")

        from slack_sdk.web.async_client import AsyncWebClient
        client = AsyncWebClient(token=bot_token)

        channels: list[SlackChannel] = []
        cursor = None
        try:
            for _ in range(10):  # page cap — plenty for typical workspaces
                resp = await client.conversations_list(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
                for c in resp.get("channels", []):
                    channels.append(SlackChannel(
                        id=c["id"], name=c.get("name", ""), is_private=c.get("is_private", False),
                    ))
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            logger.warning(f"Slack conversations_list failed for {current_user}: {e}")
            raise HTTPException(
                status_code=502,
                detail="Couldn't list Slack channels — reconnect Slack to grant channel access.",
            )

        channels.sort(key=lambda c: c.name.lower())
        return SlackChannelsResponse(channels=channels)


@router.put("/api/integrations/slack/channel")
async def slack_set_channel(body: SetSlackChannelRequest,
                            current_user: str = Depends(get_current_user)):
    """Choose (or clear) the channel Birdy posts suggestions to. Editable anytime."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        ok = await set_notify_channel(db, current_user, body.channel_id, body.channel_name)
        if not ok:
            raise HTTPException(status_code=404, detail="Slack bot not connected")
        return {
            "ok": True,
            "notify_channel_id": body.channel_id,
            "notify_channel_name": body.channel_name,
        }
