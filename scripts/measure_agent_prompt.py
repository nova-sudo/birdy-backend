"""Measure what one Birdy team reply sends to the model, offline.

    python scripts/measure_agent_prompt.py            # the size tables
    python scripts/measure_agent_prompt.py --json     # the same, as JSON

Runs the real `run_chat` (ai/orchestrator.py) for each agent's own chat (Birdy, Marketing Guru, Sales Coach)
and for every nested run a reply can make (Birdy asking Marketing Guru and Sales Coach, each of them asking
Birdy), with a scripted provider that records the first model call of each run and answers at once. So
the system prompt, tool schemas and history measured are exactly what turn one sends. Nothing leaves
the process: the database is in-memory (mongomock, empty: no lessons or Context cards saved), MCP tool
schemas come from the in-process FastMCP server, and no model is called.

Tokens are estimated as characters / 4 (no tokenizer is a dependency). Tool schemas are measured as
compact JSON, which is close to what the provider renders.
"""
import asyncio
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

USER = "measure@example.com"
GROUP = "grp-measure"


def tokens(chars: int) -> int:
    return round(chars / 4)


def _role(messages) -> tuple[str, str | None]:
    """(agent, asked_by or None) from a call's system prompt."""
    system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
    agent = next((k for k, m in (("birdy", "Your role: Birdy"), ("media_buyer", "Your role: Marketing Guru"),
                                 ("sales_coach", "Your role: Sales Coach")) if m in system), "other")
    asked_by = None
    if "## You were asked by another agent" in system:
        asked_by = system.split("## You were asked by another agent\n", 1)[1].split(" asked you", 1)[0]
    return agent, asked_by


async def measure() -> list[dict]:
    """The size rows, one per run. Patches the Mongo singleton and the MCP client while it runs, and
    puts both back."""
    import core.mongo_client as core_mongo
    from ai import mcp_client
    saved = core_mongo._client, mcp_client.mcp_client_for
    try:
        return await _measure()
    finally:
        core_mongo._client, mcp_client.mcp_client_for = saved


async def _measure() -> list[dict]:
    from mongomock_motor import AsyncMongoMockClient

    import core.mongo_client as core_mongo
    from ai import mcp_client
    from ai.orchestrator import run_chat
    from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall, Usage
    from ai.team import definitions as defs
    from ai.tools.registry import registry
    import routers.chat  # noqa: F401  registers every tool, as the app does
    from ai.mcp import mcp
    from core.database import DB_NAME
    from fastmcp import Client

    client = AsyncMongoMockClient()
    core_mongo._client = client
    db = client[DB_NAME]
    await db["client_groups"].insert_one({"user_id": USER, "id": GROUP, "name": "Measure Co"})

    @contextlib.asynccontextmanager
    async def in_process_mcp(user_id):
        async with Client(mcp) as c:     # in-memory transport: the same tool list, no auth round trip
            yield c
    mcp_client.mcp_client_for = in_process_mcp

    captured: dict[tuple, dict] = {}

    def ask(name):
        return ProviderResponse(content=None, tool_calls=[ToolCall(id="tc-" + name, name=name, arguments=json.dumps(
            {"question": "How did last week go?", "client": "agency"}))], finish_reason="tool_calls",
            usage=Usage(0, 0))

    class Recorder(BaseLLMProvider):
        model = "gpt-4o"

        def __init__(self, first: dict):
            self.first = dict(first)

        async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
            key = _role(messages)
            if key not in captured:
                captured[key] = {"messages": [dict(m) for m in messages], "tools": list(tools or [])}
            nxt = self.first.pop(key, None)
            return nxt or ProviderResponse(content="Done.", usage=Usage(0, 0))

    script = {("birdy", None): ask("ask_media_buyer"),
              ("media_buyer", None): ask("ask_birdy"),
              ("sales_coach", None): ask("ask_birdy")}
    for agent in defs.AGENT_KEYS:
        await run_chat(provider=Recorder(script), tool_registry=registry, db=db, user_id=USER,
                       message="How did last week go?", agent=agent)
    # Birdy asking Sales Coach (a second nested shape)
    await run_chat(provider=Recorder({("birdy", None): ask("ask_sales_coach")}), tool_registry=registry, db=db,
                   user_id=USER, message="How did the calls go?", agent="birdy")

    rows = []
    for (agent, asked_by), call in captured.items():
        if agent == "other":
            continue
        system = call["messages"][0]["content"]
        parts = await defs.instruction_parts(agent, db, USER, client_group_id=None, client_name=None,
                                             asked_by=asked_by, source=None)
        rebuilt = "".join(t for _, t in parts)
        assert rebuilt == system, f"instruction_parts drifted from what {agent} was sent"
        tools = sorted(((t["function"]["name"], len(json.dumps(t, separators=(",", ":")))) for t in call["tools"]),
                       key=lambda x: -x[1])
        history = sum(len(json.dumps(m.get("content"))) for m in call["messages"][1:])
        rows.append({
            "run": f"{defs.NAMES[agent]}" + (f" (asked by {asked_by})" if asked_by else " (own chat)"),
            "system_chars": len(system),
            "parts": [{"part": n, "chars": len(t)} for n, t in parts],
            "tools": [{"tool": n, "chars": c} for n, c in tools],
            "tools_chars": sum(c for _, c in tools),
            "history_chars": history,
        })
    rows.sort(key=lambda r: r["run"])
    return rows


def report(rows: list[dict]) -> str:
    out = ["Estimated tokens = characters / 4 (no tokenizer installed).", ""]
    out.append(f"{'run':38} {'system':>8} {'tools':>8} {'#tools':>6} {'history':>8} {'total':>8} {'~tokens':>8}")
    for r in rows:
        total = r["system_chars"] + r["tools_chars"] + r["history_chars"]
        out.append(f"{r['run']:38} {r['system_chars']:>8} {r['tools_chars']:>8} {len(r['tools']):>6} "
                   f"{r['history_chars']:>8} {total:>8} {tokens(total):>8}")
    for r in rows:
        out += ["", f"== {r['run']}", "  system prompt parts:"]
        out += [f"    {p['part']:24} {p['chars']:>7} chars  ~{tokens(p['chars']):>5} tok" for p in r["parts"]]
        out.append(f"  tool schemas ({len(r['tools'])}), largest first:")
        out += [f"    {t['tool']:34} {t['chars']:>6} chars  ~{tokens(t['chars']):>5} tok" for t in r["tools"]]
        out.append(f"  history (turn one: the question)  {r['history_chars']} chars")
    every = sorted([(p["part"] + " / " + r["run"], p["chars"]) for r in rows for p in r["parts"]]
                   + [("tool " + t["tool"] + " / " + r["run"], t["chars"]) for r in rows for t in r["tools"]],
                   key=lambda x: -x[1])
    out += ["", "Largest single parts:"] + [f"  {n:70} {c:>7}" for n, c in every[:10]]
    return "\n".join(out)


def main():
    import logging
    import warnings
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    rows = asyncio.run(measure())
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
    else:
        print(report(rows))


if __name__ == "__main__":
    main()
