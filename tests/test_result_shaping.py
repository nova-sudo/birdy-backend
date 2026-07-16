"""
Both ai/tools/registry.py (legacy fallback) and ai/mcp_client.py (MCP path)
delegate to these two functions for the JSON contract the LLM sees — this is
the single source of truth both paths share, so testing it once covers both.
"""

import json

from ai.config import MAX_RESULT_CHARS
from ai.tools.result_shaping import serialize_with_truncation, error_payload


def test_small_result_serializes_untouched():
    result = {"alerts": [{"id": "a1", "name": "Test"}], "counts": {"total": 1}}
    out = serialize_with_truncation(result)
    assert json.loads(out) == result


def test_oversized_result_is_truncated_with_valid_json_marker():
    huge = {"data": "x" * (MAX_RESULT_CHARS + 5000)}
    out = serialize_with_truncation(huge)
    parsed = json.loads(out)  # must still be valid JSON, not truncated mid-string

    assert parsed["_truncated"] is True
    assert parsed["_original_length"] > MAX_RESULT_CHARS
    assert "preview" in parsed
    assert len(out) < len(json.dumps(huge))


def test_error_payload_shape():
    out = error_payload("get_alerts", "boom")
    parsed = json.loads(out)

    assert parsed["error"] == "boom"
    assert parsed["tool"] == "get_alerts"
    assert "_note" in parsed


def test_serialize_with_truncation_handles_non_json_native_types():
    from datetime import datetime

    result = {"created_at": datetime(2026, 1, 1)}
    out = serialize_with_truncation(result)  # datetime isn't JSON-serializable by default
    parsed = json.loads(out)
    assert "2026-01-01" in parsed["created_at"]
