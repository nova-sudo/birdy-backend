"""services/slack_signature.py — HMAC verification shared by the Events API
and Interactivity endpoints. Uses hand-computed HMAC vectors, matching the
convention Slack itself documents for v0 request signing."""

import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException, Request

from services import slack_signature


def _make_request(headers: dict) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def _sign(secret: str, ts: str, body: bytes) -> str:
    basestring = f"v0:{ts}:{body.decode()}".encode()
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def slack_secret(monkeypatch):
    monkeypatch.setattr(slack_signature, "SLACK_SIGNING_SECRET", "test-secret-abc")


def test_valid_signature_passes():
    body = b'{"type":"url_verification","challenge":"abc"}'
    ts = str(int(time.time()))
    sig = _sign("test-secret-abc", ts, body)
    request = _make_request({"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": ts})
    slack_signature.verify_slack_request(request, body)  # should not raise


def test_bad_signature_rejected():
    body = b'{"type":"url_verification","challenge":"abc"}'
    ts = str(int(time.time()))
    request = _make_request({"X-Slack-Signature": "v0=deadbeef", "X-Slack-Request-Timestamp": ts})
    with pytest.raises(HTTPException) as exc:
        slack_signature.verify_slack_request(request, body)
    assert exc.value.status_code == 401


def test_stale_timestamp_rejected():
    body = b'{"type":"url_verification","challenge":"abc"}'
    old_ts = str(int(time.time()) - 600)
    sig = _sign("test-secret-abc", old_ts, body)
    request = _make_request({"X-Slack-Signature": sig, "X-Slack-Request-Timestamp": old_ts})
    with pytest.raises(HTTPException) as exc:
        slack_signature.verify_slack_request(request, body)
    assert exc.value.status_code == 401


def test_unconfigured_secret_fails_closed(monkeypatch):
    monkeypatch.setattr(slack_signature, "SLACK_SIGNING_SECRET", None)
    body = b'{"type":"url_verification","challenge":"abc"}'
    ts = str(int(time.time()))
    request = _make_request({"X-Slack-Signature": "v0=whatever", "X-Slack-Request-Timestamp": ts})
    with pytest.raises(HTTPException) as exc:
        slack_signature.verify_slack_request(request, body)
    assert exc.value.status_code == 503
