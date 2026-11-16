"""Alert emails (services/email_service.py) and "Ask Claude about this" links (services/ask_claude.py)."""
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import pytest

import services.email_service as es
from services.ask_claude import alert_prompt, ask_claude_url
from tests.test_report_events import USER, _seed, event_report, slack  # noqa: F401  (slack is a fixture)

RESULT = {"triggered": True, "message": "Triggered for: Glow Clinic", "current_value": 9.0, "progress_pct": 100.0,
          "per_client_results": [{"group_name": "Glow Clinic", "triggered": True},
                                 {"group_name": "Smile", "triggered": False}]}


@pytest.fixture
def sent(monkeypatch):
    """Record every send_email call instead of reaching SMTP."""
    calls = []

    async def fake_send(to, subject, html, text):
        calls.append({"to": to, "subject": subject, "html": html, "text": text})
        return True

    monkeypatch.setattr(es, "send_email", fake_send)
    return calls


@pytest.fixture
def run_job(mock_mongo_client, monkeypatch):
    import jobs.alert_jobs as jobs
    state = {"result": RESULT}

    @asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def fake_eval(alert, client):
        return state["result"]

    monkeypatch.setattr(jobs, "get_mongo_client", fake_client)
    monkeypatch.setattr(jobs, "evaluate_alert", fake_eval)

    async def run(result=None):
        if result is not None:
            state["result"] = result
        await jobs.evaluate_all_alerts()
    return run


async def _alert(db, channels=("in_app", "email"), name="CPL high"):
    await db["alerts"].insert_one({"id": "a1", "user_id": USER, "name": name, "status": "active",
                                   "notification_channels": list(channels)})


async def test_email_sent_once_when_alert_enters_triggered(mock_db, sent, run_job):
    await _alert(mock_db)
    await run_job()
    await run_job()   # still triggered: no second email
    assert len(sent) == 1
    assert sent[0]["to"] == USER and sent[0]["subject"] == "Birdy alert: CPL high"
    assert "Glow Clinic" in sent[0]["text"] and "Smile" not in sent[0]["text"]
    notes = await mock_db["alert_notifications"].find({}).to_list(None)
    assert len(notes) == 2 and [n.get("email_sent") for n in notes] == [True, None]


async def test_email_again_after_clear_and_retrigger(mock_db, sent, run_job):
    await _alert(mock_db)
    await run_job()
    await run_job({"triggered": False, "message": "ok", "current_value": 1.0})
    assert (await mock_db["alerts"].find_one({"id": "a1"}))["status"] == "active"
    await run_job(RESULT)
    assert len(sent) == 2


async def test_no_email_without_email_channel(mock_db, sent, run_job):
    await _alert(mock_db, channels=("in_app", "slack", "carrier_pigeon"))
    await run_job()
    assert sent == []


async def test_failed_email_never_blocks_the_alert(mock_db, monkeypatch, run_job):
    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(es, "send_email", boom)
    await _alert(mock_db)
    await run_job()
    assert (await mock_db["alerts"].find_one({"id": "a1"}))["status"] == "triggered"
    note = await mock_db["alert_notifications"].find_one({})
    assert note["email_sent"] is False


async def test_email_failure_returns_false_note(mock_db, monkeypatch, run_job):
    async def nope(to, subject, html, text):
        return False
    monkeypatch.setattr(es, "send_email", nope)
    await _alert(mock_db)
    await run_job()
    assert (await mock_db["alert_notifications"].find_one({}))["email_sent"] is False


async def test_missing_smtp_settings_do_not_raise(monkeypatch):
    monkeypatch.setattr(es, "SMTP_PASS", "")
    assert await es.send_email("a@b.co", "s", "<p>h</p>", "t") is False


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL; records what a send did."""
    log = []
    fail = None

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.log.append(("connect", host, port, "ssl" if context is not None else "plain"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSMTP.log.append(("starttls",))

    def login(self, user, password):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        FakeSMTP.log.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.log.append(("send", msg["From"], msg["To"], msg["Subject"],
                             msg.get_body(("plain",)).get_content(), msg.get_body(("html",)).get_content()))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.log, FakeSMTP.fail = [], None
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(es.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(es, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(es, "SMTP_USER", "birdy@gmail.com")
    monkeypatch.setattr(es, "SMTP_PASS", "app-pass")
    monkeypatch.setattr(es, "EMAIL_FROM", "Birdy <birdy@gmail.com>")
    return FakeSMTP


async def test_send_email_over_ssl_sends_text_and_html(monkeypatch, smtp):
    monkeypatch.setattr(es, "SMTP_SECURE", True)
    monkeypatch.setattr(es, "SMTP_PORT", 465)
    assert await es.send_email("a@b.co", "Subj", "<p>h</p>", "t") is True
    assert smtp.log[0] == ("connect", "smtp.gmail.com", 465, "ssl")
    assert smtp.log[1] == ("login", "birdy@gmail.com", "app-pass")
    _, frm, to, subject, text, html = smtp.log[2]
    assert (frm, to, subject) == ("Birdy <birdy@gmail.com>", "a@b.co", "Subj")
    assert text.strip() == "t" and "<p>h</p>" in html


async def test_send_email_starttls_when_not_secure(monkeypatch, smtp):
    monkeypatch.setattr(es, "SMTP_SECURE", False)
    monkeypatch.setattr(es, "SMTP_PORT", 587)
    assert await es.send_email("a@b.co", "Subj", "<p>h</p>", "t") is True
    assert smtp.log[0] == ("connect", "smtp.gmail.com", 587, "plain") and smtp.log[1] == ("starttls",)


async def test_send_email_swallows_smtp_errors(smtp):
    import smtplib
    smtp.fail = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    assert await es.send_email("a@b.co", "Subj", "<p>h</p>", "t") is False


def test_ask_claude_url_encodes_prompt():
    alert = {"id": "a1", "name": 'CPL "high" & more'}
    prompt = alert_prompt(alert)
    assert prompt == 'Using Birdy, explain my alert "CPL "high" & more" (alert id a1) and tell me what to do about it.'
    url = ask_claude_url(prompt)
    assert url.startswith("https://claude.ai/new?q=") and " " not in url and "&" not in url.split("?q=")[1]
    assert parse_qs(urlparse(url).query)["q"] == [prompt]


def test_email_escapes_user_text_and_has_links_in_both_parts():
    alert = {"id": "a1", "name": "<script>alert(1)</script>", "user_id": USER}
    result = {"message": "<b>x</b>", "per_client_results": [{"group_name": "<i>Acme</i>", "triggered": True}]}
    subject, html, text = es.render_alert_email(alert, result)
    assert "<script>" not in html and "&lt;script&gt;" in html and "<b>x</b>" not in html and "<i>Acme</i>" not in html
    for part in (html, text):
        assert "/alerts" in part and "https://claude.ai/new?q=" in part
    assert subject == "Birdy alert: <script>alert(1)</script>"   # a plain header, not html


async def test_slack_alert_block_has_ask_claude_link(mock_db, slack):
    await _seed(mock_db, event_report("alert_fired"))
    alert = {"id": "a1", "user_id": USER, "name": "CPL high", "status": "active"}
    import services.report_events as ev
    await ev.alert_fired(mock_db, alert, RESULT, wait=True)
    assert len(slack) == 1
    buttons = [e for b in slack[0]["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert [b["url"] for b in buttons] == [ask_claude_url(alert_prompt(alert))]
    assert buttons[0]["text"]["text"] == "Ask Claude about this" and len(buttons[0]["url"]) <= 3000


async def test_alert_created_already_triggered_emails_on_next_pass(mock_db, sent, run_job):
    """I5: create-time evaluation sets status "triggered" without emailing; the cron must still email once."""
    await _alert(mock_db)
    await mock_db["alerts"].update_one({"id": "a1"}, {"$set": {"status": "triggered"}})
    await run_job()
    await run_job()
    assert len(sent) == 1


def _per_client(*triggered):
    return {"triggered": bool(triggered), "message": "Triggered for: " + ", ".join(triggered), "current_value": 9.0,
            "progress_pct": 100.0,
            "per_client_results": [{"group_id": gid, "group_name": gid.title(), "triggered": gid in triggered}
                                   for gid in ("acme", "beta")]}


async def test_second_client_triggering_emails_once_for_it(mock_db, sent, run_job):
    await _alert(mock_db)
    await run_job(_per_client("acme"))
    await run_job(_per_client("acme"))
    assert len(sent) == 1
    await run_job(_per_client("acme", "beta"))
    await run_job(_per_client("acme", "beta"))
    assert len(sent) == 2 and "Beta" in sent[1]["text"]
    # acme clears and fires again while beta holds: one more email, for acme
    await run_job(_per_client("beta"))
    await run_job(_per_client("acme", "beta"))
    assert len(sent) == 3


async def test_overlapping_runs_email_once(mock_db, mock_mongo_client, sent, monkeypatch):
    """Two cron runs that both read the alert before either writes: only one sends."""
    import asyncio
    import jobs.alert_jobs as jobs

    @asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    arrived = []
    both_in = asyncio.Event()

    async def barrier_eval(alert, client):
        arrived.append(1)
        if len(arrived) == 2:
            both_in.set()
        await both_in.wait()
        return RESULT

    monkeypatch.setattr(jobs, "get_mongo_client", fake_client)
    monkeypatch.setattr(jobs, "evaluate_alert", barrier_eval)
    await _alert(mock_db)

    await asyncio.gather(jobs.evaluate_all_alerts(), jobs.evaluate_all_alerts())

    assert len(sent) == 1
    assert await mock_db["alert_notifications"].count_documents({}) == 1
