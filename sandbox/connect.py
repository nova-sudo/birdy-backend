"""
sandbox/connect.py
------------------
The only product-side branching on sandbox: the connect flows.

- Meta / GoHighLevel: a sandbox account's auth URL skips the provider's dialog and goes straight to
  the same redirect URI the real flow uses, with ``code=sbx_...``. The existing callback then runs
  unchanged; its code exchange carries ``sbx_`` and is answered by the fake.
- Hot Prospector: whatever was typed is replaced by the account's sandbox credentials.
- Callbacks refuse a code / token that does not match the kind of account (``credential_error``).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sandbox import credential_error, is_sandbox_user, make_credential


def sandbox_auth_url(redirect_uri: str, user_id: str, code_kind: str) -> str:
    """``redirect_uri`` with ``code=<sbx_ code for user_id>`` added to its query."""
    parts = urlsplit(redirect_uri or "")
    query = parse_qsl(parts.query, keep_blank_values=True) + [("code", make_credential(user_id, code_kind))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def hp_credentials_for(user_id: str, api_uid: str, api_key: str) -> tuple[str, str]:
    """The Hot Prospector credentials to test and store: a sandbox account's own, whatever was typed."""
    if is_sandbox_user(user_id):
        from sandbox.hp_app import sandbox_hp_credentials

        return sandbox_hp_credentials(user_id)
    return api_uid, api_key


def code_error(user_id: str, code: str) -> Optional[str]:
    """Why a callback must not exchange this code for this account (None: go ahead)."""
    return credential_error(user_id, code)


def token_error(user_id: str, tokens: dict, *fields: str) -> Optional[str]:
    """Why a callback must not store these tokens for this account (None: go ahead)."""
    for name in fields or ("access_token",):
        value = (tokens or {}).get(name)
        if value is None and name != "access_token":
            continue
        problem = credential_error(user_id, value)
        if problem:
            return problem
    return None
