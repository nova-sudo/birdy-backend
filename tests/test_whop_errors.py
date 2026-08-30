"""
tests/test_whop_errors.py
-------------------------
How a Whop failure reaches the admin promo-codes screen.

Every one of these used to collapse into a bare 502 "Could not reach Whop to
list promo codes" — untrue (we reach Whop fine) and unactionable (Whop had
already named the problem in the response body). These pin the translation.

The doubles below carry the *shape* billing.py reads — `status_code` and a
`body` dict — rather than importing the SDK's exception classes. Those classes
moved between whop-sdk 0.x and 1.x, which is the very thing that took the app
down; a test suite that imports them would break on the pinned version and
would only ever prove the branch works on whichever SDK happens to be
installed. `_whop_http_error` deliberately falls back to reading `status_code`
off any exception for exactly this reason, so that is what gets tested.
"""

import pytest
from fastapi import HTTPException

import billing


class FakeStatusError(Exception):
    """An SDK error carrying Whop's JSON body, as both 0.x and 1.x do."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"type": "bad_request", "message": message}}


class FakeConnectionError(billing.APIConnectionError):
    """The SDK's own connection error, whatever it resolved to."""


def test_missing_scope_names_the_scope():
    """The whole diagnosis is in Whop's sentence — surface it verbatim."""
    err = FakeStatusError(
        400, "Unauthorized: Actor is missing all required permissions: promo_code:basic:read"
    )

    result = billing._whop_http_error(err, "list promo codes")

    assert result.status_code == 400
    assert "promo_code:basic:read" in result.detail


def test_unknown_company_says_so():
    """What a WHOP_COMPANY_ID the key cannot act for looks like."""
    err = FakeStatusError(404, "This Bot was not found")

    result = billing._whop_http_error(err, "list promo codes")

    assert result.status_code == 400
    assert "This Bot was not found" in result.detail


@pytest.mark.parametrize("status", [401, 403])
def test_whops_auth_failures_are_not_passed_through_as_401(status):
    """A 401 here is about Birdy's API key, not the admin's session.

    Passing it through would bounce the admin to /login — the one screen where
    a server-side credential problem cannot be fixed.
    """
    err = FakeStatusError(status, "Invalid API key")

    result = billing._whop_http_error(err, "list promo codes")

    assert result.status_code == 502


def test_an_error_with_no_status_is_reported_rather_than_swallowed():
    """The path taken when the SDK has moved its exception types.

    Nothing can be an instance of the sentinels, so this is the branch every
    SDK error falls into on a version whose exports we do not recognise. It
    must still say what happened.
    """
    result = billing._whop_http_error(RuntimeError("connection reset"), "list promo codes")

    assert result.status_code == 502
    assert "connection reset" in result.detail


def test_a_missing_company_id_is_configuration_not_a_whop_outage(monkeypatch):
    monkeypatch.setattr(billing, "WHOP_COMPANY_ID", "")

    with pytest.raises(HTTPException) as exc:
        billing._require_company_id()

    assert exc.value.status_code == 500
    assert "WHOP_COMPANY_ID" in exc.value.detail


def test_reason_prefers_whops_message_over_the_python_repr():
    err = FakeStatusError(400, "Plan not found")
    assert billing._whop_reason(err) == "Plan not found"


def test_reason_falls_back_to_the_exception_when_there_is_no_body():
    assert billing._whop_reason(RuntimeError("boom")) == "boom"


def test_the_error_types_resolve_to_something_usable():
    """Whatever the installed SDK exports, these must be exception classes.

    If the soft import ever falls through to the sentinels, isinstance checks
    still have to be legal rather than raising inside the error handler.
    """
    assert isinstance(billing.APIStatusError, type)
    assert issubclass(billing.APIStatusError, BaseException)
    assert issubclass(billing.APIConnectionError, BaseException)
