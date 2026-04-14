"""
Utilities for normalizing phone numbers and emails for cross-source lead matching.

Match keys are stored on each lead document and used to join across
ghl_contacts, facebook_leads, and hotprospector_leads at query time.
"""

import re


def normalize_phone(raw: str | None) -> str | None:
    """
    Normalize a phone number to its last 10 digits for matching.

    Strips all non-digit characters, removes common country code prefixes
    (UK +44, US +1), and returns the last 10 digits.
    Returns None if the result is fewer than 7 digits.

    Examples:
        "+44 7505 123456"  → "7505123456"
        "07505123456"      → "7505123456"
        "447505123456"     → "7505123456"
        "+1 (555) 123-4567" → "5551234567"
    """
    if not raw or not isinstance(raw, str):
        return None

    # Strip everything except digits
    digits = re.sub(r"\D", "", raw)

    if not digits:
        return None

    # Take last 10 digits (handles country code prefixes automatically)
    canonical = digits[-10:]

    # Too short to be a real number
    if len(canonical) < 7:
        return None

    return canonical


def normalize_email(raw: str | None) -> str | None:
    """
    Normalize an email for matching: lowercase, stripped.
    Returns None for empty, missing, or placeholder emails.

    Examples:
        "John@Example.COM"     → "john@example.com"
        "no_email_ghl_abc123"  → None
        ""                     → None
    """
    if not raw or not isinstance(raw, str):
        return None

    email = raw.strip().lower()

    if not email or email.startswith("no_email_") or "@" not in email:
        return None

    return email


def compute_match_keys(email: str | None, phone: str | None) -> list[str]:
    """
    Compute a list of match keys from email and/or phone.

    Returns keys like ["email:john@example.com", "phone:7505123456"].
    A lead matches another lead if ANY match key overlaps.
    """
    keys = []

    norm_email = normalize_email(email)
    if norm_email:
        keys.append(f"email:{norm_email}")

    norm_phone = normalize_phone(phone)
    if norm_phone:
        keys.append(f"phone:{norm_phone}")

    return keys
