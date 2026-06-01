"""
services/contact_classifier.py
------------------------------
Single source of truth for deciding whether a GHL contact is a "lead" or a
plain "contact" for the Leads page.

Rule (locked in with product 2026-06-01):
    A contact is a LEAD iff its first-touch attribution shows Facebook paid
    social. Specifically:

        attributionSource.utmSource     (trimmed, lower-cased) == "facebook"
        attributionSource.sessionSource (trimmed, lower-cased) == "paid social"

    Both must match. Anything else — missing attribution, different utm
    source, different session source, organic / Google / direct, etc. — is
    a plain CONTACT.

The check is intentionally case-insensitive and whitespace-tolerant because
GHL's attribution fields are free-form and we've seen capitalisation drift
across workflow versions ("Paid Social" vs "paid social" vs " Paid Social ").

Used at two points:

  1. Write time: services/ghl_service.py stamps `lead_type` on each contact
     document as it lands, so the field is indexable.
  2. Read time:  routers/client_groups.py uses this same function to set
     the `type` field in API responses, so a contact that hasn't been
     backfilled yet still reports the right value.

Don't read `lastAttributionSource` here — that's last-touch and would
re-classify a Google contact as a lead the moment they later clicked a
Facebook ad. Product wants first-touch.
"""

from __future__ import annotations

from typing import Literal


ContactType = Literal["lead", "contact"]


def _norm(value) -> str:
    """Lower-cased, trimmed string. Returns '' for None or non-strings."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def classify_contact_type(contact_data) -> ContactType:
    """Return "lead" or "contact" for a single GHL contact payload.

    `contact_data` is the raw GHL contact dict as stored in
    ghl_contacts.contact_data. Anything not a dict (None, list, str) is
    treated as a plain contact — caller bugs shouldn't bubble up.
    """
    if not isinstance(contact_data, dict):
        return "contact"

    attr = contact_data.get("attributionSource")
    if not isinstance(attr, dict):
        return "contact"

    if _norm(attr.get("utmSource")) == "facebook" \
       and _norm(attr.get("sessionSource")) == "paid social":
        return "lead"

    return "contact"
