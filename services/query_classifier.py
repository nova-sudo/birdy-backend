"""
services/query_classifier.py
----------------------------
Lightweight, deterministic classifier that buckets a user's Birdy AI query
into one of the Admin console's request themes (the "top request themes"
clustering on the AI-query analytics view).

v1 is keyword/rule based — cheap, synchronous, no extra API calls. The
resulting category is stored on each user-turn row in `ai_conversation_log`,
so the analytics endpoint is a plain aggregation rather than a live
clustering job. The category set mirrors the design's clusters; upgrading to
embedding/LLM clustering later only changes this function, not the schema.
"""

import re

CATEGORY_PERFORMANCE = "performance_roas"
CATEGORY_PAUSE_SCALE = "pause_scale"
CATEGORY_COST_PER_LEAD = "cost_per_lead"
CATEGORY_LEAD_VOLUME = "lead_volume"
CATEGORY_CRM_SYNC = "crm_sync"
CATEGORY_OTHER = "other"

# Human labels for the analytics UI. Keys are the stored category values.
CATEGORY_LABELS = {
    CATEGORY_PERFORMANCE: "Performance & ROAS",
    CATEGORY_PAUSE_SCALE: "Pause / scale ads",
    CATEGORY_COST_PER_LEAD: "Cost per lead",
    CATEGORY_LEAD_VOLUME: "Lead volume & pacing",
    CATEGORY_CRM_SYNC: "CRM / GHL sync",
    CATEGORY_OTHER: "Other",
}

# Ordered — first match wins, so put the more specific / narrower intents
# ahead of the broad "performance" catch-all (which would otherwise swallow
# pause/scale and CPL questions).
_RULES = [
    (CATEGORY_PAUSE_SCALE,
     r"\b(pause|scale|turn off|switch off|underperform\w*|winning ad|increase (the )?budget|scale up|scale back|budget up)\b"),
    (CATEGORY_COST_PER_LEAD,
     r"\b(cpl|cost[\s-]?per[\s-]?lead|cost[\s-]?per[\s-]?acquisition|cpa|cost[\s-]?per[\s-]?result|cost[\s-]?per[\s-]?booking)\b"),
    (CATEGORY_CRM_SYNC,
     r"\b(ghl|go\s?high\s?level|highlevel|crm|sync\w*|not syncing|pipeline stage|opportunit\w*|contact\w* (not|aren'?t))\b"),
    (CATEGORY_LEAD_VOLUME,
     r"\b(lead volume|how many leads|leads (this|last|so far)|on pace|pacing|hit .*\bleads\b|number of leads|lead count|leads (are we|do we))\b"),
    (CATEGORY_PERFORMANCE,
     r"\b(roas|return on ad spend|performance|performing|ctr|click[\s-]?through|conversion rate|revenue|report\w*|how (is|are|'?s)\b.*\b(doing|performing|going))\b"),
]


def classify_query(text: str | None) -> str:
    """Return the theme category for a user query. Never raises."""
    if not text:
        return CATEGORY_OTHER
    t = text.lower()
    for category, pattern in _RULES:
        if re.search(pattern, t):
            return category
    return CATEGORY_OTHER
