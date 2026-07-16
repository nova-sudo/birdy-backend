import os
import logging
import json
from anthropic import AsyncAnthropic

from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = api_key or ANTHROPIC_API_KEY
        if not key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
        self.client = AsyncAnthropic(api_key=key)
        self.model = model or "claude-sonnet-4-20250514"

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ProviderResponse:
        # Anthropic uses a separate system param, not a system message
        system_prompt = None
        filtered_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompt = msg["content"]
            else:
                filtered_messages.append(msg)

        # Convert messages from OpenAI/Groq format to Anthropic format
        anthropic_messages = self._convert_messages(filtered_messages)

        kwargs = {
            "model": self.model,
            "messages": anthropic_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = {"type": tool_choice}

        response = await self.client.messages.create(**kwargs)

        # Parse response
        content_text = None
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                content_text = block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=json.dumps(block.input),
                ))

        return ProviderResponse(
            content=content_text,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if response.stop_reason == "tool_use" else "stop",
        )

    def _convert_tools(self, openai_tools: list[dict]) -> list[dict]:
        """Convert OpenAI/Groq tool format to Anthropic tool format."""
        anthropic_tools = []
        for tool in openai_tools:
            func = tool["function"]
            anthropic_tools.append({
                "name": func["name"],
                "description": func["description"],
                "input_schema": func["parameters"],
            })
        return anthropic_tools

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        """Convert OpenAI/Groq message format to Anthropic message format."""
        anthropic_messages = []

        for msg in messages:
            role = msg["role"]

            if role == "assistant" and msg.get("tool_calls"):
                # Assistant message with tool calls -> content blocks
                content = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    func = tc["function"]
                    try:
                        tool_input = json.loads(func["arguments"]) if func["arguments"] else {}
                    except (json.JSONDecodeError, TypeError):
                        tool_input = {}
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": func["name"],
                        "input": tool_input,
                    })
                anthropic_messages.append({"role": "assistant", "content": content})

            elif role == "tool":
                # Tool result -> merge into user message with tool_result blocks
                # Anthropic expects tool results inside a user message
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
                # If last message is already a user message with tool results, merge
                if (anthropic_messages
                        and anthropic_messages[-1]["role"] == "user"
                        and isinstance(anthropic_messages[-1]["content"], list)):
                    anthropic_messages[-1]["content"].append(tool_result)
                else:
                    anthropic_messages.append({
                        "role": "user",
                        "content": [tool_result],
                    })

            elif role == "assistant":
                anthropic_messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                })

            elif role == "user":
                anthropic_messages.append({
                    "role": "user",
                    "content": msg.get("content") or "",
                })

        return anthropic_messages
