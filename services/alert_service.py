import os
import logging
from datetime import datetime, timedelta

from core.constants import METRIC_LABELS, OPERATOR_LABELS
from core.database import DB_NAME

logger = logging.getLogger(__name__)


def format_condition_display(condition: dict) -> str:
    op  = OPERATOR_LABELS.get(condition.get("operator", ""), condition.get("operator", ""))
    val = condition.get("value", 0)
    if condition.get("operator") in ("pct_drop", "pct_rise"):
        return f"{op} {val}%"
    return f"{op} {val}"


async def evaluate_alert(alert: dict, mongo_client) -> dict:
    """
    Evaluate whether an alert condition is currently triggered.
    Returns {"triggered": bool, "current_value": float, "message": str}
    """
    condition = alert.get("condition", {})
    metric    = condition.get("metric", "")
    operator  = condition.get("operator", "")
    threshold = float(condition.get("value", 0))
    user_id   = alert.get("user_id")
    group_ids = alert.get("target_group_ids", [])

    db = mongo_client[DB_NAME]

    current_value = 0.0
    message = ""

    try:
        # Fetch client groups -- data lives at facebook.metrics.insights
        groups_query = {"user_id": user_id}
        if group_ids:
            groups_query["id"] = {"$in": group_ids}

        client_groups = await db["client_groups"].find(groups_query).to_list(None)

        if not client_groups:
            return {
                "triggered": False,
                "current_value": 0.0,
                "progress_pct": 0.0,
                "threshold": threshold,
                "message": "No client groups found for this alert"
            }

        # Sum across all targeted groups from facebook.metrics.insights
        total_spend       = 0.0
        total_impressions = 0.0
        total_clicks      = 0.0
        total_leads       = 0.0

        for g in client_groups:
            # Data is stored in facebook_cache (not facebook)
            fb = g.get("facebook_cache") or g.get("facebook") or {}
            insights = fb.get("metrics", {}).get("insights", {})
            total_spend       += float(insights.get("spend", 0) or 0)
            total_impressions += float(insights.get("impressions", 0) or 0)
            total_clicks      += float(insights.get("clicks", 0) or 0)
            total_leads       += float(fb.get("total_leads", 0) or 0)

        if metric == "lead_count":
            current_value = total_leads
        elif metric == "spend":
            current_value = total_spend
        elif metric == "impressions":
            current_value = total_impressions
        elif metric == "clicks":
            current_value = total_clicks
        elif metric == "ctr":
            current_value = (total_clicks / total_impressions * 100) if total_impressions else 0.0
        elif metric == "cpc":
            current_value = (total_spend / total_clicks) if total_clicks else 0.0
        elif metric == "cpm":
            current_value = (total_spend / total_impressions * 1000) if total_impressions else 0.0

        # Evaluate operator
        triggered    = False
        progress_pct = 0.0   # 0-100: how close we are to triggering

        if operator == "gt":
            triggered    = current_value > threshold
            progress_pct = min(100.0, (current_value / threshold * 100)) if threshold > 0 else (100.0 if current_value > 0 else 0.0)
            message      = f"{METRIC_LABELS.get(metric, metric)} is {current_value:.2f}, threshold is > {threshold}"

        elif operator == "lt":
            # Progress toward trigger = how close current_value is to falling below threshold
            # 100% = exactly at threshold (about to trigger), 0% = far above
            if threshold > 0:
                progress_pct = min(100.0, max(0.0, (1 - (current_value - threshold) / threshold) * 100))
            else:
                progress_pct = 100.0 if current_value <= threshold else 0.0
            triggered = current_value < threshold
            message   = f"{METRIC_LABELS.get(metric, metric)} is {current_value:.2f}, threshold is < {threshold}"

        elif operator == "eq":
            triggered    = abs(current_value - threshold) < 0.01
            diff         = abs(current_value - threshold)
            progress_pct = max(0.0, 100.0 - (diff / max(threshold, 1) * 100))
            message      = f"{METRIC_LABELS.get(metric, metric)} is {current_value:.2f}, target is {threshold}"

        elif operator in ("pct_drop", "pct_rise"):
            # Compare lead counts between current and previous period
            period_days = {"day": 1, "week": 7, "month": 30}.get(condition.get("period", "week"), 7)
            now    = datetime.utcnow()
            base_q = {"user_id": user_id}
            if group_ids:
                base_q["client_group_id"] = {"$in": group_ids}
            curr_q = {**base_q, "lead_data.created_time": {"$gte": (now - timedelta(days=period_days)).isoformat()}}
            prev_q = {**base_q, "lead_data.created_time": {
                "$gte": (now - timedelta(days=period_days * 2)).isoformat(),
                "$lt":  (now - timedelta(days=period_days)).isoformat()
            }}
            curr_val = float(await db["facebook_leads"].count_documents(curr_q))
            prev_val = float(await db["facebook_leads"].count_documents(prev_q))

            pct_change = ((curr_val - prev_val) / prev_val * 100) if prev_val > 0 else 0.0
            current_value = pct_change

            if operator == "pct_drop":
                triggered    = pct_change <= -threshold
                # progress = how close the drop is to the threshold drop
                progress_pct = min(100.0, (abs(min(pct_change, 0)) / threshold * 100)) if threshold > 0 else 0.0
                message      = f"{METRIC_LABELS.get(metric, metric)} changed {pct_change:+.1f}% vs previous {condition.get('period', 'week')} (threshold: \u2193{threshold}%)"
            else:
                triggered    = pct_change >= threshold
                progress_pct = min(100.0, (pct_change / threshold * 100)) if threshold > 0 else 0.0
                message      = f"{METRIC_LABELS.get(metric, metric)} changed {pct_change:+.1f}% vs previous {condition.get('period', 'week')} (threshold: \u2191{threshold}%)"

        return {
            "triggered":     triggered,
            "current_value": current_value,
            "progress_pct":  round(progress_pct, 1),
            "threshold":     threshold,
            "message":       message,
        }

    except Exception as e:
        logger.error(f"Error evaluating alert {alert.get('id')}: {e}")
        return {"triggered": False, "current_value": 0.0, "message": str(e)}
