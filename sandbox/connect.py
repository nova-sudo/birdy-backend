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


def foreign_provider_id(user_id: str, location_id=None, meta_ad_account_id=None) -> Optional[str]:
    """For a sandbox account: the id (a GHL location or Meta ad account) that is not in its world,
    else None. A sandbox account may only hold its own fake ids, so it can never claim a real
    location and have that location's incoming webhooks (call logs) attributed to it. Always None
    for a normal account."""
    if not is_sandbox_user(user_id):
        return None
    from sandbox.world import world_for

    w = world_for(user_id)
    if location_id:
        c = w.client(str(location_id))
        if c is None or c.ghl_location_id != str(location_id):
            return str(location_id)
    if meta_ad_account_id:
        act = str(meta_ad_account_id)
        act = act if act.startswith("act_") else f"act_{act}"
        c = w.client(act)
        if c is None or c.meta_ad_account_id != act:
            return str(meta_ad_account_id)
    return None


def recording_url_error(user_id: str, recording_url) -> Optional[str]:
    """Why call analysis must not download this ad-hoc recording URL for this account (None: go
    ahead). Sandbox calls have no recordings, so a sandbox account never downloads one: no real
    host is fetched and nothing reaches the transcription service."""
    if is_sandbox_user(user_id) and recording_url:
        return "Sandbox calls have no recordings to analyze."
    return None
