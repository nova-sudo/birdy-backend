from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class ProviderResponse:
    content: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"


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
