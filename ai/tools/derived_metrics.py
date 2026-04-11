"""Shared utility to enrich insight records with derived metrics."""


def _safe_float(val) -> float | None:
    """Convert a value to float, tolerant of strings and missing data."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_div(numerator, denominator) -> float | None:
    """Safe division — returns None when either side is missing or zero."""
    n = _safe_float(numerator)
    d = _safe_float(denominator)
    if n is None or d is None or d == 0:
        return None
    return round(n / d, 4)


def enrich(record: dict) -> dict:
    """
    Add derived metrics to an insight record in-place.

    Works on campaign/adset/ad rows AND aggregated metrics objects.
    Tolerant of missing keys and string values from the Facebook API.
    """
    spend = _safe_float(record.get("spend"))
    clicks = _safe_float(record.get("clicks"))
    impressions = _safe_float(record.get("impressions"))
    reach = _safe_float(record.get("reach"))
    results = _safe_float(record.get("results"))
    total_leads = _safe_float(record.get("total_leads"))

    leads = total_leads if total_leads else results

    # CPL — cost per lead
    if "cpl" not in record or record["cpl"] is None:
        record["cpl"] = _safe_div(spend, leads)

    # Conversion rate — leads / clicks as percentage
    if "conversion_rate" not in record or record["conversion_rate"] is None:
        cr = _safe_div(leads, clicks)
        record["conversion_rate"] = round(cr * 100, 2) if cr is not None else None

    # Frequency — impressions / reach
    if "frequency" not in record or record["frequency"] is None:
        record["frequency"] = _safe_div(impressions, reach)

    # Cost per result — fill if missing
    if "cost_per_result" not in record or record["cost_per_result"] is None:
        record["cost_per_result"] = _safe_div(spend, results)

    return record
