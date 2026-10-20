from datetime import date as _date, datetime as _datetime, timedelta

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

# The window immediately before each preset, for dashkit's `compare: 'previous'`.
# Only targets that are themselves cached presets (PRESET_ALIAS keys) are
# listed: the caches store one bucket per canonical preset. The rolling
# windows (last_7d / last_14d / last_30d) have no "previous N days" bucket;
# for Meta-backed metrics (daily rows in meta_daily_spend)
# services/dashboards_resolve computes their previous window from the daily
# rows instead, and for everything else they have no `previous`. this_week has no last_week bucket, so no `previous`.
# Whether a group actually holds the target bucket is checked per group by the
# resolver (_covers), so a missing bucket never produces an all-time fallback.
PREVIOUS_PRESET = {
    "this_month":   "last_month",
    "this_quarter": "last_quarter",
    "this_year":    "last_year",
    "today":        "yesterday",
}


def previous_preset(preset: str):
    """The cached preset covering the window before `preset`, or None when there is none."""
    target = PREVIOUS_PRESET.get(preset)
    return target if target in PRESET_ALIAS else None


# ---------------------------------------------------------------------------
# dashkit query windows (POST /api/dashboards/query)
# ---------------------------------------------------------------------------
# Inclusive date ranges for a dashkit window, matching dashkit's reference
# calendar (adapters/mock-calendar.ts): last_Nd is the N days ending today,
# this_* runs from the period's first day to today, last_* is the whole
# preceding period. `maximum` has no start of its own (the query engine reads
# it from the data) and no previous period.

_ROLLING_DAYS = {"last_7d": 7, "last_14d": 14, "last_30d": 30}
_PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}


def _period_start(day: _date, period: str) -> _date:
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "quarter":
        return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)
    return day.replace(month=1, day=1)


def _period_before(start: _date, period: str) -> _date:
    """First day of the calendar period before the one starting on `start`."""
    if period == "week":
        return start - timedelta(days=7)
    index = start.year * 12 + (start.month - 1) - _PERIOD_MONTHS[period]
    return _date(index // 12, index % 12 + 1, 1)


_CALENDAR_PRESETS = {
    "this_week_mon_today": ("this", "week"),
    "this_month": ("this", "month"),
    "this_quarter": ("this", "quarter"),
    "this_year": ("this", "year"),
    "last_month": ("last", "month"),
    "last_quarter": ("last", "quarter"),
    "last_year": ("last", "year"),
}


def _parse_day(value) -> _date:
    """A strict YYYY-MM-DD date (no ISO week dates or other forms fromisoformat allows)."""
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"Not a YYYY-MM-DD date: {value!r}")
    return _datetime.strptime(value, "%Y-%m-%d").date()


def bucket_spans_window(preset: str) -> bool:
    """
    Whether the cached bucket of canonical `preset` covers the same days as
    window_range: calendar windows, today and yesterday. Not maximum (no start)
    and not last_Nd (Meta's buckets end yesterday, GHL's span N+1 days).
    """
    return preset in _CALENDAR_PRESETS or preset in ("today", "yesterday")


def window_range(window: dict, today: _date = None):
    """
    (from, to) inclusive dates for a dashkit window ({preset} or {from, to}).
    `from` is None for `maximum`. Raises ValueError for an unknown preset or a
    malformed / reversed custom range.
    """
    today = today or _date.today()
    if "preset" not in window:
        start, end = _parse_day(window.get("from")), _parse_day(window.get("to"))
        if start > end:
            raise ValueError(f"Window from {start} is after to {end}")
        return start, end
    preset = PRESET_ALIAS.get(window["preset"])
    if preset is None:
        raise ValueError(f"Unknown window preset {window['preset']!r}")
    if preset == "maximum":
        return None, today
    if preset == "today":
        return today, today
    if preset == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if preset in _ROLLING_DAYS:
        return today - timedelta(days=_ROLLING_DAYS[preset] - 1), today
    kind, period = _CALENDAR_PRESETS[preset]
    start = _period_start(today, period)
    if kind == "this":
        return start, today
    prev = _period_before(start, period)
    return prev, start - timedelta(days=1)


def previous_window(window: dict, today: _date = None):
    """
    The period before a dashkit window, as inclusive (from, to) dates, or None
    for `maximum`:
      this_*      -> the preceding calendar period cut to the same elapsed
                     length, clamped to that period's last day (31 Mar's
                     this_month compares with 1-28 Feb, not 1 Feb-3 Mar);
      last_*      -> the preceding calendar period;
      everything else (today, yesterday, last_Nd, custom) -> the same number of
                     days immediately before.
    """
    today = today or _date.today()
    start, end = window_range(window, today)
    if start is None:
        return None
    preset = PRESET_ALIAS.get(window["preset"]) if "preset" in window else None
    if preset in _CALENDAR_PRESETS:
        kind, period = _CALENDAR_PRESETS[preset]
        prev_start = _period_before(start, period)
        prev_last = start - timedelta(days=1)
        if kind == "last":
            return prev_start, prev_last
        elapsed = (end - start).days
        return prev_start, min(prev_start + timedelta(days=elapsed), prev_last)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


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
    # Call Center (HotProspector) — per-client, date-windowed
    "hp_leads":            "Call Center Leads",
    "hp_total_calls":      "Total Calls",
    "hp_inbound":          "Inbound Calls",
    "hp_outbound":         "Outbound Calls",
    "hp_transfers":        "Call Transfers",
    "hp_leads_with_calls": "Leads Called",
    "hp_answered_calls":   "Answered Calls",
    "hp_talk_time":        "Talk Time (min)",
    "hp_connect_rate":     "Connect Rate (%)",
    "hp_answer_rate":      "Answer Rate (%)",
    # Call Center (HotProspector) — per-agent, account-wide
    "hp_agent_outbound":    "Agent Outbound Calls",
    "hp_agent_inbound":     "Agent Inbound Calls",
    "hp_agent_dialed":      "Agent Total Dials",
    "hp_agent_answered":    "Agent Answered Calls",
    "hp_agent_convos":      "Conversations",
    "hp_agent_appts":       "Appointments Set",
    "hp_agent_talk_min":    "Agent Talk Time (min)",
    "hp_agent_sms":         "SMS Sent",
    "hp_agent_answer_rate": "Agent Answer Rate (%)",
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
