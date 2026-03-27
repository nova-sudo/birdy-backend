META_CACHE_PRESETS = [
    "maximum",
    "today",
    "yesterday",
    "this_week_mon_today",
    "last_7d",
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

METRIC_LABELS = {
    "spend":       "Total Spend",
    "lead_count":  "Lead Count",
    "ctr":         "CTR (%)",
    "cpc":         "CPC ($)",
    "cpm":         "CPM ($)",
    "roas":        "ROAS",
    "roi":         "ROI",
    "impressions": "Impressions",
    "clicks":      "Clicks",
}

OPERATOR_LABELS = {
    "gt":       ">",
    "lt":       "<",
    "eq":       "=",
    "pct_drop": "↓ %",
    "pct_rise": "↑ %",
}
