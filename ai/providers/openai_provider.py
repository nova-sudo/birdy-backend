import hashlib
import json
import logging
from openai import AsyncOpenAI, APIStatusError

from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall, Usage

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_MODEL = "gpt-4o"
CACHE_KEY_PREFIX_CHARS = 4000


def prompt_cache_key(model: str, messages: list[dict], tools: list[dict] | None) -> str:
    """One key per fixed prefix (model, tool schemas, the start of the system prompt). OpenAI caches a
    prompt's prefix on its own; the key routes calls that share a prefix (every loop turn of a reply,
    and the next reply of the same agent) to the same cache, so more of them hit it."""
    system = next((m.get("content") for m in messages if m.get("role") == "system"), "") or ""
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(json.dumps([t.get("function", {}).get("name") for t in tools or []]).encode())
    h.update(str(system)[:CACHE_KEY_PREFIX_CHARS].encode())
    return "birdy-" + h.hexdigest()[:32]


class OpenAIProvider(BaseLLMProvider):
    """Runs on Birdy's own account key, falling back to it when none is passed.

    This used to be BYOK-only. Asking each agency for a key meant one wrong
    paste — or a key for a model that cannot call tools — quietly broke chat in
    a way the user could not diagnose, so the account key is the default and
    an explicitly-passed key is the exception.
    """

    supports_vision = True

    def __init__(self, api_key: str | None = None, model: str | None = None):
        from ai.config import OPENAI_API_KEY
        api_key = api_key or OPENAI_API_KEY
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is not set")
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model or DEFAULT_OPENAI_MODEL

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
        # extra_body rather than the named argument, so an older pinned SDK still sends it
        kwargs["extra_body"] = {"prompt_cache_key": prompt_cache_key(self.model, messages, tools)}

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except APIStatusError as e:
            if e.status_code == 413 or "rate_limit" in str(e):
                logger.warning(f"OpenAI token limit hit: {e}")
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

        u = getattr(response, "usage", None)
        usage = Usage(
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
            cached_input_tokens=getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
        ) if u else Usage()

        return ProviderResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
