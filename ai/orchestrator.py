import json
import logging

from ai.config import MAX_TOOL_ITERATIONS, DEFAULT_TEMPERATURE
from ai.providers.base import BaseLLMProvider
from ai.tools.registry import ToolRegistry
from ai.prompts.birdy import get_system_prompt
from ai import session_store

logger = logging.getLogger(__name__)


async def run_chat(
    provider: BaseLLMProvider,
    tool_registry: ToolRegistry,
    db,
    user_id: str,
    message: str,
    session_id: str | None = None,
) -> dict:
    # Restore or create session
    session_id, history = session_store.get_or_create(session_id, user_id)

    # Build messages: system prompt + history + new user message
    if not history:
        history.append({"role": "system", "content": get_system_prompt()})

    history.append({"role": "user", "content": message})

    tools_used = []
    tool_schemas = tool_registry.get_schemas()

    for iteration in range(MAX_TOOL_ITERATIONS + 1):
        response = await provider.chat_completion(
            messages=history,
            tools=tool_schemas if iteration < MAX_TOOL_ITERATIONS else None,
            temperature=DEFAULT_TEMPERATURE,
        )

        if not response.tool_calls:
            reply = response.content or "I wasn't able to generate a response."
            history.append({"role": "assistant", "content": reply})
            session_store.save_messages(session_id, history)
            return {
                "reply": reply,
                "tools_used": tools_used,
                "session_id": session_id,
            }

        # Append assistant message with tool calls
        assistant_msg = {"role": "assistant", "content": response.content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in response.tool_calls
        ]
        history.append(assistant_msg)

        # Execute each tool call
        for tc in response.tool_calls:
            tools_used.append(tc.name)
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
                if not isinstance(args, dict):
                    args = {}
            except (json.JSONDecodeError, TypeError):
                args = {}

            logger.info(f"Tool call: {tc.name} | Args: {args}")

            result = await tool_registry.execute(
                tc.name,
                db=db,
                user_id=user_id,
                **args,
            )

            logger.info(f"Tool result ({tc.name}): {result[:500]}")

            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # Max iterations reached
    reply = response.content or "I ran into complexity limits processing your request. Try a more specific question."
    history.append({"role": "assistant", "content": reply})
    session_store.save_messages(session_id, history)
    return {
        "reply": reply,
        "tools_used": tools_used,
        "session_id": session_id,
    }
