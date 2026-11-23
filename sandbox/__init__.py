"""
sandbox
-------
Sandbox accounts: a tester registers with an email on a sandbox domain and gets the whole
product (onboarding, sync, dashboards, agents) running on generated data. No provider is ever
called for that account: every credential it holds starts with ``sbx_``, and the httpx hook
(sandbox/routing.py) answers any provider request carrying one in-process, from the fake
provider apps (sandbox/meta_app.py, ghl_app.py, hp_app.py), which read the generated world
(sandbox/world.py) and remembered writes (sandbox/state.py).

See sandbox/README.md for the interfaces the fakes are built on.
"""

import os

from sandbox.credentials import (  # noqa: F401  (re-exported)
    PREFIX,
    Credential,
    credential_error,
    find_credentials,
    is_credential,
    make_credential,
    parse_credential,
)

DEFAULT_SANDBOX_DOMAINS = "sandbox.birdy.test"


def sandbox_domains() -> set[str]:
    """The email domains that make an account a sandbox account (env SANDBOX_EMAIL_DOMAINS)."""
    raw = os.getenv("SANDBOX_EMAIL_DOMAINS", DEFAULT_SANDBOX_DOMAINS)
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def is_sandbox_user(user_id) -> bool:
    """True when the account's email (its user_id) is on a sandbox domain. Pure, no DB read."""
    if not isinstance(user_id, str) or "@" not in user_id:
        return False
    domain = user_id.rsplit("@", 1)[1].strip().lower()
    return bool(domain) and domain in sandbox_domains()


def install() -> None:
    """Route sandbox credentials to the in-process fakes (idempotent). Called from main.py."""
    from sandbox import routing

    routing.install()
