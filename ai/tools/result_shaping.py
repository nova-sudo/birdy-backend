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
