from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class Usage:
    """Token usage for one model call, used for Birdy Credits metering.
    Providers populate this from their raw response; defaults to zero so any
    provider that can't report usage simply meters nothing for that call."""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ProviderResponse:
    content: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)


class BaseLLMProvider(ABC):
    @abstractmethod
    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ProviderResponse:
        ...
