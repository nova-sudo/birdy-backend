"""Transactional email over SMTP (nodemailer-style settings). Used for alert emails; never raises into callers."""
import asyncio
import html as _html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from core.constants import METRIC_LABELS
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


# Design A ("Card", mocks/alert-email): a white card on the app's grey canvas. Email-safe: tables + inline styles.
_INK, _BODY, _MUTED, _BORDER, _CANVAS, _PRIMARY = "#18181B", "#52525B", "#63636B", "#E7E6E3", "#F6F6F4", "#5B3FD6"
_FONT = "Inter,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MASCOT = "/birdy-mascot-VmH3J7Wq.png"
_PILLS = {"win": ("Win", "#16794C", "#E8F4EE"), "warning": ("Alert triggered", "#E5484D", "#FEF1F1")}


def _metric_value(metric: str, value) -> str:
    """'$47.20', '3.4%' or '1,240' from the metric's label unit ('Cost Per Lead ($)', 'CTR (%)')."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    label = METRIC_LABELS.get(metric or "", "")
    if "($)" in label:
        return f"${value:,.2f}"
    if "(%)" in label:
        return f"{value:,.1f}%"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _limit_text(condition: dict) -> tuple[str, str]:
    """(label, value) for the limit box: ('Alerts above', '$40.00'), ('Alerts on a', '20% rise'); ('', '') if unknown."""
    op, value, metric = condition.get("operator"), condition.get("value"), condition.get("metric")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "", ""
    if op in ("pct_drop", "pct_rise"):
        return "Alerts on a", f"{value:g}% {'drop' if op == 'pct_drop' else 'rise'}"
    label = {"gt": "Alerts above", "lt": "Alerts below", "eq": "Alerts at", "neq": "Alerts when not"}.get(op, "Your limit")
    return label, _metric_value(metric, value)


def render_alert_email(alert: dict, eval_result: dict) -> tuple[str, str, str]:
    """(subject, html, text). Every user-controlled string is escaped in the html."""
    esc = _html.escape
    name, message = alert.get("name") or "", eval_result.get("message") or ""
    clients = [r.get("group_name") for r in eval_result.get("per_client_results") or [] if r.get("triggered")]
    clients = [c for c in clients if c]
    view_url = f"{FRONTEND_URL}/alerts"
    ask_url = ask_claude_url(alert_prompt(alert))
    condition = alert.get("condition") or {}
    now = _metric_value(condition.get("metric"), eval_result.get("current_value"))
    limit_label, limit = _limit_text(condition)
    pill, pill_fg, pill_bg = _PILLS.get(alert.get("type"), _PILLS["warning"])
    now_color = "#16794C" if alert.get("type") == "win" else "#E5484D"

    stats = ""
    if now and limit:
        cell = 'style="padding:12px 14px;font-family:{f}" width="50%"'.format(f=_FONT)
        stats = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {_BORDER};'
            f'border-radius:12px;border-collapse:separate;margin:0 0 18px">'
            f'<tr><td {cell}><div style="font-size:12px;color:{_MUTED}">Now</div>'
            f'<div style="font-size:20px;font-weight:600;color:{now_color}">{esc(now)}</div></td>'
            f'<td {cell} style="padding:12px 14px;border-left:1px solid {_BORDER};font-family:{_FONT}">'
            f'<div style="font-size:12px;color:{_MUTED}">{limit_label}</div>'
            f'<div style="font-size:20px;font-weight:600;color:{_INK}">{esc(limit)}</div></td></tr></table>')
    chips = ""
    if clients:
        chips = (f'<div style="font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;'
                 f'color:{_MUTED};margin:0 0 8px">Clients involved</div><div>' + "".join(
                     f'<span style="display:inline-block;border:1px solid {_BORDER};border-radius:999px;padding:4px 10px;'
                     f'font-size:13px;color:{_INK};margin:0 6px 6px 0">{esc(c)}</span>' for c in clients) + "</div>")

    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:{_CANVAS}">
<div style="display:none;max-height:0;overflow:hidden">{esc(message)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_CANVAS}"><tr><td align="center" style="padding:28px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;font-family:{_FONT};color:{_INK}">
<tr><td style="padding:0 0 14px"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="width:36px;height:36px;border-radius:18px;background:#F3E8FF;border:1px solid #E9D5FF;overflow:hidden">
<img src="{esc(FRONTEND_URL + _MASCOT)}" width="36" height="36" alt="Birdy" style="display:block;border-radius:18px"></td>
<td style="padding-left:10px;font-size:15px;font-weight:600">Birdy</td></tr></table></td></tr>
<tr><td style="background:#ffffff;border:1px solid {_BORDER};border-radius:16px;padding:26px">
<span style="display:inline-block;background:{pill_bg};color:{pill_fg};font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px">&#9679; {pill}</span>
<h1 style="font-size:22px;line-height:1.25;font-weight:600;margin:12px 0 6px">{esc(name)}</h1>
<p style="font-size:15px;line-height:1.55;color:{_BODY};margin:0 0 18px">{esc(message)}</p>
{stats}{chips}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;border-top:1px solid #EFEEEB"><tr><td style="padding-top:18px">
<a href="{esc(view_url)}" style="display:inline-block;background:{_PRIMARY};color:#ffffff;text-decoration:none;font-size:14px;font-weight:600;padding:11px 18px;border-radius:10px;margin:0 14px 8px 0">View in Birdy</a>
<a href="{esc(ask_url)}" style="display:inline-block;color:{_PRIMARY};text-decoration:none;font-size:14px;font-weight:500;padding:11px 0">Ask Claude about this &rarr;</a>
</td></tr></table></td></tr>
<tr><td align="center" style="padding:18px 20px 4px;font-size:12px;line-height:1.6;color:#85858C">
You get this because email is on for this alert.<br><a href="{esc(view_url)}" style="color:#85858C">Change alert settings</a> &middot; Birdy</td></tr>
</table></td></tr></table></body></html>"""

    text = f"{pill}: {name}\n\n{message}\n"
    if now and limit:
        text += f"\nNow: {now}\n{limit_label}: {limit}\n"
    if clients:
        text += "\nClients involved: " + ", ".join(clients) + "\n"
    text += (f"\nView in Birdy: {view_url}\nAsk Claude about this: {ask_url}\n"
             f"\nYou get this because email is on for this alert. Change alert settings: {view_url}\n")
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
