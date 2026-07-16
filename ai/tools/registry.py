import json
import logging
from typing import Any, Callable, Coroutine, Optional

from ai.tools.result_shaping import serialize_with_truncation, error_payload

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}  # name -> {"schema": ..., "executor": ...}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        executor: Callable[..., Coroutine[Any, Any, Any]],
    ):
        self._tools[name] = {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            "executor": executor,
        }

    def get_schemas(self, allowed: Optional[list] = None) -> list[dict]:
        if allowed is None:
            return [t["schema"] for t in self._tools.values()]
        return [t["schema"] for name, t in self._tools.items() if name in allowed]

    def get_tool_names(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, tool_name: str, **kwargs) -> str:
        if tool_name not in self._tools:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        try:
            result = await self._tools[tool_name]["executor"](**kwargs)
            return serialize_with_truncation(result)
        except Exception as e:
            logger.error(f"Tool '{tool_name}' execution error: {e}", exc_info=True)
            # Explicitly return a structured error so the LLM can recognize
            # this as "the call failed" rather than "the answer is empty".
            return error_payload(tool_name, str(e))


# Global registry instance
registry = ToolRegistry()
