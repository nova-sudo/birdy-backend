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

# Tiered refresh: frequent presets refresh every hour, slow presets once daily
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
