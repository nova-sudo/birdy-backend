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


def _evaluate_formula_parts(formula_parts: list, row_data: dict) -> float:
    """
    Evaluate a custom metric formula against pre-computed row data.

    Delegates to the central metric_orchestrator. Kept as a thin wrapper so
    callers that already have an aggregated row_data dict don't need to
    rebuild it from raw client_group documents.
    """
    from services.metric_orchestrator import evaluate_formula
    # The orchestrator expects a group document but also accepts a pre-built
    # `row` dict to skip resolution. Pass an empty group since row has everything.
    return evaluate_formula(formula_parts, group={}, row=row_data)


def _get_insights(group: dict, preset: str) -> dict:
    """Get insights dict from a specific Meta cache preset bucket."""
    fb = group.get("facebook_cache") or {}
    preset_data = fb.get(preset, {})
    insights = preset_data.get("metrics", {}).get("insights", {})
    if not insights:
        insights = fb.get("metrics", {}).get("insights", {})
    return insights


def _raw_metric(insights: dict, metric: str) -> float:
    """Extract a raw summable value from insights for a given metric key."""
    mapping = {
        "spend": "spend", "impressions": "impressions", "clicks": "clicks",
        "reach": "reach", "ctr": "ctr", "cpc": "cpc", "cpm": "cpm",
        "cost_per_result": "cost_per_result",
        "meta_leads": "results",
    }
    key = mapping.get(metric, metric)
    return float(insights.get(key, 0) or 0)


def _sum_insights_field(client_groups: list, wider_preset: str, current_preset: str, metric: str) -> float:
    """
    Compute the previous-period metric value by subtracting current from wider.
    e.g. previous 7d spend = last_14d spend - last_7d spend.
    For rate metrics (ctr, cpc, cpm) we recompute from the underlying counts.
    """
    # Rate metrics need special handling — subtract the raw counts then recompute
    rate_metrics = {"ctr", "cpc", "cpm", "cpl", "conversion_rate", "meta_conversion", "cost_per_result", "frequency"}

    if metric not in rate_metrics:
        # Simple summable metric (spend, impressions, clicks, reach)
        wider_sum  = sum(_raw_metric(_get_insights(g, wider_preset), metric) for g in client_groups)
        current_sum = sum(_raw_metric(_get_insights(g, current_preset), metric) for g in client_groups)
        return max(0.0, wider_sum - current_sum)

    # For rate metrics, get the underlying counts for the previous period
    w_spend = w_impr = w_clicks = w_reach = w_results = 0.0
    c_spend = c_impr = c_clicks = c_reach = c_results = 0.0
    for g in client_groups:
        wi = _get_insights(g, wider_preset)
        ci = _get_insights(g, current_preset)
        w_spend   += float(wi.get("spend", 0) or 0)
        w_impr    += float(wi.get("impressions", 0) or 0)
        w_clicks  += float(wi.get("clicks", 0) or 0)
        w_reach   += float(wi.get("reach", 0) or 0)
        w_results += float(wi.get("results", 0) or 0)
        c_spend   += float(ci.get("spend", 0) or 0)
        c_impr    += float(ci.get("impressions", 0) or 0)
        c_clicks  += float(ci.get("clicks", 0) or 0)
        c_reach   += float(ci.get("reach", 0) or 0)
        c_results += float(ci.get("results", 0) or 0)

    # Previous period = wider - current
    p_spend   = max(0.0, w_spend - c_spend)
    p_impr    = max(0.0, w_impr - c_impr)
    p_clicks  = max(0.0, w_clicks - c_clicks)
    p_reach   = max(0.0, w_reach - c_reach)
    p_results = max(0.0, w_results - c_results)

    if metric == "ctr":
        return (p_clicks / p_impr * 100) if p_impr else 0.0
    elif metric == "cpc":
        return (p_spend / p_clicks) if p_clicks else 0.0
    elif metric == "cpm":
        return (p_spend / p_impr * 1000) if p_impr else 0.0
    elif metric == "cost_per_result":
        return (p_spend / p_results) if p_results else 0.0
    elif metric == "frequency":
        return (p_impr / p_reach) if p_reach else 0.0
    return 0.0


def _compute_metric_from_preset(client_groups: list, preset: str, metric: str) -> float:
    """Compute a metric value directly from a single Meta cache preset."""
    total_spend = total_impr = total_clicks = total_reach = total_results = 0.0
    for g in client_groups:
        ins = _get_insights(g, preset)
        total_spend   += float(ins.get("spend", 0) or 0)
        total_impr    += float(ins.get("impressions", 0) or 0)
        total_clicks  += float(ins.get("clicks", 0) or 0)
        total_reach   += float(ins.get("reach", 0) or 0)
        total_results += float(ins.get("results", 0) or ins.get("total_leads", 0) or 0)

    if metric == "spend":       return total_spend
    if metric == "impressions": return total_impr
    if metric == "clicks":      return total_clicks
    if metric == "reach":       return total_reach
    if metric in ("meta_leads",): return total_results
    if metric == "ctr":         return (total_clicks / total_impr * 100) if total_impr else 0.0
    if metric == "cpc":         return (total_spend / total_clicks) if total_clicks else 0.0
    if metric == "cpm":         return (total_spend / total_impr * 1000) if total_impr else 0.0
    if metric == "cost_per_result": return (total_spend / total_results) if total_results else 0.0
    if metric == "frequency":   return (total_impr / total_reach) if total_reach else 0.0
    return 0.0


async def evaluate_alert(alert: dict, mongo_client) -> dict:
    """
    Evaluate whether an alert condition is currently triggered.
    Uses the correct date window for both ad metrics (Meta cache presets)
    and lead counts (GHL contacts queried by dateAdded).
    Returns {"triggered": bool, "current_value": float, "message": str}
    """
    condition = alert.get("condition", {})
    metric    = condition.get("metric", "")
    operator  = condition.get("operator", "")
    threshold = float(condition.get("value", 0))
    period    = condition.get("period", "day")
    user_id   = alert.get("user_id")
    group_ids = alert.get("target_group_ids", [])

    db = mongo_client[DB_NAME]

    # Map alert period → Meta cache preset key
    PERIOD_TO_META_PRESET = {
        "today":  "today",
        "day":    "yesterday",
        "week":   "last_7d",
        "month":  "last_30d",
    }
    # For pct operators: how to get the previous period's ad metrics
    # "subtract" mode: previous = wider_preset - current_preset
    # "direct" mode:   previous = previous_preset directly (no subtraction)
    PERIOD_PCT_COMPARISON = {
        "day":   {"mode": "subtract", "wider": "last_7d"},
        "week":  {"mode": "subtract", "wider": "last_14d"},
        "month": {"mode": "direct",   "previous": "last_month"},
    }
    # Map alert period → number of days back for GHL date queries
    PERIOD_TO_DAYS = {
        "today": 0,
        "day":   1,
        "week":  7,
        "month": 30,
    }
    # Human-readable labels for messages
    PERIOD_LABEL = {
        "today": "today",
        "day":   "yesterday",
        "week":  "7 days",
        "month": "month",
    }

    meta_preset = PERIOD_TO_META_PRESET.get(period, "yesterday")
    period_days = PERIOD_TO_DAYS.get(period, 1)

    current_value = 0.0
    message = ""

    try:
        # ── Fetch client groups with the correct Meta preset bucket ──
        custom_metric = None  # populated later if metric starts with "custom:"

        groups_query = {"user_id": user_id}
        if group_ids:
            groups_query["id"] = {"$in": group_ids}

        # Project the preset we need + comparison presets for pct operators + legacy fallback
        pct_config = PERIOD_PCT_COMPARISON.get(period, {})
        projection = {
            "id": 1,
            f"facebook_cache.{meta_preset}.metrics.insights": 1,
            "facebook_cache.metrics.insights": 1,   # legacy fallback
        }
        if pct_config.get("mode") == "subtract":
            projection[f"facebook_cache.{pct_config['wider']}.metrics.insights"] = 1
        elif pct_config.get("mode") == "direct":
            projection[f"facebook_cache.{pct_config['previous']}.metrics.insights"] = 1

        client_groups = await db["client_groups"].find(
            groups_query, projection,
        ).to_list(None)

        if not client_groups:
            return {
                "triggered": False,
                "current_value": 0.0,
                "progress_pct": 0.0,
                "threshold": threshold,
                "message": "No client groups found for this alert"
            }

        # ── Aggregate ad metrics from the correct preset bucket ──
        total_spend       = 0.0
        total_impressions = 0.0
        total_clicks      = 0.0
        total_reach       = 0.0
        total_results     = 0.0

        for g in client_groups:
            fb = g.get("facebook_cache") or {}
            # Use the preset-specific bucket; fall back to legacy top-level metrics
            preset_data = fb.get(meta_preset, {})
            insights = preset_data.get("metrics", {}).get("insights", {})
            if not insights:
                insights = fb.get("metrics", {}).get("insights", {})

            total_spend       += float(insights.get("spend", 0) or 0)
            total_impressions += float(insights.get("impressions", 0) or 0)
            total_clicks      += float(insights.get("clicks", 0) or 0)
            total_reach       += float(insights.get("reach", 0) or 0)
            total_results     += float(insights.get("results", 0) or insights.get("actions", 0) or 0)

        # ── Meta leads: from the insights preset (results / total_leads) ──
        meta_leads = sum(
            float((_get_insights(g, meta_preset).get("results", 0) or
                   _get_insights(g, meta_preset).get("total_leads", 0) or 0))
            for g in client_groups
        )

        # ── GHL data: leads + opportunity stats for the date window ──
        now = datetime.utcnow()
        ghl_base_q = {"user_id": user_id}
        if group_ids:
            ghl_base_q["client_group_id"] = {"$in": group_ids}

        if period == "today":
            date_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            ghl_date_filter = {"$gte": date_start}
        elif period == "day":
            yesterday_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            today_start     = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            ghl_date_filter = {"$gte": yesterday_start, "$lt": today_start}
        else:
            date_start = (now - timedelta(days=period_days)).isoformat()
            ghl_date_filter = {"$gte": date_start}

        ghl_leads_q = {**ghl_base_q, "contact_data.dateAdded": ghl_date_filter}
        ghl_leads = float(await db["ghl_contacts"].count_documents(ghl_leads_q))

        # GHL opportunity stats across ALL contacts (not date-filtered)
        ghl_won = 0.0
        ghl_lost = 0.0
        ghl_open = 0.0
        ghl_abandoned = 0.0
        ghl_total_ops = 0.0
        opp_metrics = ("ghl_conversion", "ghl_won_opps", "ghl_lost_opps", "ghl_open_opps", "ghl_abandoned_opps", "ghl_total_opps")
        if metric in opp_metrics:
            # Debug: count how many contacts match the base query
            base_count = await db["ghl_contacts"].count_documents(ghl_base_q)
            logger.info(f"ghl_conversion debug: ghl_base_q={ghl_base_q}, matched_contacts={base_count}")

            opp_pipeline = [
                {"$match": ghl_base_q},  # all contacts in target groups, no date filter
                {"$unwind": {"path": "$contact_data.opportunities", "preserveNullAndEmptyArrays": False}},
                {"$group": {
                    "_id": {"$ifNull": ["$contact_data.opportunities.status", "open"]},
                    "count": {"$sum": 1},
                }},
            ]
            opp_result = await db["ghl_contacts"].aggregate(opp_pipeline).to_list(None)
            logger.info(f"ghl_conversion debug: opp_result={opp_result}")
            for doc in opp_result:
                ghl_total_ops += doc["count"]
                if doc["_id"] == "won":
                    ghl_won = doc["count"]
                elif doc["_id"] == "lost":
                    ghl_lost = doc["count"]
                elif doc["_id"] == "open":
                    ghl_open = doc["count"]
                elif doc["_id"] == "abandoned":
                    ghl_abandoned = doc["count"]

        # GHL revenue: sum of won opportunity monetaryValue (all contacts, no date filter)
        ghl_revenue = 0.0
        if metric == "ghl_revenue":
            rev_pipeline = [
                {"$match": ghl_base_q},
                {"$unwind": {"path": "$contact_data.opportunities", "preserveNullAndEmptyArrays": False}},
                {"$match": {"contact_data.opportunities.status": "won"}},
                {"$group": {
                    "_id": None,
                    "total": {"$sum": {"$ifNull": [{"$toDouble": "$contact_data.opportunities.monetaryValue"}, 0]}},
                }},
            ]
            rev_result = await db["ghl_contacts"].aggregate(rev_pipeline).to_list(1)
            if rev_result:
                ghl_revenue = float(rev_result[0].get("total", 0))

        # ── Compute the requested metric ──
        if metric == "ghl_leads" or metric == "lead_count":
            current_value = ghl_leads
        elif metric == "meta_leads":
            current_value = meta_leads
        elif metric == "ghl_revenue":
            current_value = ghl_revenue
        elif metric == "ghl_conversion":
            current_value = (ghl_won / ghl_total_ops * 100) if ghl_total_ops else 0.0
        elif metric == "ghl_won_opps":
            current_value = ghl_won
        elif metric == "ghl_lost_opps":
            current_value = ghl_lost
        elif metric == "ghl_open_opps":
            current_value = ghl_open
        elif metric == "ghl_abandoned_opps":
            current_value = ghl_abandoned
        elif metric == "ghl_total_opps":
            current_value = ghl_total_ops
        elif metric == "meta_conversion" or metric == "conversion_rate":
            current_value = (meta_leads / total_clicks * 100) if total_clicks else 0.0
        elif metric == "spend":
            current_value = total_spend
        elif metric == "impressions":
            current_value = total_impressions
        elif metric == "clicks":
            current_value = total_clicks
        elif metric == "reach":
            current_value = total_reach
        elif metric == "ctr":
            current_value = (total_clicks / total_impressions * 100) if total_impressions else 0.0
        elif metric == "cpc":
            current_value = (total_spend / total_clicks) if total_clicks else 0.0
        elif metric == "cpm":
            current_value = (total_spend / total_impressions * 1000) if total_impressions else 0.0
        elif metric == "cpl":
            current_value = (total_spend / meta_leads) if meta_leads else 0.0
        elif metric == "cost_per_result":
            current_value = (total_spend / total_results) if total_results else 0.0
        elif metric == "frequency":
            current_value = (total_impressions / total_reach) if total_reach else 0.0
        elif metric.startswith("tag:"):
            # Dynamic tag metric — count GHL contacts with this tag in the date window
            tag_name = metric[4:]  # strip "tag:" prefix
            tag_query = {**ghl_leads_q, "contact_data.tags": tag_name}
            current_value = float(await db["ghl_contacts"].count_documents(tag_query))

        elif metric.startswith("custom:"):
            # Custom formula metric — load from user's custom_metrics, evaluate formula
            custom_id = metric[7:]  # strip "custom:" prefix
            user_doc = await db["users"].find_one(
                {"user_id": user_id},
                {"custom_metrics": 1}
            )
            custom_metrics = user_doc.get("custom_metrics", []) if user_doc else []
            custom_metric = next((m for m in custom_metrics if m.get("id") == custom_id), None)
            if custom_metric and custom_metric.get("formula_parts"):
                row_data = {
                    "spend": total_spend, "meta_spend": total_spend,
                    "impressions": total_impressions, "meta_impressions": total_impressions,
                    "clicks": total_clicks, "meta_clicks": total_clicks,
                    "reach": total_reach, "meta_reach": total_reach,
                    "results": total_results, "meta_results": total_results,
                    "meta_leads": meta_leads,
                    "ghl_contacts": ghl_leads, "ghl_leads": ghl_leads,
                    "ghl_revenue": ghl_revenue,
                    "ghl_won_opps": ghl_won, "ghl_lost_opps": ghl_lost,
                    "ghl_open_opps": ghl_open, "ghl_abandoned_opps": ghl_abandoned,
                    "ghl_total_opps": ghl_total_ops,
                    "leads": ghl_leads,
                    "ctr": (total_clicks / total_impressions * 100) if total_impressions else 0,
                    "cpc": (total_spend / total_clicks) if total_clicks else 0,
                    "cpm": (total_spend / total_impressions * 1000) if total_impressions else 0,
                    "meta_ctr": (total_clicks / total_impressions * 100) if total_impressions else 0,
                    "meta_cpc": (total_spend / total_clicks) if total_clicks else 0,
                    "meta_cpm": (total_spend / total_impressions * 1000) if total_impressions else 0,
                }
                current_value = _evaluate_formula_parts(custom_metric["formula_parts"], row_data)

        # ── Evaluate the operator ──
        triggered    = False
        progress_pct = 0.0

        period_label = PERIOD_LABEL.get(period, period)
        if metric.startswith("tag:"):
            metric_label = f"Tag: {metric[4:]}"
        elif metric.startswith("custom:"):
            # Look up the custom metric name
            custom_id = metric[7:]
            cm_name = custom_id
            if custom_metric:
                cm_name = custom_metric.get("name", custom_id)
            metric_label = cm_name
        else:
            metric_label = METRIC_LABELS.get(metric, metric)

        if operator == "gt":
            triggered    = current_value > threshold
            progress_pct = min(100.0, (current_value / threshold * 100)) if threshold > 0 else (100.0 if current_value > 0 else 0.0)
            message      = f"{metric_label} is {current_value:.2f} ({period_label}), threshold is > {threshold}"

        elif operator == "lt":
            if threshold > 0:
                progress_pct = min(100.0, max(0.0, (1 - (current_value - threshold) / threshold) * 100))
            else:
                progress_pct = 100.0 if current_value <= threshold else 0.0
            triggered = current_value < threshold
            message   = f"{metric_label} is {current_value:.2f} ({period_label}), threshold is < {threshold}"

        elif operator == "eq":
            triggered    = abs(current_value - threshold) < 0.01
            diff         = abs(current_value - threshold)
            progress_pct = max(0.0, 100.0 - (diff / max(threshold, 1) * 100))
            message      = f"{metric_label} is {current_value:.2f} ({period_label}), target is {threshold}"

        elif operator == "neq":
            triggered    = abs(current_value - threshold) >= 0.01
            diff         = abs(current_value - threshold)
            progress_pct = min(100.0, (diff / max(threshold, 1) * 100)) if not triggered else 100.0
            message      = f"{metric_label} is {current_value:.2f} ({period_label}), watching for ≠ {threshold}"

        elif operator in ("pct_drop", "pct_rise"):
            # Compare the actual metric between current period and previous period
            pct_period_days = max(period_days, 1)
            curr_val = 0.0
            prev_val = 0.0

            # Helper: get GHL lead counts for current and previous periods
            async def _ghl_lead_counts():
                ghl_pct_q = {"user_id": user_id}
                if group_ids:
                    ghl_pct_q["client_group_id"] = {"$in": group_ids}
                curr_start = (now - timedelta(days=pct_period_days)).isoformat()
                prev_start = (now - timedelta(days=pct_period_days * 2)).isoformat()
                pipeline = [
                    {"$match": {**ghl_pct_q, "contact_data.dateAdded": {"$gte": prev_start}}},
                    {"$facet": {
                        "current":  [{"$match": {"contact_data.dateAdded": {"$gte": curr_start}}}, {"$count": "n"}],
                        "previous": [{"$match": {"contact_data.dateAdded": {"$gte": prev_start, "$lt": curr_start}}}, {"$count": "n"}],
                    }},
                ]
                result = await db["ghl_contacts"].aggregate(pipeline).to_list(1)
                doc = result[0] if result else {}
                c = float(doc.get("current", [{}])[0].get("n", 0) if doc.get("current") else 0)
                p = float(doc.get("previous", [{}])[0].get("n", 0) if doc.get("previous") else 0)
                return c, p

            # Helper: get previous period ad metric value
            def _prev_ad_metric(m):
                if pct_config.get("mode") == "subtract":
                    return _sum_insights_field(client_groups, pct_config["wider"], meta_preset, m)
                elif pct_config.get("mode") == "direct":
                    return _compute_metric_from_preset(client_groups, pct_config["previous"], m)
                return 0.0

            if metric in ("ghl_leads", "lead_count"):
                # Pure GHL lead count comparison
                curr_val, prev_val = await _ghl_lead_counts()

            elif metric in ("ghl_conversion", "ghl_revenue", "ghl_won_opps", "ghl_lost_opps", "ghl_open_opps", "ghl_abandoned_opps", "ghl_total_opps"):
                # Can't time-window opportunities. Compare against last stored value.
                curr_val = current_value
                prev_val = float(alert.get("current_value", 0) or 0)
                if prev_val == 0.0 and alert.get("last_evaluated_at") is None:
                    prev_val = curr_val  # first eval, no baseline

            elif metric == "meta_leads":
                # Meta leads from insights — purely from Meta presets
                curr_val = current_value
                prev_val = _prev_ad_metric("meta_leads")

            elif metric in ("cpl", "meta_conversion", "conversion_rate"):
                # Hybrid: Meta leads + Meta ad data
                curr_val = current_value
                if metric == "cpl":
                    prev_spend = _prev_ad_metric("spend")
                    prev_meta_leads = _prev_ad_metric("meta_leads")
                    prev_val = (prev_spend / prev_meta_leads) if prev_meta_leads else 0.0
                else:
                    prev_clicks = _prev_ad_metric("clicks")
                    prev_meta_leads = _prev_ad_metric("meta_leads")
                    prev_val = (prev_meta_leads / prev_clicks * 100) if prev_clicks else 0.0

            elif metric.startswith("tag:"):
                # Tag metric — query GHL contacts with this tag for current and previous period
                tag_name = metric[4:]
                pct_period_days = max(period_days, 1)
                tag_base = {"user_id": user_id}
                if group_ids:
                    tag_base["client_group_id"] = {"$in": group_ids}
                tag_base["contact_data.tags"] = tag_name

                curr_start = (now - timedelta(days=pct_period_days)).isoformat()
                prev_start = (now - timedelta(days=pct_period_days * 2)).isoformat()

                facet_result = await db["ghl_contacts"].aggregate([
                    {"$match": {**tag_base, "contact_data.dateAdded": {"$gte": prev_start}}},
                    {"$facet": {
                        "current":  [{"$match": {"contact_data.dateAdded": {"$gte": curr_start}}}, {"$count": "n"}],
                        "previous": [{"$match": {"contact_data.dateAdded": {"$gte": prev_start, "$lt": curr_start}}}, {"$count": "n"}],
                    }},
                ]).to_list(1)
                doc = facet_result[0] if facet_result else {}
                curr_val = float(doc.get("current", [{}])[0].get("n", 0) if doc.get("current") else 0)
                prev_val = float(doc.get("previous", [{}])[0].get("n", 0) if doc.get("previous") else 0)

            else:
                # Pure ad metrics: use Meta cache presets only
                curr_val = current_value
                prev_val = _prev_ad_metric(metric)

            pct_change = ((curr_val - prev_val) / prev_val * 100) if prev_val > 0 else 0.0

            # For ghl_conversion, store the actual rate (not pct_change) so next
            # evaluation has a real baseline to compare against.
            if metric in ("ghl_conversion", "ghl_revenue", "ghl_won_opps", "ghl_lost_opps", "ghl_open_opps", "ghl_abandoned_opps", "ghl_total_opps"):
                # current_value stays as the actual value so next eval has a baseline
                pass
            else:
                current_value = pct_change

            if operator == "pct_drop":
                triggered    = pct_change <= -threshold
                progress_pct = min(100.0, (abs(min(pct_change, 0)) / threshold * 100)) if threshold > 0 else 0.0
                message      = f"{metric_label} changed {pct_change:+.1f}% vs prior {period_label} (threshold: ↓{threshold}%)"
            else:
                triggered    = pct_change >= threshold
                progress_pct = min(100.0, max(0.0, pct_change / threshold * 100)) if threshold > 0 else 0.0
                message      = f"{metric_label} changed {pct_change:+.1f}% vs prior {period_label} (threshold: ↑{threshold}%)"

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
