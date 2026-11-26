"""The shared CRON_SECRET check every /api/cron route runs first."""

import pytest
from fastapi import HTTPException

from routers.cron import _verify_cron_auth

SECRET = "s3cret-value"


@pytest.fixture
def cron_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", SECRET)


def test_right_secret_passes(cron_secret):
    _verify_cron_auth(f"Bearer {SECRET}")


@pytest.mark.parametrize("header", [
    None,
    "",
    SECRET,                       # no Bearer prefix
    f"Bearer {SECRET[:-1]}",      # right prefix of the secret, one character short
    f"Bearer {SECRET}x",          # one character too many
    f"bearer {SECRET}",           # scheme is case-sensitive, as before
    "Bearer s3cret-valué",        # non-ASCII must be a 401, not a TypeError from compare_digest
])
def test_wrong_or_missing_secret_is_401(cron_secret, header):
    with pytest.raises(HTTPException) as exc:
        _verify_cron_auth(header)
    assert exc.value.status_code == 401


def test_unset_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        _verify_cron_auth(f"Bearer {SECRET}")
    assert exc.value.status_code == 503


def test_comparison_is_constant_time(cron_secret, monkeypatch):
    import hmac

    calls = []
    real_compare = hmac.compare_digest  # captured before the patch, which replaces the module attribute

    def spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr("routers.cron.hmac.compare_digest", spy)
    _verify_cron_auth(f"Bearer {SECRET}")
    assert calls == [(f"Bearer {SECRET}".encode(), f"Bearer {SECRET}".encode())]
