"""
services/slack_signature.py
------------------------------
Shared HMAC signature verification for Slack's Events API (routers/slack_events.py)
and Interactivity (routers/slack_interactions.py) request URLs. Both must verify
every incoming request is genuinely from Slack before acting on it.

Uses slack_sdk.signature.SignatureVerifier rather than hand-rolling the
v0:{timestamp}:{body} HMAC-SHA256 construction — the SDK already implements it
correctly, including rejecting requests with a timestamp more than 5 minutes
old (replay protection).
"""

import logging
import os

from fastapi import HTTPException, Request
from slack_sdk.signature import SignatureVerifier

logger = logging.getLogger(__name__)

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")


def verify_slack_request(request: Request, raw_body: bytes) -> None:
    """Raises HTTPException if the request isn't a genuine, fresh Slack request.

    Fail-closed if the signing secret isn't configured, matching the
    convention in routers/webhooks.py::_verify_webhook_secret.
    """
    if not SLACK_SIGNING_SECRET:
        logger.error("SLACK_SIGNING_SECRET env var is not set — refusing Slack request")
        raise HTTPException(status_code=503, detail="Slack integration not configured")

    verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
    if not verifier.is_valid_request(body=raw_body, headers=dict(request.headers)):
        raise HTTPException(status_code=401, detail="Invalid Slack request signature")
