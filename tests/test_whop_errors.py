"""
tests/test_whop_errors.py
-------------------------
How a Whop failure reaches the admin promo-codes screen.

Every one of these used to collapse into a bare 502 "Could not reach Whop to
list promo codes" — untrue (we reach Whop fine) and unactionable (Whop had
already named the problem in the response body). These pin the translation.
"""

import httpx
import pytest
from fastapi import HTTPException
from whop_sdk import APIConnectionError, BadRequestError, NotFoundError

import billing


def _request():
    return httpx.Request("GET", "https://api.whop.com/v5/promo_codes")


def _status_error(cls, status, message):
    response = httpx.Response(status, request=_request())
    return cls(message, response=response, body={"error": {"type": "bad_request", "message": message}})


def test_missing_scope_names_the_scope():
    """The whole diagnosis is in Whop's sentence — surface it verbatim."""
    err = _status_error(
        BadRequestError,
        400,
        "Unauthorized: Actor is missing all required permissions: promo_code:basic:read",
    )

    result = billing._whop_http_error(err, "list promo codes")

    assert result.status_code == 400
    assert "promo_code:basic:read" in result.detail


def test_unknown_company_says_so():
    """What a WHOP_COMPANY_ID the key cannot act for looks like."""
    err = _status_error(NotFoundError, 404, "This Bot was not found")

    result = billing._whop_http_error(err, "list promo codes")

    assert result.status_code == 400
    assert "This Bot was not found" in result.detail


@pytest.mark.parametrize("status", [401, 403])
def test_whops_auth_failures_are_not_passed_through_as_401(status):
    """A 401 here is about Birdy's API key, not the admin's session.

    Passing it through would bounce the admin to /login — the one screen where
    a server-side credential problem cannot be fixed.
    """
    err = _status_error(BadRequestError, status, "Invalid API key")

    result = billing._whop_http_error(err, "list promo codes")

    assert result.status_code == 502


def test_a_genuine_connection_failure_is_the_only_502_that_says_unreachable():
    err = APIConnectionError(request=_request())

    result = billing._whop_http_error(err, "list promo codes")

    assert result.status_code == 502
    assert "Could not reach Whop" in result.detail


def test_a_missing_company_id_is_configuration_not_a_whop_outage(monkeypatch):
    monkeypatch.setattr(billing, "WHOP_COMPANY_ID", "")

    with pytest.raises(HTTPException) as exc:
        billing._require_company_id()

    assert exc.value.status_code == 500
    assert "WHOP_COMPANY_ID" in exc.value.detail


def test_reason_falls_back_to_the_exception_when_there_is_no_body():
    assert billing._whop_reason(RuntimeError("boom")) == "boom"
