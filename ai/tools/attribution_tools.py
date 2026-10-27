"""
Which ads produce the closes: GHL won opportunities traced back to the
campaign / ad set / ad that brought the lead in.

Birdy had no way to answer "what ad is generating all the closes?". The ad
tools carry Meta's numbers only, and the GHL opportunity tools count closes per
client, not per ad. The join already existed for the Marketing Hub's GHL
columns (services/ad_leads.opp_rollup), so this tool reads the same rollup.
"""

from datetime import date, timedelta

from ai.tools.meta_tools import _get_cached_data, _pick_preset
from ai.tools.registry import registry
from core.mongo_client import get_shared_mongo_client
from services.ad_leads import fetch_ad_leads, opp_rollup

TOOL_NAME = "get_closes_by_ad"
DEFAULT_DAYS = 30
MAX_ROWS = 15

_LEVELS = {
    # level: (rollup key, lead id field, lead name field, parent name fields, Meta cache level)
    "ad": ("by_ad", "ad_id", "ad_name", ("adset_name", "campaign_name"), "ads"),
    "adset": ("by_adset", "adset_id", "adset_name", ("campaign_name",), "adsets"),
    "campaign": ("by_campaign", "campaign_id", "campaign_name", (), "campaigns"),
}


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


async def get_closes_by_ad(db, user_id, mongo_client=None, level="ad", start_date=None,
                           end_date=None, group_ids=None, limit=MAX_ROWS, **_):
    if level not in _LEVELS:
        return {"ok": False, "error": f"level must be one of {sorted(_LEVELS)}"}
    rollup_key, id_field, name_field, parent_fields, cache_level = _LEVELS[level]

    end_date = end_date or date.today().isoformat()
    if not start_date:
        try:
            start_date = (date.fromisoformat(end_date) - timedelta(days=DEFAULT_DAYS - 1)).isoformat()
        except ValueError:
            return {"ok": False, "error": "end_date must be YYYY-MM-DD"}
    try:
        limit = max(1, min(int(limit), MAX_ROWS))
    except (TypeError, ValueError):
        limit = MAX_ROWS

    try:
        leads = await fetch_ad_leads(user_id, group_ids or None, start_date, end_date,
                                     mongo_client or get_shared_mongo_client())
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    names = {}
    for lead in leads:
        key = lead.get(id_field)
        if key and key not in names:
            names[key] = {"name": lead.get(name_field) or key,
                          **{f: lead.get(f) for f in parent_fields if lead.get(f)}}

    # Spend from the Meta cache preset nearest the window, for cost per close.
    preset = _pick_preset(start_date, end_date)
    spend = {}
    for row in await _get_cached_data(db, user_id, cache_level, group_ids or None, start_date, end_date) or []:
        if row.get("id"):
            spend[row["id"]] = spend.get(row["id"], 0.0) + _num(row.get("spend"))

    rows = []
    for key, b in opp_rollup(leads)[rollup_key].items():
        row = {"id": key, **names.get(key, {"name": key}),
               "closes": b["won"], "revenue": round(b["revenue"], 2), "leads": b["contacts"],
               "open": b["open"], "lost": b["lost"],
               "close_rate_pct": round(100 * b["won"] / b["contacts"], 1) if b["contacts"] else 0.0}
        if key in spend:
            row["spend"] = round(spend[key], 2)
            if b["won"]:
                row["cost_per_close"] = round(spend[key] / b["won"], 2)
        rows.append(row)
    rows.sort(key=lambda r: (r["closes"], r["revenue"], r["leads"]), reverse=True)

    total_closes = sum(r["closes"] for r in rows)
    return {
        "ok": True,
        "level": level,
        "window": {"start_date": start_date, "end_date": end_date},
        "counting": "Leads that arrived in the window, by the ad that brought them in, and their "
                    "opportunity's current status: a close is a won opportunity.",
        "spend_source": f"Meta cached preset {preset} (nearest to the window)",
        "total_leads": len(leads),
        "total_closes": total_closes,
        "rows": rows[:limit],
        "rows_total": len(rows),
    }


def register_attribution_tools(reg=registry):
    reg.register(
        TOOL_NAME,
        "Which campaigns, ad sets or ads produce the CLOSES (won GHL opportunities), with revenue, "
        "leads, close rate, spend and cost per close, best first. Use for 'which ad is generating the "
        "closes / sales / won deals', 'best ad by revenue', 'cost per close by ad'. Counts leads that "
        f"arrived in the window by the ad that brought them in. Defaults to the last {DEFAULT_DAYS} days "
        "ending today.",
        {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": sorted(_LEVELS), "description": "Default ad"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD; lead arrival window start"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD; defaults to today"},
                "group_ids": {"type": "array", "items": {"type": "string"},
                              "description": "Client group IDs. Omit for all clients."},
                "limit": {"type": "integer", "description": f"Rows to return, max {MAX_ROWS}"},
            },
        },
        get_closes_by_ad,
    )
