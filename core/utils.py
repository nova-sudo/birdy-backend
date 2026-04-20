from bson import ObjectId
from datetime import datetime

from core.config import COOKIE_SECURE, COOKIE_SAMESITE, COOKIE_DOMAIN


def mongo_to_dict(obj):
    """Convert MongoDB documents to JSON-serializable format."""
    if isinstance(obj, dict):
        return {k: mongo_to_dict(v) for k, v in obj.items() if k != "_id"}
    elif isinstance(obj, list):
        return [mongo_to_dict(item) for item in obj]
    elif isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def set_cookie(response, key, value, max_age):
    """Set a cookie with flexible settings."""
    response.set_cookie(
        key=key,
        value=value,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        domain=COOKIE_DOMAIN,
    )


def preset_date_bounds(preset_key: str):
    """
    Return (start_iso, end_iso) date strings for a given preset key.
    Returns (None, None) for 'maximum' (all-time).
    """
    from datetime import date as _date, timedelta as _td
    from core.constants import GHL_PRESET_DATE_RANGE

    today = _date.today()
    spec = GHL_PRESET_DATE_RANGE.get(preset_key)
    if spec is None:
        return None, None
    if isinstance(spec, tuple):
        start_days, end_days = spec
        return (
            (today - _td(days=start_days)).isoformat(),
            (today - _td(days=end_days)).isoformat(),
        )
    if spec == "this_week_mon":
        s = today - _td(days=today.weekday())
        return s.isoformat(), today.isoformat()
    if spec == "this_week_sun":
        s = today - _td(days=(today.weekday() + 1) % 7)
        return s.isoformat(), today.isoformat()
    if spec == "this_month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if spec == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - _td(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    if spec == "this_quarter":
        qm = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=qm, day=1).isoformat(), today.isoformat()
    if spec == "last_quarter":
        qm = ((today.month - 1) // 3) * 3 + 1
        first_this_q = today.replace(month=qm, day=1)
        last_prev_q = first_this_q - _td(days=1)
        pqm = ((last_prev_q.month - 1) // 3) * 3 + 1
        return last_prev_q.replace(month=pqm, day=1).isoformat(), last_prev_q.isoformat()
    if spec == "this_year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if spec == "last_year":
        return _date(today.year - 1, 1, 1).isoformat(), _date(today.year - 1, 12, 31).isoformat()
    return None, None


# Meta reports lead-like conversions under many action_type keys depending on
# the campaign objective. We prefer the exact requested type, then fall back
# through the common lead-related variants, and finally use `results[0]` which
# Meta populates with the campaign's primary result.
_LEAD_ACTION_TYPES = (
    "lead",
    "onsite_conversion.lead_grouped",
    "offsite_conversion.fb_pixel_lead",
    "leadgen_grouped",
    "leadgen_other",
    "submit_application_total",
    "onsite_web_lead",
    "complete_registration",
)


def _int_or_zero(v):
    # Meta sometimes nests values inside action-attribution breakdowns (e.g.
    # `{"1d_click": "3", "7d_click": "5"}`) or wraps scalars in a list. Coerce
    # those shapes defensively so we never crash on "int() argument must be …".
    if v is None:
        return 0
    if isinstance(v, list):
        # Use the first numeric-looking element
        for item in v:
            r = _int_or_zero(item)
            if r:
                return r
        return 0
    if isinstance(v, dict):
        # Prefer a "value" field if present; else sum numeric leaves
        if "value" in v:
            return _int_or_zero(v["value"])
        total = 0
        for leaf in v.values():
            total += _int_or_zero(leaf)
        return total
    try:
        return int(float(v or 0))
    except (ValueError, TypeError):
        return 0


def get_result_value(insights_data, action_type="lead"):
    """Safely extract numeric value from insights results or actions by action_type.

    Resolution order:
      1. `results` with matching action_type
      2. `actions` with matching action_type
      3. If action_type is "lead" (default): `results`/`actions` with any
         known lead-like action_type (pixel lead, onsite lead, application, etc.)
      4. If action_type is "lead" (default) and `results` is present but none
         of the above matched: use the first entry in `results` (Meta puts the
         campaign-relevant outcome there regardless of its action_type).
    """
    if not insights_data or not isinstance(insights_data, list) or len(insights_data) == 0:
        return 0
    insight = insights_data[0]

    results_list = insight.get("results") or []
    actions_list = insight.get("actions") or []

    # 1. Exact match in results
    for res in results_list:
        if res.get("action_type") == action_type:
            return _int_or_zero(res.get("value"))

    # 2. Exact match in actions
    for action in actions_list:
        if action.get("action_type") == action_type:
            return _int_or_zero(action.get("value"))

    # 3 & 4: Only relaxed fallbacks when we were looking for "lead"
    if action_type == "lead":
        # Check all lead-like action types in priority order
        for at in _LEAD_ACTION_TYPES[1:]:  # skip "lead" (already tried)
            for res in results_list:
                if res.get("action_type") == at:
                    return _int_or_zero(res.get("value"))
            for action in actions_list:
                if action.get("action_type") == at:
                    return _int_or_zero(action.get("value"))

        # 4. Last resort — Meta populates results[0] with whatever the
        # campaign's primary outcome is. If it's non-zero and no lead-like
        # type matched, use it (this covers custom conversion events).
        if results_list:
            return _int_or_zero(results_list[0].get("value"))

    return 0
