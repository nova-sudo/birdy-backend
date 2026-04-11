import logging
from groq import AsyncGroq, APIStatusError

from ai.config import GROQ_API_KEY, GROQ_MODEL
from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall

logger = logging.getLogger(__name__)


class GroqProvider(BaseLLMProvider):
    def __init__(self, model: str | None = None):
        if not GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY environment variable is not set")
        self.client = AsyncGroq(api_key=GROQ_API_KEY)
        self.model = model or GROQ_MODEL

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ProviderResponse:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except APIStatusError as e:
            if e.status_code == 413 or "rate_limit" in str(e):
                logger.warning(f"Groq token limit hit: {e}")
                return ProviderResponse(
                    content="I have too much data to process at once. Try a more specific question, like adding a date range or asking about a specific client group.",
                    tool_calls=[],
                    finish_reason="stop",
                )
            raise

        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                ))

        return ProviderResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
        )
