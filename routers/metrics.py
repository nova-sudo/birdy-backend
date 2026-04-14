import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from core.database import DB_NAME
from core.models import CreateCustomMetricRequest, UpdateCustomMetricRequest
from dependencies import get_mongo_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/custom-metrics")
async def list_custom_metrics(current_user: str = Depends(get_current_user)):
    """Return all custom metrics for the current user."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        user = await db["users"].find_one(
            {"user_id": current_user},
            {"custom_metrics": 1}
        )
        return {"custom_metrics": user.get("custom_metrics", []) if user else []}


@router.get("/api/custom-metrics/available-fields")
async def get_available_metric_fields(current_user: str = Depends(get_current_user)):
    """
    Lightweight endpoint: returns just the metric field names and tag list
    for the formula builder — no campaign/adset/ad data.
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        # Get tag names from client groups (just the tag_breakdown keys)
        groups = await db["client_groups"].find(
            {"user_id": current_user},
            {"gohighlevel_cache.metrics.tag_breakdown": 1}
        ).to_list(None)

        tags = set()
        for g in groups:
            breakdown = g.get("gohighlevel_cache", {}).get("metrics", {}).get("tag_breakdown", {})
            tags.update(breakdown.keys())

        return {
            "base_metrics": [
                # Group-level (Client Groups page)
                {"id": "meta_spend", "label": "Meta Spend", "category": "Meta Ads", "level": "group"},
                {"id": "meta_impressions", "label": "Impressions", "category": "Meta Ads", "level": "group"},
                {"id": "meta_clicks", "label": "Clicks", "category": "Meta Ads", "level": "group"},
                {"id": "meta_reach", "label": "Reach", "category": "Meta Ads", "level": "group"},
                {"id": "meta_results", "label": "Results", "category": "Meta Ads", "level": "group"},
                {"id": "meta_ctr", "label": "CTR", "category": "Meta Ads", "level": "group"},
                {"id": "meta_cpc", "label": "CPC", "category": "Meta Ads", "level": "group"},
                {"id": "meta_cpm", "label": "CPM", "category": "Meta Ads", "level": "group"},
                {"id": "meta_leads", "label": "Meta Leads", "category": "Meta Ads", "level": "group"},
                {"id": "ghl_contacts", "label": "GHL Contacts", "category": "GoHighLevel", "level": "group"},
                {"id": "ghl_revenue", "label": "GHL Revenue", "category": "GoHighLevel", "level": "group"},
                {"id": "ghl_won_opps", "label": "Won Opps", "category": "GoHighLevel", "level": "group"},
                {"id": "ghl_lost_opps", "label": "Lost Opps", "category": "GoHighLevel", "level": "group"},
                {"id": "ghl_open_opps", "label": "Open Opps", "category": "GoHighLevel", "level": "group"},
                {"id": "ghl_abandoned_opps", "label": "Abandoned Opps", "category": "GoHighLevel", "level": "group"},
                {"id": "ghl_total_opps", "label": "Total Opps", "category": "GoHighLevel", "level": "group"},
                {"id": "conversion_rate", "label": "Conversion Rate", "category": "Calculated", "level": "group"},
                {"id": "cost_per_lead", "label": "Cost Per Lead", "category": "Calculated", "level": "group"},
                {"id": "engagement_rate", "label": "Engagement Rate", "category": "Calculated", "level": "group"},
                # Campaign-level (Marketing Hub — Campaigns/AdSets/Ads)
                {"id": "spend", "label": "Spend", "category": "Campaigns", "level": "campaign"},
                {"id": "impressions", "label": "Impressions", "category": "Campaigns", "level": "campaign"},
                {"id": "clicks", "label": "Clicks", "category": "Campaigns", "level": "campaign"},
                {"id": "reach", "label": "Reach", "category": "Campaigns", "level": "campaign"},
                {"id": "results", "label": "Results", "category": "Campaigns", "level": "campaign"},
                {"id": "leads", "label": "Leads", "category": "Campaigns", "level": "campaign"},
                {"id": "ctr", "label": "CTR", "category": "Campaigns", "level": "campaign"},
                {"id": "cpc", "label": "CPC", "category": "Campaigns", "level": "campaign"},
                {"id": "cpm", "label": "CPM", "category": "Campaigns", "level": "campaign"},
                {"id": "frequency", "label": "Frequency", "category": "Campaigns", "level": "campaign"},
                # Lead-level (Leads Hub / Marketing Hub Leads tab)
                {"id": "opportunityValue", "label": "Opportunity Value", "category": "Lead Fields", "level": "lead"},
            ],
            "tags": sorted(tags),
            "level_dashboards": {
                "group": ["clients"],
                "campaign": ["campaigns", "adsets", "ads"],
                "lead": ["leads", "marketing_leads"],
            },
        }


@router.post("/api/custom-metrics")
async def create_custom_metric(
    request: CreateCustomMetricRequest,
    current_user: str = Depends(get_current_user),
):
    """Create a new custom metric."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        metric_id = f"custom_{current_user}_{int(datetime.utcnow().timestamp() * 1000)}"

        metric_doc = {
            "id": metric_id,
            "name": request.name,
            "description": request.description or "",
            "formula_parts": request.formula_parts,
            "formula_display": request.formula_display or "",
            "dashboards": request.dashboards or [],
            "format_type": request.format_type or "integer",
            "aggregation": request.aggregation or "total",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        await db["users"].update_one(
            {"user_id": current_user},
            {"$push": {"custom_metrics": metric_doc}},
            upsert=True,
        )

        logger.info(f"Created custom metric {metric_id} for user {current_user}")
        return {"success": True, "metric": metric_doc}


@router.patch("/api/custom-metrics/{metric_id}")
async def update_custom_metric(
    metric_id: str,
    request: UpdateCustomMetricRequest,
    current_user: str = Depends(get_current_user),
):
    """Update an existing custom metric."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        user = await db["users"].find_one(
            {"user_id": current_user},
            {"custom_metrics": 1}
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        metrics = user.get("custom_metrics", [])
        found = False
        for m in metrics:
            if m.get("id") == metric_id:
                if request.name is not None:
                    m["name"] = request.name
                if request.description is not None:
                    m["description"] = request.description
                if request.formula_parts is not None:
                    m["formula_parts"] = request.formula_parts
                if request.formula_display is not None:
                    m["formula_display"] = request.formula_display
                if request.dashboards is not None:
                    m["dashboards"] = request.dashboards
                if request.format_type is not None:
                    m["format_type"] = request.format_type
                if request.aggregation is not None:
                    m["aggregation"] = request.aggregation
                if request.enabled is not None:
                    m["enabled"] = request.enabled
                m["updated_at"] = datetime.utcnow().isoformat()
                found = True
                break

        if not found:
            raise HTTPException(status_code=404, detail="Metric not found")

        await db["users"].update_one(
            {"user_id": current_user},
            {"$set": {"custom_metrics": metrics}},
        )

        logger.info(f"Updated custom metric {metric_id} for user {current_user}")
        return {"success": True, "metric_id": metric_id}


@router.delete("/api/custom-metrics/{metric_id}")
async def delete_custom_metric(
    metric_id: str,
    current_user: str = Depends(get_current_user),
):
    """Delete a custom metric."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        result = await db["users"].update_one(
            {"user_id": current_user},
            {"$pull": {"custom_metrics": {"id": metric_id}}},
        )

        if result.modified_count == 0:
            raise HTTPException(status_code=404, detail="Metric not found")

        logger.info(f"Deleted custom metric {metric_id} for user {current_user}")
        return {"success": True, "message": "Metric deleted"}
