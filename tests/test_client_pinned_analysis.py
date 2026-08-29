"""
tests/test_client_pinned_analysis.py
------------------------------------
Server-side client pinning: on a client-scoped chat (client_group_id present),
the call-recording analysis tools must run against THAT client no matter what
group_id the model passes — the prompt rule is guidance, the orchestrator
override is the guarantee.
"""

import json

import pytest

from ai.orchestrator import run_chat
from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall
from ai.tools.registry import ToolRegistry


class ScriptedProvider(BaseLLMProvider):
    """Plays back a fixed sequence of responses, one per chat_completion call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.model = "stub-model"

    async def chat_completion(self, messages, tools=None, tool_choice="auto",
                              temperature=0.3, max_tokens=2048):
        return self._responses.pop(0)


def _tool_call_response(name, arguments):
    return ProviderResponse(
        content=None,
        tool_calls=[ToolCall(id="tc-1", name=name, arguments=json.dumps(arguments))],
        finish_reason="tool_calls",
    )


@pytest.fixture
def captured_registry():
    """Registry with a stub analysis tool that records the args it receives."""
    reg = ToolRegistry()
    captured = {}

    async def fake_summary(db, user_id, group_id, **kwargs):
        captured["group_id"] = group_id
        return {"ok": True, "group_id": group_id}

    reg.register(
        name="get_call_recordings_summary",
        description="stub",
        parameters={"type": "object", "properties": {}},
        executor=fake_summary,
    )
    return reg, captured


async def _run(db, registry, *, client_group_id, model_args):
    provider = ScriptedProvider([
        _tool_call_response("get_call_recordings_summary", model_args),
        ProviderResponse(content="done"),
    ])
    return await run_chat(
        provider=provider,
        tool_registry=registry,
        db=db,
        user_id="user-1",
        message="analyze this client's calls",
        page="client_detail",
        client_group_id=client_group_id,
        client_name="Acme",
    )


async def test_wrong_group_id_is_overridden(mock_db, captured_registry):
    reg, captured = captured_registry
    result = await _run(mock_db, reg, client_group_id="grp-A",
                        model_args={"group_id": "grp-OTHER-CLIENT"})
    assert captured["group_id"] == "grp-A"
    assert result["tools_used"] == ["get_call_recordings_summary"]


async def test_missing_group_id_is_filled(mock_db, captured_registry):
    reg, captured = captured_registry
    await _run(mock_db, reg, client_group_id="grp-A", model_args={})
    assert captured["group_id"] == "grp-A"


async def test_unscoped_chat_is_not_pinned(mock_db, captured_registry):
    reg, captured = captured_registry
    await _run(mock_db, reg, client_group_id=None,
               model_args={"group_id": "grp-B"})
    assert captured["group_id"] == "grp-B"
