"""What a team reply sends to the model, trimmed (ai/prompts/birdy.py, ai/team/definitions.py,
ai/orchestrator.py) and cached (ai/providers): the general chat's prompt is unchanged, an agent only
gets the guide lines for its own tools, a delegated run gets no screen blocks, no tool is sent twice,
the instructions earlier fixes added are all still there, and the providers cache the fixed prefix.
No model or API calls: the provider clients are fakes."""

import contextlib
import importlib.util
import json
import os
from types import SimpleNamespace

from ai.prompts.birdy import get_system_prompt
from ai.team import definitions as defs
from tests.test_agent_team import TeamProvider, U, call, chat, isolate, registry, say  # noqa: F401


def _agent_prompt(agent, nested):
    return get_system_prompt(defs.tool_names(agent, nested=nested), for_screen=not nested)


def test_the_general_chat_prompt_is_unchanged():
    full = get_system_prompt()
    assert "⟦" not in full
    # every guide line is still there for the page and general chats
    for text in ("`get_call_center_stats` —", "**CRITICAL — analyzing call recordings", "## Interactive UI Blocks",
                 "## Status blocks", "## Showing numbers visually", "**Alert Metrics:**", "`get_ghl_opp_stats_monthly` —"):
        assert text in full


def test_an_agent_gets_only_its_own_tools_guide():
    birdy = _agent_prompt(defs.BIRDY, False)
    assert "⟦" not in birdy
    # Birdy has no call or ad tools: no guide to them (it asks the owning agent)
    for gone in ("`get_call_center_stats` —", "analyze_call_recordings` —", "`get_campaign_insights` —",
                 "**CRITICAL — analyzing call recordings", "**Preset Mapping for compare_periods**"):
        assert gone not in birdy
    # its own settings tools keep their guide
    for kept in ("`create_alert` —", "**Alert Metrics:**", "When creating a metric, pick a sensible", "`get_account_summary` —"):
        assert kept in birdy
    mb = _agent_prompt(defs.MEDIA_BUYER, False)
    assert "`get_campaign_insights` —" in mb and "**Preset Mapping for compare_periods**" in mb
    assert "`get_call_center_stats` —" not in mb and "**Alert Metrics:**" not in mb


def test_the_anti_fabrication_rules_stay_for_every_agent():
    for agent in (defs.BIRDY, defs.MEDIA_BUYER):
        for nested in (False, True):
            text = _agent_prompt(agent, nested)
            for rule in ("## ⛔ ANTI-HALLUCINATION", "NEVER SYNTHESIZE A DISTRIBUTION FROM A TOTAL",
                         "CONCRETE PATTERNS FROM REAL INCIDENTS", "Incident 5", "Tool failure handling",
                         "## Formatting Rules (STRICT)"):
                assert rule in text, (agent, nested, rule)


def test_a_delegated_run_has_no_screen_blocks():
    for agent in (defs.BIRDY, defs.MEDIA_BUYER):
        nested = _agent_prompt(agent, True)
        own = _agent_prompt(agent, False)
        for block in ("## Interactive UI Blocks", "## Status blocks", "## Showing numbers visually"):
            assert block not in nested and block in own


async def test_the_team_instructions_earlier_fixes_added_are_kept(mock_db):
    birdy = await defs.build_instructions(defs.BIRDY, mock_db, U, client_group_id=None, client_name=None,
                                          asked_by=None)
    assert "always `ask_sales_coach`" in birdy and "always `ask_media_buyer`" in birdy
    assert "do not repeat earlier figures as a fresh answer" in birdy
    assert "call\n`recall_conversations` first" in birdy
    for agent in defs.MAIN_AGENTS:
        text = await defs.build_instructions(agent, mock_db, U, client_group_id=None, client_name=None, asked_by=None)
        assert "do not interview them" in text and "call `propose_sub_agent` straight away" in text
    parts = await defs.instruction_parts(defs.MEDIA_BUYER, mock_db, U, client_group_id=None, client_name=None,
                                         asked_by="Birdy")
    names = [n for n, _ in parts]
    # the stable part first, so the provider's prompt cache keeps hitting; per-agency and per-day text last
    assert names[:3] == ["base prompt", "team notes", "role prompt"] and names[-1] == "date"
    assert names.index("client scope") > names.index("media buying module")


async def test_no_tool_is_sent_twice(mock_db, registry, monkeypatch):
    """A tool on both the registry and the MCP server is sent once, as the registry's copy (which runs)."""
    from ai import mcp_client
    dup = {"type": "function", "function": {"name": "get_call_center_stats", "description": "mcp copy",
                                            "parameters": {"type": "object", "properties": {}}}}
    hosted = {"type": "function", "function": {"name": "get_client", "description": "mcp only",
                                               "parameters": {"type": "object", "properties": {}}}}

    @contextlib.asynccontextmanager
    async def fake_mcp(user_id):
        yield object()

    async def schemas(client, allowed):
        return [s for s in (dup, hosted) if allowed is None or s["function"]["name"] in allowed]
    monkeypatch.setattr(mcp_client, "needs_mcp", lambda allowed: True)
    monkeypatch.setattr(mcp_client, "mcp_client_for", fake_mcp)
    monkeypatch.setattr(mcp_client, "get_mcp_schemas", schemas)
    p = TeamProvider({"sales_coach": [say("Fine.")], "birdy": [say("Fine.")]})
    calls = []
    orig = p.chat_completion

    async def spy(messages, tools=None, **kw):
        calls.append(tools or [])
        return await orig(messages, tools=tools, **kw)
    p.chat_completion = spy
    await chat(p, registry, mock_db, agent="sales_coach")
    await chat(p, registry, mock_db, agent="birdy")
    for tools in calls:
        names = [t["function"]["name"] for t in tools]
        assert len(names) == len(set(names))
    sc_tools = {t["function"]["name"]: t for t in calls[0]}
    assert sc_tools["get_call_center_stats"]["function"]["description"] == "stub"      # the registry's
    assert "get_client" in {t["function"]["name"] for t in calls[1]}                    # MCP-only tools stay


# ── providers: the fixed prefix is cached ────────────────────────────────────

def _openai_response(cached=0):
    msg = SimpleNamespace(content="hi", tool_calls=None)
    usage = SimpleNamespace(prompt_tokens=5000, completion_tokens=20,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cached))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


async def test_openai_calls_share_a_cache_key_per_prefix():
    from ai.providers.openai_provider import OpenAIProvider
    sent = []

    async def create(**kwargs):
        sent.append(kwargs)
        return _openai_response(cached=4096)
    p = OpenAIProvider(api_key="test", model="gpt-4o")
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    tools = [{"type": "function", "function": {"name": "a", "description": "", "parameters": {}}}]
    sys_msg = {"role": "system", "content": "You are Birdy."}
    r = await p.chat_completion([sys_msg, {"role": "user", "content": "one"}], tools=tools)
    await p.chat_completion([sys_msg, {"role": "user", "content": "one"}, {"role": "assistant", "content": "x"},
                             {"role": "user", "content": "two"}], tools=tools)
    await p.chat_completion([{"role": "system", "content": "Other agent."}, {"role": "user", "content": "one"}],
                            tools=tools)
    keys = [k["extra_body"]["prompt_cache_key"] for k in sent]
    assert keys[0] == keys[1] != keys[2]
    # credits stay metered on every prompt token; the cached share is reported beside it
    assert r.usage.input_tokens == 5000 and r.usage.cached_input_tokens == 4096


async def test_anthropic_marks_tools_and_system_for_caching():
    from ai.providers.anthropic_provider import AnthropicProvider
    sent = []

    async def create(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10,
                                                     cache_read_input_tokens=3000, cache_creation_input_tokens=0))
    p = AnthropicProvider(api_key="test", model="claude-sonnet-5")
    p.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    tools = [{"type": "function", "function": {"name": n, "description": "d", "parameters": {"type": "object"}}}
             for n in ("a", "b")]
    r = await p.chat_completion([{"role": "system", "content": "You are Birdy."}, {"role": "user", "content": "hi"}],
                                tools=tools)
    kw = sent[0]
    assert kw["system"] == [{"type": "text", "text": "You are Birdy.", "cache_control": {"type": "ephemeral"}}]
    assert "cache_control" not in kw["tools"][0] and kw["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert r.usage.input_tokens == 3100 and r.usage.cached_input_tokens == 3000


# ── the measuring script ─────────────────────────────────────────────────────

async def test_the_measure_script_runs_offline():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts",
                        "measure_agent_prompt.py")
    spec = importlib.util.spec_from_file_location("measure_agent_prompt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = await mod.measure()
    runs = {r["run"] for r in rows}
    assert {"Birdy (own chat)", "Media Buyer (own chat)", "Sales Coach (own chat)",
            "Media Buyer (asked by Birdy)", "Sales Coach (asked by Birdy)", "Birdy (asked by Media Buyer)"} <= runs
    for r in rows:
        names = [t["tool"] for t in r["tools"]]
        assert len(names) == len(set(names)), r["run"]
    assert "Birdy (own chat)" in mod.report(rows) and json.dumps(rows)
