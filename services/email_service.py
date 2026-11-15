"""Transactional email through Resend. Used for alert emails; never raises into callers."""
import html as _html
import logging

import httpx

from core.config import EMAIL_FROM, FRONTEND_URL, RESEND_API_KEY
from services.ask_claude import alert_prompt, ask_claude_url

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 10


async def send_email(to: str, subject: str, html: str, text: str) -> bool:
    """POST one email to Resend. False (logged, no secrets) when the key is missing or anything fails."""
    if not RESEND_API_KEY:
        logger.warning("email not sent: RESEND_API_KEY is not set")
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(
                RESEND_URL, headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html, "text": text})
        if resp.status_code >= 300:
            logger.warning("email not sent: Resend answered %s", resp.status_code)
            return False
        return True
    except Exception as e:
        logger.warning("email not sent: %s", type(e).__name__)
        return False


def render_alert_email(alert: dict, eval_result: dict) -> tuple[str, str, str]:
    """(subject, html, text). Every user-controlled string is escaped in the html."""
    esc = _html.escape
    name, message = alert.get("name") or "", eval_result.get("message") or ""
    clients = [r.get("group_name") for r in eval_result.get("per_client_results") or [] if r.get("triggered")]
    clients = [c for c in clients if c]
    view_url = f"{FRONTEND_URL}/alerts"
    ask_url = ask_claude_url(alert_prompt(alert))

    html = f"<h2>{esc(name)}</h2><p>{esc(message)}</p>"
    if clients:
        html += "<p>Clients involved: " + ", ".join(esc(c) for c in clients) + "</p>"
    html += (f'<p><a href="{esc(view_url)}">View in Birdy</a> &middot; '
             f'<a href="{esc(ask_url)}">Ask Claude about this</a></p>')

    text = f"{name}\n\n{message}\n"
    if clients:
        text += "\nClients involved: " + ", ".join(clients) + "\n"
    text += f"\nView in Birdy: {view_url}\nAsk Claude about this: {ask_url}\n"
    return f"Birdy alert: {name}", html, text


async def send_alert_email(db, alert: dict, eval_result: dict) -> bool:
    """Email the alert's owner (the user_id is the account email unless the user doc has one)."""
    user = await db["users"].find_one({"user_id": alert["user_id"]}, {"email": 1}) or {}
    to = user.get("email") or alert["user_id"]
    if "@" not in str(to):
        logger.warning("alert %s: no email address for user %s", alert.get("id"), alert["user_id"])
        return False
    subject, html, text = render_alert_email(alert, eval_result)
    ok = await send_email(to, subject, html, text)
    logger.info("alert %s: email to user %s %s", alert.get("id"), alert["user_id"], "sent" if ok else "failed")
    return ok
