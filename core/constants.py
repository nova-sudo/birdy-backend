# ---------------------------------------------------------------------------
# Meta preset taxonomy
# ---------------------------------------------------------------------------
#
# ONGOING — windows that keep shifting; need to refresh on a cadence (every
# 5h via Vercel cron, or the legacy hourly APScheduler on Azure).
#
# STATIC — windows that only change at period boundaries (1st of month,
# new quarter, new year). Refreshed by the monthly cron on the 1st.
# ---------------------------------------------------------------------------

META_ONGOING_PRESETS = [
    "maximum",
    "today",
    "yesterday",
    "this_week_mon_today",
    "last_7d",
    "last_14d",
    "last_30d",
    "this_month",
    "this_quarter",
    "this_year",
]

META_STATIC_PRESETS = [
    "last_month",
    "last_quarter",
    "last_year",
]

META_CACHE_PRESETS = META_ONGOING_PRESETS + META_STATIC_PRESETS

# Legacy tiered names — kept for backward compat with any callers that
# imported them. New code should use ONGOING / STATIC.
META_PRESETS_FREQUENT = [
    "today",
    "yesterday",
    "this_week_mon_today",
    "last_7d",
]

META_PRESETS_SLOW = [
    "maximum",
    "last_14d",
    "last_30d",
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
]

PRESET_ALIAS = {
    "maximum":             "maximum",
    "data_maximum":        "maximum",
    "today":               "today",
    "yesterday":           "yesterday",
    "this_week":           "this_week_mon_today",
    "this_week_mon_today": "this_week_mon_today",
    "last_7d":             "last_7d",
    "last_14d":            "last_14d",
    "last_30d":            "last_30d",
    "this_month":          "this_month",
    "last_month":          "last_month",
    "this_quarter":        "this_quarter",
    "last_quarter":        "last_quarter",
    "this_year":           "this_year",
    "last_year":           "last_year",
}

GHL_PRESET_DATE_RANGE = {
    "maximum":             None,
    "today":               (0, 0),
    "yesterday":           (1, 1),
    "this_week_mon_today": "this_week_mon",
    "last_7d":             (7, 0),
    "last_14d":            (14, 0),
    "last_30d":            (30, 0),
    "this_month":          "this_month",
    "last_month":          "last_month",
    "this_quarter":        "this_quarter",
    "last_quarter":        "last_quarter",
    "this_year":           "this_year",
    "last_year":           "last_year",
}


# ---------------------------------------------------------------------------
# GHL date-bound helpers (shared by router + caching service)
# ---------------------------------------------------------------------------
from datetime import date as _date, timedelta


def ghl_date_bounds(preset_key: str):
    """
    Convert a preset key to (start_iso, end_iso) date strings (yyyy-mm-dd).
    Returns (None, None) for 'maximum' (all-time).
    """
    today = _date.today()
    spec = GHL_PRESET_DATE_RANGE.get(preset_key)
    if spec is None:
        return None, None
    if isinstance(spec, tuple):
        start_days, end_days = spec
        return (
            (today - timedelta(days=start_days)).isoformat(),
            (today - timedelta(days=end_days)).isoformat(),
        )
    if spec == "this_week_mon":
        s = today - timedelta(days=today.weekday())
        return s.isoformat(), today.isoformat()
    if spec == "this_week_sun":
        s = today - timedelta(days=(today.weekday() + 1) % 7)
        return s.isoformat(), today.isoformat()
    if spec == "this_month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if spec == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.isoformat(), last_prev.isoformat()
    if spec == "this_quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_start_month, day=1).isoformat(), today.isoformat()
    if spec == "last_quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        q_start = today.replace(month=q_start_month, day=1)
        last_q_end = q_start - timedelta(days=1)
        last_q_start_month = ((last_q_end.month - 1) // 3) * 3 + 1
        return last_q_end.replace(month=last_q_start_month, day=1).isoformat(), last_q_end.isoformat()
    if spec == "this_year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if spec == "last_year":
        return today.replace(year=today.year - 1, month=1, day=1).isoformat(), today.replace(year=today.year - 1, month=12, day=31).isoformat()
    return None, None


def ghl_date_bounds_mmddyyyy(preset_key: str):
    """
    Same as ghl_date_bounds but returns dates in mm-dd-yyyy format
    as required by the GHL Opportunities Search API.
    Returns (None, None) for 'maximum'.
    """
    start_iso, end_iso = ghl_date_bounds(preset_key)
    if start_iso is None:
        return None, None
    # Convert yyyy-mm-dd → mm-dd-yyyy
    def _convert(iso_str):
        parts = iso_str.split("-")
        return f"{parts[1]}-{parts[2]}-{parts[0]}"
    return _convert(start_iso), _convert(end_iso)


METRIC_LABELS = {
    # Meta Ads
    "spend":            "Total Spend",
    "impressions":      "Impressions",
    "clicks":           "Clicks",
    "reach":            "Reach",
    "ctr":              "CTR (%)",
    "cpc":              "CPC ($)",
    "cpm":              "CPM ($)",
    "meta_leads":       "Meta Leads",
    "meta_conversion":  "Meta Conversion Rate (%)",
    "cpl":              "Cost Per Lead ($)",
    "cost_per_result":  "Cost Per Result ($)",
    "frequency":        "Ad Frequency",
    # GHL
    "ghl_leads":        "GHL Leads",
    "ghl_conversion":   "GHL Conversion Rate (%)",
    "ghl_revenue":      "GHL Revenue",
    "ghl_won_opps":     "Won Opps",
    "ghl_lost_opps":    "Lost Opps",
    "ghl_open_opps":    "Open Opps",
    "ghl_abandoned_opps": "Abandoned Opps",
    "ghl_total_opps":   "Total Opps",
    # Legacy aliases (kept for backward compat with existing alerts)
    "lead_count":       "Lead Count",
    "conversion_rate":  "Conversion Rate (%)",
}

OPERATOR_LABELS = {
    "gt":       ">",
    "lt":       "<",
    "eq":       "=",
    "neq":      "≠",
    "pct_drop": "↓ %",
    "pct_rise": "↑ %",
}
