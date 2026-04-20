import json
import logging
from typing import Any, Callable, Coroutine

from ai.config import MAX_RESULT_CHARS

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

    def get_schemas(self) -> list[dict]:
        return [t["schema"] for t in self._tools.values()]

    def get_tool_names(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, tool_name: str, **kwargs) -> str:
        if tool_name not in self._tools:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        try:
            result = await self._tools[tool_name]["executor"](**kwargs)
            serialized = json.dumps(result, default=str)
            if len(serialized) > MAX_RESULT_CHARS:
                # Wrap an explicit, VALID-JSON truncation marker so the model
                # can't silently work off half-cut data.
                return json.dumps({
                    "_truncated": True,
                    "_original_length": len(serialized),
                    "_note": (
                        "Tool response exceeded the size budget and was truncated. "
                        "Re-call with narrower filters (fewer groups, shorter date range, "
                        "or a specific level) before making any claim about the data. "
                        "DO NOT fabricate values for what was cut off."
                    ),
                    "preview": serialized[:MAX_RESULT_CHARS - 500],
                })
            return serialized
        except Exception as e:
            logger.error(f"Tool '{tool_name}' execution error: {e}", exc_info=True)
            # Explicitly return a structured error so the LLM can recognize
            # this as "the call failed" rather than "the answer is empty".
            return json.dumps({
                "error": str(e),
                "tool": tool_name,
                "_note": (
                    "This tool call failed. Tell the user the tool errored out "
                    "and what you attempted — do not answer their question with "
                    "fabricated data."
                ),
            })


# Global registry instance
registry = ToolRegistry()
