"""
ai/suggestions/composer.py
--------------------------
The "hybrid" half of the engine: turns a deterministically-detected Finding into
human-facing copy.

Grounding contract (this is the whole point):
  * The model is given ONLY the pre-computed numbers as facts.
  * It has NO tools, so it cannot fetch or invent data.
  * It may only rephrase — it never decides whether to pause, and it never
    changes the severity (severity stays exactly what detection computed).
  * On any failure (no key, bad JSON, API error) we fall back to the finding's
    deterministic template copy, so the pipeline always produces a valid result.

Provider resolution uses the app's BYOK factory (each agency's own Anthropic /
OpenAI key) — the same policy as the Birdy chat agent, with no Birdy-global
fallback. When a user hasn't connected a key, composition falls back to the
deterministic template copy.
"""

import json
import logging

from ai.provider_factory import get_provider_for_user, NoAiCredentialError
from ai.suggestions.contracts import Finding

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You write short, punchy copy for a marketing dashboard's AI suggestion cards. "
    "You are given a set of FACTS that were already computed from real ad data. "
    "Rules you must never break:\n"
    "1. Use ONLY the numbers in FACTS. Never invent, round differently, or add any "
    "figure that is not in FACTS.\n"
    "2. Do not change the recommendation or its urgency — only reword it.\n"
    "3. Be concrete and specific; sound like a sharp media buyer, not a chatbot.\n"
    "Return STRICT JSON only: {\"title\": string, \"description\": string}. "
    "title <= 60 characters. description <= 240 characters, 1-2 sentences."
)


async def get_composer_provider(user_id: str, db):
    """
    Resolve the user's own BYOK provider for composing their suggestions — same
    policy as the Birdy chat agent (no Birdy-global fallback). Returns None when
    the user hasn't connected a key, so composition uses the template. Never
    raises.
    """
    try:
        return await get_provider_for_user(user_id, db)
    except NoAiCredentialError:
        return None
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("composer: BYOK provider failed for %s: %s", user_id, e)
        return None


def _template_result(finding: Finding) -> dict:
    return {
        "title": finding.title,
        "description": finding.description,
        "severity": finding.severity,
        "composer": "template",
        "usage": {"in": 0, "out": 0, "model": None},  # no LLM call → nothing to bill
    }


def _facts_block(finding: Finding) -> str:
    """A compact, unambiguous list of the only numbers the model may use."""
    raw = finding.evidence.raw or {}
    lines = [
        f"client: {finding.client_name}",
        f"platform: {finding.platform}",
        f"window: {finding.evidence.window}",
        f"recommendation: {finding.title}",
    ]
    for s in finding.evidence.stats:
        lines.append(f"stat {s.label}: {s.value}")
    for key in ("total_spend", "total_leads", "worst_cpl", "worst_ad", "target", "target_source"):
        if raw.get(key) is not None:
            lines.append(f"{key}: {raw[key]}")
    offenders = raw.get("offenders") or []
    if offenders:
        lines.append(f"ad_count: {len(offenders)}")
        # name up to 3 ads so the model can reference them concretely
        for o in offenders[:3]:
            lines.append(f"ad: {o.get('name')} (cpl={o.get('cpl')}, leads={o.get('leads')}, spend={o.get('spend')})")
    return "\n".join(lines)


def _parse_json(content: str) -> dict | None:
    if not content:
        return None
    text = content.strip()
    # Tolerate ```json ... ``` fences.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("title") and data.get("description"):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return None


async def compose(provider, finding: Finding) -> dict:
    """
    Return {title, description, severity, composer}. Severity is always the
    finding's (deterministic). Falls back to the template on any problem.
    """
    if provider is None:
        return _template_result(finding)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "FACTS:\n" + _facts_block(finding)},
    ]
    try:
        resp = await provider.chat_completion(
            messages=messages,
            tools=None,
            temperature=0.3,
            max_tokens=300,
        )
        usage = {
            "in": getattr(resp.usage, "input_tokens", 0),
            "out": getattr(resp.usage, "output_tokens", 0),
            "model": getattr(provider, "model", None),
        }
        data = _parse_json(resp.content or "")
        if not data:
            logger.info("composer: unparseable LLM output for %s, using template", finding.agent)
            # The model still ran and consumed tokens — bill for them.
            return {**_template_result(finding), "usage": usage}
        return {
            "title": str(data["title"])[:120].strip(),
            "description": str(data["description"]).strip(),
            "severity": finding.severity,  # never LLM-controlled
            "composer": "llm",
            "usage": usage,
        }
    except Exception as e:
        logger.warning("composer: LLM call failed (%s), using template", e)
        return _template_result(finding)
