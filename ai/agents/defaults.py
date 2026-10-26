"""
ai/agents/defaults.py
---------------------
Default rule parameters, modes, guardrails and permissions for a new agent.
Every value here is editable per agent (and per client for rule params).
"""

RULE_KINDS = ("pause_over_target", "revive_winner", "flag_no_spend", "flag_declining")

DEFAULT_PARAMS: dict[str, dict] = {
    "pause_over_target": {
        "patience_spend": 60,
        "patience_days": 3,
        "cpl_multiple": 1.5,
        "zero_lead_spend": 80,
        "recent_window_days": 7,
    },
    "revive_winner": {
        "min_paused_days": 30,
        "min_leads": 5,
        "max_creative_age_days": 90,
    },
    "flag_no_spend": {"hours": 24},
    "flag_declining": {"pct_rise": 30, "min_leads": 10, "window_days": 7},
}

# Modes are flag | ask | auto.
DEFAULT_MODE = {
    "pause_over_target": "ask",
    "revive_winner": "ask",
    "flag_no_spend": "flag",
    "flag_declining": "flag",
}

DEFAULT_GUARDRAILS = {
    "max_pauses_per_client_day": 3,
    "protect_last_live_ad": True,
    "protected_tags": ["Evergreen"],
}

DEFAULT_PERMISSIONS = {
    "ads": True,
    "adsets": True,
    "campaigns": False,
    "budgets": False,
}
