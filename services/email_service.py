"""Transactional email over SMTP (nodemailer-style settings). Used for alert emails; never raises into callers."""
import asyncio
import html as _html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from core.config import EMAIL_FROM, FRONTEND_URL, SMTP_HOST, SMTP_PASS, SMTP_PORT, SMTP_SECURE, SMTP_USER
from services.ask_claude import alert_prompt, ask_claude_url

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


def _build_message(to: str, subject: str, html: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = EMAIL_FROM, to, subject
    msg["Message-ID"] = make_msgid(domain=(SMTP_USER.split("@")[-1] if "@" in SMTP_USER else None))
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _send_sync(msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if SMTP_SECURE:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SECONDS, context=context) as smtp:
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SECONDS) as smtp:
            smtp.starttls(context=context)
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)


async def send_email(to: str, subject: str, html: str, text: str) -> bool:
    """Send one email over SMTP. False (logged, no secrets) when SMTP isn't configured or anything fails."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        logger.warning("email not sent: SMTP_HOST, SMTP_USER and SMTP_PASS must all be set")
        return False
    try:
        await asyncio.to_thread(_send_sync, _build_message(to, subject, html, text))
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
