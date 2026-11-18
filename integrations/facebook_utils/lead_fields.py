"""
A Meta lead's name, email and phone, read from its form answers.

Meta puts the answers in `field_data` under the question's own key. A form built from the standard
questions uses `full_name`, `email` and `phone_number`; a form with its own questions uses whatever the
label was ("first name", "phone number"). Reading only the standard keys left the name and phone empty
on those leads, and the phone out of `match_keys`.
"""

import re

_NAME_FULL = ("full name", "full_name", "fullname")
_NAME_FIRST = ("first name", "first_name", "firstname")
_NAME_LAST = ("last name", "last_name", "lastname", "surname")
_PHONE = ("phone", "mobile")
_EMAIL = ("email", "e-mail")


def _find(answers: dict, words, accept=lambda value: True) -> str:
    for label, value in answers.items():
        text = str(value).strip() if value is not None else ""
        if text and any(word in str(label).lower() for word in words) and accept(text):
            return text
    return ""


def _looks_like_phone(text: str) -> bool:
    return len(re.sub(r"\D", "", text)) >= 7


def contact_fields(answers) -> tuple[str, str, str]:
    """(full name, email, phone) from a lead's form answers ({question: answer}); "" where there is none."""
    if not isinstance(answers, dict):
        return "", "", ""
    name = str(answers.get("full_name") or "").strip() or _find(answers, _NAME_FULL)
    if not name:
        name = f"{_find(answers, _NAME_FIRST)} {_find(answers, _NAME_LAST)}".strip()
    email = str(answers.get("email") or "").strip() or _find(answers, _EMAIL, lambda t: "@" in t)
    phone = str(answers.get("phone_number") or "").strip() or _find(answers, _PHONE, _looks_like_phone)
    return name, email, phone
