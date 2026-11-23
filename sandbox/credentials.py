"""
sandbox/credentials.py
----------------------
Every credential a sandbox account holds is an ``sbx_`` credential: OAuth codes, access and
refresh tokens, Hot Prospector api_uid / api_key and bearer tokens, location tokens, media URLs.
It encodes the account (user_id) and a kind, so a fake provider knows whose world a request is
about from the credential alone (no ContextVar: cron jobs and background tasks work too).

    sbx_<base64url("kind~nonce~user_id")>.<12-char signature>

URL-safe (letters, digits, ``-``, ``_``, ``.``), about 90-130 characters. The signature is an
HMAC over the payload, so a credential for one sandbox account cannot be edited into another's.

Kinds in use (a kind is free text without ``~``; add new ones as needed):
    meta_code                    Meta OAuth code (the connect URL hands it out)
    meta                         Meta user access token
    ghl_code                     GoHighLevel OAuth code
    ghl / ghl_refresh            GoHighLevel agency access / refresh token
    ghl_location:<location_id>   GoHighLevel location access token
    ghl_location_refresh:<id>    GoHighLevel location refresh token
    hp_uid / hp_key              Hot Prospector api_uid / api_key (what the connect stores)
    hp / hp_refresh              Hot Prospector v2 bearer access / refresh token
    media                        a placeholder media URL path segment
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from typing import Iterable, Optional

PREFIX = "sbx_"
_SIG_LEN = 12
_SEP = "~"

# A credential in any text: the marker must start a token (start of text, or after a character
# that cannot be part of one), so a real token that happens to contain "sbx_" (a JWT's base64)
# never matches.
CREDENTIAL_RE = re.compile(r"(?<![A-Za-z0-9_.\-])sbx_[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{%d}" % _SIG_LEN)
MARKER_RE = re.compile(rb"(?<![A-Za-z0-9_.\-])sbx_")


@dataclass(frozen=True)
class Credential:
    user_id: str
    kind: str
    nonce: str = ""
    raw: str = ""

    @property
    def base_kind(self) -> str:
        """``ghl_location:abc`` -> ``ghl_location``."""
        return self.kind.split(":", 1)[0]

    @property
    def arg(self) -> Optional[str]:
        """``ghl_location:abc`` -> ``abc``; None for a kind without an argument."""
        return self.kind.split(":", 1)[1] if ":" in self.kind else None


def _key() -> bytes:
    secret = os.getenv("SANDBOX_CREDENTIAL_SECRET") or os.getenv("JWT_SECRET") or "birdy-sandbox"
    return ("sandbox-credential:" + secret).encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())[:_SIG_LEN]


def make_credential(user_id: str, kind: str, nonce: Optional[str] = None) -> str:
    """A credential for ``user_id`` of ``kind``. ``nonce=None`` makes a random one (codes, tokens);
    pass ``""`` (or a fixed string) for a stable credential (Hot Prospector api_uid / api_key)."""
    if not user_id or not kind or _SEP in kind:
        raise ValueError("a sandbox credential needs a user_id and a kind without '~'")
    if nonce is None:
        nonce = secrets.token_hex(4)
    if _SEP in nonce:
        raise ValueError("nonce cannot contain '~'")
    payload = _b64(f"{kind}{_SEP}{nonce}{_SEP}{user_id.strip().lower()}".encode())
    return f"{PREFIX}{payload}.{_sign(payload)}"


def is_credential(value) -> bool:
    """Looks like a sandbox credential (the ``sbx_`` prefix). Does not check the signature."""
    return isinstance(value, str) and value.strip().startswith(PREFIX)


def parse_credential(value) -> Optional[Credential]:
    """The account and kind a credential encodes, or None when it is not a valid one (bad shape,
    bad signature, or an account that is not a sandbox account)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value.startswith(PREFIX) or "." not in value:
        return None
    payload, _, sig = value[len(PREFIX):].rpartition(".")
    if not payload or not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        kind, nonce, user_id = _unb64(payload).decode().split(_SEP, 2)
    except (ValueError, UnicodeDecodeError):
        return None
    from sandbox import is_sandbox_user

    if not kind or not is_sandbox_user(user_id):
        return None
    return Credential(user_id=user_id, kind=kind, nonce=nonce, raw=value)


def find_credentials(text) -> list[Credential]:
    """Every valid credential in a piece of text (str or bytes), in order."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="ignore")
    if not text:
        return []
    out = []
    for m in CREDENTIAL_RE.finditer(text):
        cred = parse_credential(m.group(0))
        if cred:
            out.append(cred)
    return out


def has_marker(chunks: Iterable[bytes]) -> bool:
    """Any chunk carries the ``sbx_`` marker at a token boundary (the routing test)."""
    return any(chunk and MARKER_RE.search(chunk) for chunk in chunks)


def credential_error(user_id: str, value) -> Optional[str]:
    """Why ``value`` may not be stored for ``user_id``, or None when it may.

    A sandbox account may only hold sandbox credentials that encode that same account; a normal
    account may never hold one. The connect callbacks call this before saving anything."""
    from sandbox import is_sandbox_user

    sandbox_user = is_sandbox_user(user_id)
    if not is_credential(value):
        return "Sandbox accounts can only connect sandbox credentials." if sandbox_user else None
    if not sandbox_user:
        return "Sandbox credentials can only be used by sandbox accounts."
    cred = parse_credential(value)
    if cred is None or cred.user_id != user_id.strip().lower():
        return "This sandbox credential belongs to a different account."
    return None
