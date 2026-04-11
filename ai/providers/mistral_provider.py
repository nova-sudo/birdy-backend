import os
import logging
import httpx

from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall

logger = logging.getLogger(__name__)

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-medium-latest")
MISTRAL_BASE_URL = "https://api.mistral.ai/v1/chat/completions"


class MistralProvider(BaseLLMProvider):
    def __init__(self, model: str | None = None):
        if not MISTRAL_API_KEY:
            raise ValueError("MISTRAL_API_KEY environment variable is not set")
        self.model = model or MISTRAL_MODEL

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ProviderResponse:
        headers = {
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
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
            resp = await client.post(MISTRAL_BASE_URL, headers=headers, json=payload)

            if resp.status_code != 200:
                error_text = resp.text
                logger.error(f"Mistral error ({resp.status_code}): {error_text}")
                if resp.status_code == 429:
                    return ProviderResponse(
                        content="Rate limit reached. Please try again in a moment.",
                        tool_calls=[],
                        finish_reason="stop",
                    )
                raise RuntimeError(f"Mistral API error ({resp.status_code}): {error_text}")

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
