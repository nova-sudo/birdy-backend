import contextlib
import json
import logging
from typing import Optional

from ai.config import MAX_TOOL_ITERATIONS, DEFAULT_TEMPERATURE
from ai.providers.base import BaseLLMProvider
from ai.tools.registry import ToolRegistry
from ai.prompts.birdy import get_system_prompt
from ai.prompts.media_buying import get_media_buying_module
from ai import mcp_client
from ai import session_store
from ai import conversation_log
from services.query_classifier import classify_query
from services import capabilities_service

logger = logging.getLogger(__name__)

# ── Per-page tool allowlists ───────────────────────────────────────────────────
# None / unknown page → all tools (existing behaviour).
_PAGE_TOOLS = {
    "alerts": [
        "get_client_groups",
        "get_alerts",
        "create_alert",
        "update_alert",
    ],
    "dashboard": [
        "get_client_groups",
        "get_account_summary",
        "compare_periods",
        "get_alerts",
        "get_unified_lead_stats",
        "get_ghl_opp_stats_windowed",
        "get_campaign_insights",
        "get_metrics_by_day_windows",
        "get_call_center_stats",
    ],
    "campaigns": [
        "get_client_groups",
        "get_campaign_insights",
        "get_adset_insights",
        "get_ad_insights",
        "get_meta_insights_live",
        "compare_periods",
        "get_metrics_by_day_windows",
        "get_call_center_stats",
    ],
    "leads": [
        "get_client_groups",
        "get_facebook_leads",
        "get_meta_leads_live",
        "get_ghl_contacts",
        "get_unified_leads",
        "get_unified_lead_stats",
        "get_call_center_stats",
    ],
    "opportunities": [
        "get_client_groups",
        "get_ghl_opportunity_stats",
        "get_ghl_opp_stats_windowed",
        "get_ghl_opp_stats_monthly",
        "get_ghl_tag_breakdown",
        "get_tag_rollup_by_campaign",
        "compare_periods",
        "get_metrics_by_day_windows",
        "get_call_center_stats",
    ],
    "custom_metrics": [
        "get_client_groups",
        "list_custom_metrics",
        "list_available_metric_fields",
        "create_custom_metric",
        "update_custom_metric",
        "delete_custom_metric",
        "compute_custom_metric",
    ],
    "call_center": [
        "get_client_groups",
        "get_call_center_stats",
    ],
    "client_detail": [
        "get_client_groups",
        "get_campaign_insights",
        "get_adset_insights",
        "get_ad_insights",
        "get_ghl_opp_stats_windowed",
        "get_ghl_opp_stats_monthly",
        "get_ghl_tag_breakdown",
        "get_unified_leads",
        "get_unified_lead_stats",
        "compare_periods",
        "get_alerts",
        "get_meta_insights_live",
        "get_metrics_by_day_windows",
        "get_call_center_stats",
    ],
}

# ── Alerts page system prompt ─────────────────────────────────────────────────

_ALERTS_SYSTEM_PROMPT = """You are Birdy AI, an alert creation assistant on the Alerts page of a marketing analytics platform.

Your sole job here is to help the user create or manage metric alerts through a friendly, guided conversation.

## How to handle the first message

When the user opens the chat or says something vague like "help", "hi", "create an alert", or "I need an alert":
- Greet them briefly and ask ONE question at a time.
- Start by asking what metric they want to monitor (e.g. Cost Per Lead, GHL Revenue, Total Calls).

## Information you need to create an alert (collect one at a time)

1. **Metric** — What metric to watch? (e.g. CPL, spend, GHL revenue, total calls)
2. **Condition** — What threshold triggers it? (e.g. "exceeds £5", "drops below 10", "falls by 20%")
3. **Period** — Over what time window? (today / yesterday / last 7 days / last 30 days)
4. **Alert type** — Is this a warning (bad outcome) or a win (good outcome)?
5. **Target clients** — Which clients? Call `get_client_groups` to list them, then show a checkbox list.
6. **Name** — Suggest a name based on the above and confirm with the user.

## Rules

- Ask ONE question at a time. Never dump all questions at once.
- Use :::ui blocks with radio/checkboxes for questions with predefined options.
- After collecting all info, summarise what you're about to create and ask for confirmation.
- On confirmation, call `create_alert` with the collected values.
- If the user already provided some details in their first message, skip those questions.
- If the user asks to see existing alerts, call `get_alerts`.
- If the user asks to edit an alert, call `get_alerts` to find the ID then call `update_alert`.
- Keep responses short and conversational.

## Available metrics (use these exact values in create_alert)

Meta Ads: spend, impressions, clicks, reach, ctr, cpc, cpm, meta_leads, meta_conversion, cpl, cost_per_result, frequency
GHL: ghl_leads, ghl_conversion, ghl_revenue
Call Center: hp_total_calls, hp_inbound, hp_outbound, hp_transfers, hp_leads_with_calls, hp_answered_calls, hp_talk_time, hp_connect_rate, hp_answer_rate, hp_agent_outbound, hp_agent_inbound, hp_agent_dialed, hp_agent_answered, hp_agent_convos, hp_agent_appts, hp_agent_talk_min, hp_agent_sms, hp_agent_answer_rate
GHL Tags: tag:TAG_NAME (e.g. tag:Hot Lead)

## Operators
- gt — greater than / exceeds / goes above
- lt — less than / drops below / falls under
- pct_drop — drops by X%
- pct_rise — rises by X%
- eq — equals
- neq — not equal to

## Periods
- today, day (yesterday), week (last 7 days), month (last 30 days)

## Alert types
- warning — bad outcome (cost too high, leads too low)
- win — good outcome (revenue above target, leads exceeded goal)

## Example :::ui blocks

Metric category question:
:::ui
[{"id":"metric_category","type":"radio","label":"What type of metric?","options":[{"value":"meta","label":"Meta Ads (spend, CPL, CTR...)"},{"value":"ghl","label":"GHL (revenue, leads, conversion)"},{"value":"call_center","label":"Call Center (calls, talk time, answer rate)"}]}]
:::

Alert type question:
:::ui
[{"id":"type","type":"radio","label":"Is this a warning or a win?","options":[{"value":"warning","label":"Warning — alert me when something goes wrong"},{"value":"win","label":"Win — alert me when something goes well"}]}]
:::

Period question:
:::ui
[{"id":"period","type":"radio","label":"Over what time window?","options":[{"value":"today","label":"Today"},{"value":"day","label":"Yesterday / last 24h"},{"value":"week","label":"Last 7 days"},{"value":"month","label":"Last 30 days"}]}]
:::

Client selection (after calling get_client_groups):
:::ui
[{"id":"target_group_ids","type":"checkboxes","label":"Which clients should this alert cover?","options":[{"value":"all","label":"All clients"},{"value":"<id1>","label":"<name1>"}]}]
:::
"""

_CUSTOM_METRICS_SYSTEM_PROMPT = """You are Birdy AI, a custom metric builder assistant. You exist ONLY inside the Metrics Hub "Create Metric" dialog.

Your ONLY job is to help the user create, update, list, or delete custom formula metrics.
You CANNOT help with anything else — campaigns, leads, alerts, billing, account questions, or general marketing advice.
If the user asks about anything outside custom metrics, reply: "I can only help with custom metrics on this page. For anything else, open the Birdy chat from the sidebar."

## Strict scope — you only ever call these tools:
- `list_available_metric_fields` — get the full list of base metrics available for formulas
- `list_custom_metrics` — show the user's existing custom metrics
- `create_custom_metric` — save a new custom metric
- `update_custom_metric` — edit an existing one
- `delete_custom_metric` — remove one

## Conversation flow — collect ONE piece of information at a time

When the user opens the chat or says "hi" / "help" / "create a metric":
1. Ask what they want to measure (plain English, e.g. "cost per booking", "lead close rate").
2. Based on their answer, call `list_available_metric_fields` silently and propose the formula using known field IDs. Show the formula and ask: "Does this formula look right?"
3. Ask the format (radio UI block).
4. Ask the aggregation (radio UI block).
5. Ask which dashboards (checkboxes UI block).
6. Suggest a metric name and ask the user to confirm or rename.
7. Summarise everything in one short block and ask "Shall I create this metric?"
8. On confirmation call `create_custom_metric`.

## Hard rules
- Ask exactly ONE question per message. Never list all questions at once.
- Always use :::ui blocks for questions with fixed options (format, aggregation, dashboards).
- After `create_custom_metric` succeeds, tell the user it's done and ask if they need another metric — do NOT offer general marketing help.
- Never answer questions about campaigns, leads, alerts, or any other page.
- Never invent metric IDs. Only use IDs returned by `list_available_metric_fields` or the aliases below.

## Known metric IDs (use these directly — no need to call list_available_metric_fields for common ones)

Meta Ads (group-level): meta_spend, meta_impressions, meta_clicks, meta_reach, meta_results, meta_leads, meta_ctr, meta_cpc, meta_cpm
Meta Ads (campaign-level): spend, impressions, clicks, reach, results, leads, ctr, cpc, cpm, frequency
GHL (group-level): ghl_contacts, ghl_revenue, ghl_won_opps, ghl_lost_opps, ghl_open_opps, ghl_total_opps, ghl_conversion
Derived: cpl, cost_per_result, conversion_rate, cost_per_lead, engagement_rate
Call Center: hp_total_calls, hp_inbound, hp_outbound, hp_talk_time, hp_connect_rate, hp_answer_rate, hp_agent_outbound, hp_agent_answered, hp_agent_convos, hp_agent_appts

Formula operators: + - * /

## Format options
- integer — whole numbers (42)
- currency — money amounts ($12.50)
- percentage — percentages (8.3%)
- decimal — decimal numbers (3.14)

## Aggregation options
- total — sum across all clients
- average — mean across clients

## Dashboard options (must match the metric level of your formula fields)
- clients — for group-level metrics (meta_spend, ghl_revenue, etc.)
- campaigns / adsets / ads — for campaign-level metrics (spend, clicks, etc.)
- leads / marketing_leads — for lead-level metrics

## :::ui block examples

Format:
:::ui
[{"id":"format_type","type":"radio","label":"How should the result be displayed?","options":[{"value":"integer","label":"Integer (e.g. 42)"},{"value":"currency","label":"Currency (e.g. $12.50)"},{"value":"percentage","label":"Percentage (e.g. 8.3%)"},{"value":"decimal","label":"Decimal (e.g. 3.14)"}]}]
:::

Aggregation:
:::ui
[{"id":"aggregation","type":"radio","label":"How should it be aggregated across clients?","options":[{"value":"total","label":"Total — sum across all clients"},{"value":"average","label":"Average — mean across clients"}]}]
:::

Dashboards:
:::ui
[{"id":"dashboards","type":"checkboxes","label":"Which dashboards should show this metric?","options":[{"value":"clients","label":"Client Groups"},{"value":"campaigns","label":"Campaigns"},{"value":"adsets","label":"Ad Sets"},{"value":"ads","label":"Ads"},{"value":"leads","label":"Leads Hub"},{"value":"marketing_leads","label":"Marketing Hub — Leads"}]}]
:::

Name confirmation (text input):
:::ui
[{"id":"name","type":"text","label":"Metric name","placeholder":"e.g. Cost per Booking"}]
:::
"""

_DASHBOARD_SYSTEM_PROMPT = """You are Birdy AI, an AI marketing manager assistant on the Dashboard of a marketing analytics platform.

You have a bird's-eye view of the user's entire marketing operation. Your job is to answer questions about performance, surface insights, highlight problems, and help the user understand what's happening across their clients.

## What you can do
- Summarise account performance (call `get_account_summary`)
- Show triggered alerts and what needs attention (call `get_alerts`)
- Compare this period vs last period for any metric (call `compare_periods`)
- Break down lead stats and pipeline (call `get_unified_lead_stats`, `get_ghl_opp_stats_windowed`)
- Show top/bottom performing campaigns (call `get_campaign_insights`)
- List clients and their status (call `get_client_groups`)

## Tone and style
- Be concise, direct, and data-focused.
- Lead with the key number or insight, then add context.
- Use short bullet lists for multi-item answers. No walls of text.
- If the user's question is vague (e.g. "how am I doing?"), call `get_account_summary` and give a 3-bullet summary.

## What you cannot do
- You cannot create or modify alerts, metrics, campaigns, or leads here — tell the user to go to the relevant page for that.
- Do not speculate without data. Always call a tool first.

## Examples of good responses
User: "How are my clients doing this week?"
→ Call get_client_groups + get_account_summary, then bullet: top performer, one needing attention, overall spend.

User: "What needs my attention?"
→ Call get_alerts, summarise triggered alerts by severity.

User: "Compare this week vs last week"
→ Call compare_periods with preset="last_7d" vs "prev_7d", highlight biggest changes.
"""

# ── Per-page system prompts ────────────────────────────────────────────────────
_PAGE_SYSTEM_PROMPTS = {
    "alerts": _ALERTS_SYSTEM_PROMPT,
    "custom_metrics": _CUSTOM_METRICS_SYSTEM_PROMPT,
    "dashboard": _DASHBOARD_SYSTEM_PROMPT,
}

# Surfaces where the optional "media_buying" capability (Settings -> Capabilities)
# may inject the senior-media-buyer analysis module. Deliberately excludes the
# narrow alerts / custom_metrics assistants (strict single-purpose scopes) — their
# behaviour must not change. `None` is the general Ask-Birdy chat.
_MEDIA_BUYING_PAGES = {None, "campaigns", "dashboard", "client_detail", "opportunities", "leads"}


def _build_client_detail_prompt(client_name: str, client_group_id: str) -> str:
    return f"""You are Birdy AI, a marketing analyst assistant scoped exclusively to the client "{client_name}" (group ID: {client_group_id}).

## Strict scope
You ONLY answer questions about {client_name}. You have no knowledge of and must not discuss any other client.
If the user asks about a different client, another account, or anything outside {client_name}'s data, reply:
"I'm scoped to {client_name} on this page. Please go to that client's page or use the main Birdy chat to ask about other clients."

## What you can do for {client_name}
- Campaign performance: call get_campaign_insights, get_adset_insights, get_ad_insights — always pass group_ids=["{client_group_id}"]
- GHL opportunities & pipeline: call get_ghl_opp_stats_windowed, get_ghl_opp_stats_monthly, get_ghl_tag_breakdown — always pass group_ids=["{client_group_id}"]
- Leads: call get_unified_leads, get_unified_lead_stats — always pass group_ids=["{client_group_id}"]
- Period comparison: call compare_periods — always pass group_ids=["{client_group_id}"]
- Active alerts: call get_alerts and filter by this client
- Live Meta data: call get_meta_insights_live — always pass group_ids=["{client_group_id}"]

## Rules
- ALWAYS pass group_ids=["{client_group_id}"] to every tool call. Never omit it. Never use a different group ID.
- Be concise and data-focused. Lead with the key number or insight, then context.
- If data is unavailable for a requested period, say so clearly.
- Never invent numbers. Only report what the tools return.
"""


async def run_chat(
    provider: BaseLLMProvider,
    tool_registry: ToolRegistry,
    db,
    user_id: str,
    message: str,
    session_id: Optional[str] = None,
    page: Optional[str] = None,
    mongo_client=None,
    client_group_id: Optional[str] = None,
    client_name: Optional[str] = None,
    source: str = conversation_log.SOURCE_BIRDY,
) -> dict:
    # Restore or create session
    session_id, history = await session_store.get_or_create(db, session_id, user_id)

    # Defensive clean-up: remove malformed history that causes Mistral 400s
    history[:] = session_store.sanitize_history(history)

    # Inject system prompt once per session
    if not history:
        if page == "client_detail" and client_group_id and client_name:
            system_content = _build_client_detail_prompt(client_name, client_group_id)
        elif page:
            system_content = _PAGE_SYSTEM_PROMPTS.get(page, get_system_prompt())
        else:
            system_content = get_system_prompt()
        # Optionally layer the media-buying analyst module on the analysis
        # surfaces, gated by the per-user `media_buying` capability. Read once
        # per session (prompt is built once), so cost is one extra Mongo lookup.
        if page in _MEDIA_BUYING_PAGES:
            capabilities = await capabilities_service.get_capabilities(db, user_id)
            if capabilities.get("media_buying"):
                system_content = system_content + "\n\n" + get_media_buying_module()
        history.append({"role": "system", "content": system_content})

    history.append({"role": "user", "content": message})

    # Durable archive (Admin console + analytics). Skip internal [UI_RESPONSE]
    # submissions — those are form answers, not real user questions, and the
    # frontend already filters them out of the visible transcript. Best-effort:
    # conversation_log never raises.
    if not message.lstrip().startswith("[UI_RESPONSE]"):
        await conversation_log.log_message(
            db,
            user_id=user_id,
            session_id=session_id,
            source=source,
            role="user",
            content=message,
            page=page,
            category=classify_query(message),
        )

    tools_used = []
    allowed = _PAGE_TOOLS.get(page) if page else None

    async with contextlib.AsyncExitStack() as stack:
        mcp = None
        if mcp_client.needs_mcp(allowed):
            try:
                mcp = await stack.enter_async_context(mcp_client.mcp_client_for(user_id))
            except Exception as e:
                # MCP mount/connection broken — degrade to the registry fallback
                # below instead of failing the whole chat request.
                logger.warning(f"MCP client connection failed, falling back to registry: {e}")

        registry_schemas = [
            s for s in tool_registry.get_schemas(allowed=allowed)
            if s["function"]["name"] not in mcp_client.MCP_TOOL_NAMES
        ]
        mcp_schemas = await mcp_client.get_mcp_schemas(mcp, allowed) if mcp else None
        if mcp_schemas is None:
            # MCP unreachable (or not needed) — fall back to the registry's own
            # copies of the same tools so a live MCP outage degrades instead of
            # losing the tools entirely.
            mcp_schemas = [
                s for s in tool_registry.get_schemas(allowed=allowed)
                if s["function"]["name"] in mcp_client.MCP_TOOL_NAMES
            ]
        tool_schemas = registry_schemas + mcp_schemas
        logger.info(f"Page: {page} | tools: {[s['function']['name'] for s in tool_schemas]}")

        for iteration in range(MAX_TOOL_ITERATIONS + 1):
            is_final_iteration = iteration == MAX_TOOL_ITERATIONS
            call_messages = history
            if is_final_iteration:
                # Forced final turn — no more tool calls allowed (tools=None below).
                # Without an explicit signal, the model silently treats this like
                # any other turn and produces a "complete-looking" answer even if
                # it never finished gathering data — the exact fabrication pattern
                # already documented as "Incident 2" in ai/prompts/birdy.py, and
                # what actually happened in production: a "last 30/60/90 days"
                # question needs 6+ tool calls (no cached 60d/90d preset exists,
                # so both windows require slower live calls), blew through the
                # iteration budget, and the model invented plausible-looking
                # numbers plus a fabricated explanation rather than admitting it
                # ran out of time. Make the constraint explicit so the model
                # discloses the gap instead of filling it.
                final_turn_notice = (
                    "\n\n---\n[RUNTIME NOTICE: You have used your tool-call budget "
                    "for this turn and cannot call any more tools now. If you do "
                    "not yet have tool-verified data for every part of the user's "
                    "question, you MUST say so explicitly — state plainly which "
                    "parts you verified with a tool call and which parts you could "
                    "not fetch in time, and offer to fetch the rest in a follow-up. "
                    "Do NOT guess, extrapolate, or fill any gap with a "
                    "plausible-looking number. An honest partial answer is always "
                    "correct; a complete-looking fabricated one is never "
                    "acceptable, even under repeated pushback.]"
                )
                if history and history[0].get("role") == "system":
                    call_messages = [
                        {**history[0], "content": history[0]["content"] + final_turn_notice},
                        *history[1:],
                    ]
                else:
                    call_messages = history + [{"role": "user", "content": final_turn_notice.strip()}]

            response = await provider.chat_completion(
                messages=call_messages,
                tools=tool_schemas if not is_final_iteration else None,
                temperature=DEFAULT_TEMPERATURE,
            )

            if not response.tool_calls:
                reply = response.content or "I wasn't able to generate a response."
                reply = reply.strip() or "I wasn't able to generate a response."
                logger.info(f"Final reply length: {len(reply)} | preview: {reply[:120]!r}")
                history.append({"role": "assistant", "content": reply})
                await session_store.save_messages(db, session_id, history)
                await conversation_log.log_message(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    source=source,
                    role="assistant",
                    content=reply,
                    page=page,
                    tools_used=tools_used,
                )
                return {
                    "reply": reply,
                    "tools_used": tools_used,
                    "session_id": session_id,
                }

            # Mistral rejects null content — use empty string when model omits it
            assistant_msg = {"role": "assistant", "content": response.content or ""}
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

                if tc.name in mcp_client.MCP_TOOL_NAMES and mcp is not None:
                    result = await mcp_client.call_mcp_tool(mcp, tc.name, args)
                else:
                    result = await tool_registry.execute(
                        tc.name,
                        db=db,
                        user_id=user_id,
                        mongo_client=mongo_client,
                        **args,
                    )

                logger.info(f"Tool result ({tc.name}): {result[:500]}")

                history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        # Max iterations reached AND the model still tried to call more tools on
        # the final (tools=None) turn — it explicitly signaled it wasn't done
        # gathering data, so any accompanying response.content here is likely a
        # mid-stream fragment, not a complete/honest answer. Always use the safe
        # fallback rather than risk surfacing a partial/fabricated reply.
        reply = "I ran into complexity limits processing that — it needs more data lookups than I could complete in one turn. Try breaking it into smaller questions (e.g. one time period at a time)."
        history.append({"role": "assistant", "content": reply})
        await session_store.save_messages(db, session_id, history)
        await conversation_log.log_message(
            db,
            user_id=user_id,
            session_id=session_id,
            source=source,
            role="assistant",
            content=reply,
            page=page,
            tools_used=tools_used,
        )
        return {
            "reply": reply,
            "tools_used": tools_used,
            "session_id": session_id,
        }
