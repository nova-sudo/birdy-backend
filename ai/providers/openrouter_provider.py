import os
import logging
import httpx
import json

from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(BaseLLMProvider):
    def __init__(self, model: str | None = None):
        if not OPENROUTER_API_KEY:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set")
        self.model = model or OPENROUTER_MODEL

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ProviderResponse:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(OPENROUTER_BASE_URL, headers=headers, json=payload)

            if resp.status_code != 200:
                error_text = resp.text
                logger.error(f"OpenRouter error ({resp.status_code}): {error_text}")
                if resp.status_code == 429 or resp.status_code == 413:
                    return ProviderResponse(
                        content="I have too much data to process at once. Try a more specific question.",
                        tool_calls=[],
                        finish_reason="stop",
                    )
                raise RuntimeError(f"OpenRouter API error ({resp.status_code}): {error_text}")

            data = resp.json()

        choice = data["choices"][0]
        message = choice["message"]

        tool_calls = []
        for tc in message.get("tool_calls") or []:
            tool_calls.append(ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=tc["function"]["arguments"],
            ))

        return ProviderResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or "stop",
        )
