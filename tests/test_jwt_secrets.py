"""core.config._jwt_secrets: the app refuses to start without a real signing secret."""

import pytest

from core.config import _jwt_secrets


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_jwt_secret_stops_startup(value):
    environ = {} if value is None else {"JWT_SECRET": value}
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _jwt_secrets(environ)


def test_refresh_secret_used_as_given():
    assert _jwt_secrets({"JWT_SECRET": "main-secret", "JWT_REFRESH_SECRET": "other"}) == ("main-secret", "other")


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_blank_refresh_secret_is_derived_exactly_as_before(value, caplog):
    environ = {"JWT_SECRET": "main-secret"}
    if value is not None:
        environ["JWT_REFRESH_SECRET"] = value
    # Same value the old fallback produced, so refresh cookies already issued keep working.
    assert _jwt_secrets(environ) == ("main-secret", "main-secret_refresh")
    assert "JWT_REFRESH_SECRET is not set" in caplog.text


def test_refresh_secret_equal_to_jwt_secret_is_refused():
    with pytest.raises(RuntimeError, match="must differ"):
        _jwt_secrets({"JWT_SECRET": "same", "JWT_REFRESH_SECRET": "same"})


def test_secrets_are_not_trimmed():
    # Trimming a real secret would change it and sign everyone out.
    assert _jwt_secrets({"JWT_SECRET": " padded ", "JWT_REFRESH_SECRET": "r "}) == (" padded ", "r ")
