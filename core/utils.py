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


def get_result_value(insights_data, action_type="lead"):
    """Safely extract numeric value from insights.results list by action_type."""
    if not insights_data or not isinstance(insights_data, list) or len(insights_data) == 0:
        return 0
    insight = insights_data[0]
    results = insight.get("results") or []
    for res in results:
        if res.get("action_type") == action_type:
            try:
                return int(res.get("value", "0"))
            except (ValueError, TypeError):
                return 0
    return 0
