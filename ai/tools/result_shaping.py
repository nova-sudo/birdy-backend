import json
from typing import Any

from ai.config import MAX_RESULT_CHARS


def serialize_with_truncation(result: Any) -> str:
    """Serialize a tool result to JSON, truncating with an explicit marker if oversized."""
    serialized = json.dumps(result, default=str)
    if len(serialized) > MAX_RESULT_CHARS:
        # Wrap an explicit, VALID-JSON truncation marker so the model
        # can't silently work off half-cut data.
        return json.dumps({
            "_truncated": True,
            "_original_length": len(serialized),
            "_note": (
                "Tool response exceeded the size budget and was truncated. "
                "Re-call with narrower filters (fewer groups, shorter date range, "
                "or a specific level) before making any claim about the data. "
                "DO NOT fabricate values for what was cut off."
            ),
            "preview": serialized[:MAX_RESULT_CHARS - 500],
        })
    return serialized


# compare_periods across a whole agency: ~1.3 KB a client in full, so anything over a handful of
# clients blew the budget and the agent could only say "too much data". Over budget, each client
# becomes one short row of the figures a media buyer reads first; the agency totals stay in full.
_COMPACT_METRICS = ("spend", "results", "cpl", "ctr", "cpm")


def _compact_compare(payload: dict) -> dict:
    def row(g: dict) -> dict:
        changes = g.get("changes") or {}
        out = {"group_id": g.get("group_id"), "group_name": g.get("group_name"), "currency": g.get("currency")}
        for m in _COMPACT_METRICS:
            c = changes.get(m) or {}
            out[m] = [c.get("value_a"), c.get("value_b"), c.get("pct_change")]
        return out

    groups = payload.get("groups") or []
    return {
        "overall": payload.get("overall"),
        "preset_a": payload.get("preset_a"), "preset_b": payload.get("preset_b"),
        "total_groups": payload.get("total_groups"),
        "groups": [row(g) for g in groups],
        "_note": (
            f"Compact view: one row per client for the {len(groups)} clients with the most spend, each metric as "
            "[period_a value, period_b value, percent change] (results = leads as Meta counts them). `overall` covers "
            "every client. This is enough to answer; for one client's full metrics call again with its group_ids."
        ),
    }


def shape_for_chat(tool_name: str, result: Any) -> Any:
    """A smaller form of a result that would otherwise be truncated, for the tools that have one."""
    if tool_name == "compare_periods" and isinstance(result, dict) and isinstance(result.get("groups"), list):
        if len(json.dumps(result, default=str)) > MAX_RESULT_CHARS:
            return _compact_compare(result)
    return result


def error_payload(tool_name: str, message: str) -> str:
    """Structured error JSON so the LLM recognizes 'the call failed' rather than 'the answer is empty'."""
    return json.dumps({
        "error": message,
        "tool": tool_name,
        "_note": (
            "This tool call failed. Tell the user the tool errored out "
            "and what you attempted — do not answer their question with "
            "fabricated data."
        ),
    })
