"""POST /api/billing/webhook only acts on events Whop signed.

It grants subscriptions and credits, so an unset secret refuses everything
instead of skipping verification.
"""

import contextlib
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import billing

SECRET = "ws_test_secret"
MEMBERSHIP = {"type": "membership.went_valid",
              "data": {"id": "mem_1", "status": "active", "plan": {"id": "plan_x"},
                       "user": {"email": "attacker@example.com"}}}
PAYMENT = {"type": "payment.succeeded",
           "data": {"id": "pay_1", "plan": {"id": "plan_topup"}, "user": {"email": "attacker@example.com"}}}


@pytest.fixture
def handled(monkeypatch):
    """Records which handler ran, and with what, instead of touching Mongo."""
    calls = []

    async def membership(data, mc):
        calls.append(("membership", data["id"]))

    async def payment(data, mc):
        calls.append(("payment", data["id"]))

    @contextlib.asynccontextmanager
    async def fake_mongo():
        yield object()

    monkeypatch.setattr(billing, "_handle_membership", membership)
    monkeypatch.setattr(billing, "_handle_payment", payment)
    monkeypatch.setattr(billing, "get_mongo_client", fake_mongo)
    return calls


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(billing.router)
    return TestClient(app)


def _signed(event: dict, secret: str = SECRET) -> tuple[bytes, dict]:
    body = json.dumps(event)
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return body.encode(), {"x-whop-signature": f"t={ts},v1={sig}", "content-type": "application/json"}


@pytest.mark.parametrize("unset", ["", None])
@pytest.mark.parametrize("event", [MEMBERSHIP, PAYMENT])
def test_unset_secret_refuses_every_event(monkeypatch, client, handled, unset, event):
    monkeypatch.setattr(billing, "WHOP_WEBHOOK_SECRET", unset or "")
    res = client.post("/api/billing/webhook", content=json.dumps(event),
                      headers={"content-type": "application/json"})
    assert res.status_code == 503
    assert handled == []  # no plan granted, no credits added


@pytest.mark.parametrize("event,expected", [(MEMBERSHIP, ("membership", "mem_1")), (PAYMENT, ("payment", "pay_1"))])
def test_signed_event_is_handled(monkeypatch, client, handled, event, expected):
    monkeypatch.setattr(billing, "WHOP_WEBHOOK_SECRET", SECRET)
    body, headers = _signed(event)
    res = client.post("/api/billing/webhook", content=body, headers=headers)
    assert res.status_code == 200 and res.json() == {"success": True}
    assert handled == [expected]


@pytest.mark.parametrize("headers", [
    {},                                                    # unsigned
    {"x-whop-signature": "t=1,v1=" + "0" * 64},            # signature that doesn't match
    {"x-whop-signature": "garbage"},                       # malformed header
])
def test_unsigned_or_badly_signed_event_is_refused(monkeypatch, client, handled, headers):
    monkeypatch.setattr(billing, "WHOP_WEBHOOK_SECRET", SECRET)
    res = client.post("/api/billing/webhook", content=json.dumps(MEMBERSHIP),
                      headers={"content-type": "application/json", **headers})
    assert res.status_code == 400
    assert handled == []


def test_event_signed_with_another_secret_is_refused(monkeypatch, client, handled):
    monkeypatch.setattr(billing, "WHOP_WEBHOOK_SECRET", SECRET)
    body, headers = _signed(PAYMENT, secret="someone-elses-secret")
    res = client.post("/api/billing/webhook", content=body, headers=headers)
    assert res.status_code == 400
    assert handled == []


def test_body_changed_after_signing_is_refused(monkeypatch, client, handled):
    monkeypatch.setattr(billing, "WHOP_WEBHOOK_SECRET", SECRET)
    body, headers = _signed(MEMBERSHIP)
    tampered = body.replace(b"attacker@example.com", b"victim@example.com")
    res = client.post("/api/billing/webhook", content=tampered, headers=headers)
    assert res.status_code == 400
    assert handled == []
