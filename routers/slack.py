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
    DEAD_TOKEN_ERRORS,
    mark_needs_reconnect,
    list_bot_channels,
    SlackTeamAlreadyLinkedError,
)

logger = logging.getLogger(__name__)

from sandbox import is_credential, is_sandbox_user
from sandbox.connect import code_error, sandbox_auth_url

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


def _mint_install_state(user_id: str, real: bool = False) -> str:
    # A distinct "purpose" claim (not "type": "access") documents intent, but
    # the real security boundary is the short TTL — any correctly-signed JWT
    # with a "sub" claim would pass get_current_user()'s cookie check
    # regardless of claim shape (a pre-existing property of this codebase's
    # single-shared-secret scheme, e.g. ai/mcp_client.py's internal token).
    exp = int(time.time()) + _STATE_TTL_SECONDS
    claims = {"sub": user_id, "purpose": "slack_install", "exp": exp}
    if real:
        # A sandbox account that asked for a real workspace (see connect_slack).
        claims["real"] = True
    return pyjwt.encode(
        claims,
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def _read_install_state(state: str) -> dict:
    payload = pyjwt.decode(state, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get("purpose") != "slack_install":
        raise ValueError("Wrong token purpose")
    return payload


def _verify_install_state(state: str) -> str:
    return _read_install_state(state)["sub"]


@router.get("/api/connect/slack")
async def connect_slack(real: bool = False, current_user: str = Depends(get_current_user)):
    if not SLACK_CLIENT_ID or not SLACK_REDIRECT_URI:
        raise HTTPException(status_code=503, detail="Slack integration not configured")

    sandbox = is_sandbox_user(current_user)
    state = _mint_install_state(current_user, real=sandbox and real)
    if sandbox and not real:
        # By default a sandbox account gets a made-up workspace (sandbox/slack_fake.py): Slack
        # links a real one to a single Birdy account, so a tester's own workspace may be refused.
        # Straight back to our callback with a sandbox code, no Slack screens. `?real=true` sends
        # it through Slack like a normal account, so a tester can see Birdy in a real workspace.
        from sandbox import slack_fake
        auth_url = sandbox_auth_url(SLACK_REDIRECT_URI, current_user, slack_fake.CODE_KIND)
        return {"auth_url": f"{auth_url}&{urllib.parse.urlencode({'state': state})}"}
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
        install_state = _read_install_state(state)
        user_id = install_state["sub"]
    except Exception as e:
        logger.warning(f"Slack install state verification failed: {e}")
        redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=error&error=invalid_state"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}

    # A sandbox account exchanges a sandbox code, and a normal account never does. The one exception:
    # a sandbox account that asked for a real workspace (the signed state says so) exchanges Slack's
    # own code.
    real_for_sandbox = bool(install_state.get("real")) and is_sandbox_user(user_id) and not is_credential(code)
    problem = None if real_for_sandbox else code_error(user_id, code)
    if problem:
        logger.warning(f"Slack callback refused for {user_id}: {problem}")
        redirect_url = f"{_FRONTEND_SETTINGS_URL}&status=error&error={urllib.parse.quote(problem)}"
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


def slack_fake_team_prefix() -> str:
    from sandbox import slack_fake
    return slack_fake.TEAM_PREFIX


@router.get("/api/integrations/slack/status", response_model=SlackBotStatusResponse)
async def slack_status(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        status = await get_slack_bot_status(db, current_user)
        sandbox = is_sandbox_user(current_user)
        if not status:
            return SlackBotStatusResponse(installed=False, sandbox_account=sandbox)
        test_workspace = sandbox and str(status.get("team_id") or "").startswith(slack_fake_team_prefix())
        return SlackBotStatusResponse(installed=True, sandbox_account=sandbox, test_workspace=test_workspace, **status)


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

        try:
            channels = [SlackChannel(**c) for c in await list_bot_channels(bot_token)]
        except Exception as e:
            # Every failure used to come back as "reconnect Slack to grant
            # channel access", which named the one cause it usually was not.
            # The account that surfaced this had a token Slack was answering
            # `invalid_auth` to — nothing to do with scopes — so the screen
            # sent someone to re-grant a permission they already had, and the
            # step stayed broken afterwards.
            slack_error = getattr(getattr(e, "response", None), "data", {}) or {}
            code = slack_error.get("error") if isinstance(slack_error, dict) else None
            logger.warning(
                f"Slack conversations_list failed for {current_user}: {code or e}"
            )

            if code in DEAD_TOKEN_ERRORS:
                # Remembered, so /status stops calling this install healthy and
                # the UI can offer the one action that actually helps.
                await mark_needs_reconnect(db, current_user, code)
                raise HTTPException(
                    status_code=409,
                    detail="Slack has disconnected Birdy from your workspace. Reconnect Slack to continue.",
                )
            if code == "missing_scope":
                await mark_needs_reconnect(db, current_user, code)
                raise HTTPException(
                    status_code=409,
                    detail="Birdy can't read your channel list yet — reconnect Slack to grant channel access.",
                )
            # Transient, or something we have not seen. Not the user's to fix,
            # and not worth flagging the install over.
            raise HTTPException(
                status_code=502,
                detail="Couldn't reach Slack just now. Try again in a moment.",
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
