"""Plain-language explanations (and one-click fixes) for Meta write errors."""

from __future__ import annotations

from integrations.facebook_utils.meta_creative_writer import MetaWriteError

_ADSET_STATES = ("paused", "archived", "deleted", "inactive")


def _text(err: MetaWriteError) -> str:
    return f"{err.user_msg or ''} {err}".lower()


def explain(err: MetaWriteError) -> dict:
    """{"message", "fix"}; fix is a machine key the wizard maps to an action, or None."""
    message = err.user_msg or str(err)
    text = _text(err)
    if err.code == 190:
        return {"message": "Your Meta connection expired. Reconnect Meta and try again.",
                "fix": "reconnect_meta"}
    if err.retryable:
        return {"message": "Meta is rate limiting requests right now. Try again in a few minutes.",
                "fix": "retry_later"}
    if err.code == 368 or err.subcode == 1885183:
        return {"message": message, "fix": None}
    if err.code == 100 and ("form" in text or "page" in text) and "ad set" not in text:
        return {"message": "This lead form does not belong to the selected Page. "
                           "Use a website link instead, or pick the form's own Page.",
                "fix": "use_website_url"}
    if "ad set" in text and any(s in text for s in _ADSET_STATES):
        return {"message": "The chosen ad set is paused or archived. Pick another ad set.",
                "fix": "pick_adset"}
    return {"message": message, "fix": None}
