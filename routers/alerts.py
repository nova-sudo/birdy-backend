import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from core.constants import METRIC_LABELS
from core.database import DB_NAME
from core.models import CreateAlertRequest, UpdateAlertRequest, SnoozeAlertRequest
from core.utils import mongo_to_dict
from dependencies import get_mongo_client, get_current_user
from services.alert_service import evaluate_alert, format_condition_display

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/alerts")
async def list_alerts(current_user: str = Depends(get_current_user)):
    """Return all alerts for the current user, split by status."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        alerts = await db["alerts"].find({"user_id": current_user}).sort("created_at", -1).to_list(None)

        result = []
        for a in alerts:
            d = mongo_to_dict(a)
            d["condition_display"] = format_condition_display(d.get("condition", {}))
            d["metric_label"]      = METRIC_LABELS.get(d.get("condition", {}).get("metric", ""), "Unknown")
            # Ensure progress fields always present (may be 0 until first evaluate)
            d.setdefault("current_value", 0.0)
            d.setdefault("progress_pct",  0.0)
            result.append(d)

        active    = [a for a in result if a.get("status") == "active"]
        triggered = [a for a in result if a.get("status") == "triggered"]
        paused    = [a for a in result if a.get("status") == "paused"]

        return {
            "alerts": result,
            "active": active,
            "triggered": triggered,
            "paused": paused,
            "counts": {
                "total": len(result),
                "active": len(active),
                "triggered": len(triggered),
                "paused": len(paused),
            }
        }


@router.post("/api/alerts")
async def create_alert(request: CreateAlertRequest, current_user: str = Depends(get_current_user)):
    """Create a new alert."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert_id = f"alert_{current_user}_{int(datetime.utcnow().timestamp() * 1000)}"

        # Fetch target group names for display
        group_names = []
        if request.target_group_ids:
            groups = await db["client_groups"].find(
                {"id": {"$in": request.target_group_ids}, "user_id": current_user},
                {"name": 1, "id": 1}
            ).to_list(None)
            group_names = [g["name"] for g in groups]

        alert_doc = {
            "id": alert_id,
            "user_id": current_user,
            "name": request.name,
            "description": request.description or "",
            "type": request.type or "warning",
            "condition": request.condition.model_dump(),
            "target_group_ids": request.target_group_ids or [],
            "target_group_names": group_names,
            "notification_channels": request.notification_channels or ["in_app"],
            "frequency": request.frequency or "daily",
            "status": "active",   # active | paused | triggered
            "last_triggered_at": None,
            "last_evaluated_at": None,
            "trigger_count": 0,
            "snoozed_until": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        await db["alerts"].insert_one(alert_doc)
        logger.info(f"Created alert {alert_id} for user {current_user}")

        # Auto-evaluate immediately so the user sees a value right away
        try:
            eval_result = await evaluate_alert(alert_doc, mongo_client)
            update = {
                "last_evaluated_at": datetime.utcnow(),
                "last_eval_result":  eval_result,
                "current_value":     eval_result.get("current_value", 0.0),
                "progress_pct":      eval_result.get("progress_pct", 0.0),
                "updated_at":        datetime.utcnow(),
            }
            if eval_result.get("triggered"):
                update["status"] = "triggered"
                update["last_triggered_at"] = datetime.utcnow()
            await db["alerts"].update_one({"id": alert_id}, {"$set": update})
            alert_doc.update(update)
            logger.info(f"Auto-evaluated alert {alert_id}: {eval_result.get('message', '')}")
        except Exception as e:
            logger.warning(f"Auto-evaluation failed for {alert_id}: {e}")

        return {"success": True, "alert": mongo_to_dict(alert_doc), "message": "Alert created successfully"}


@router.patch("/api/alerts/{alert_id}")
async def update_alert(
    alert_id: str,
    request: UpdateAlertRequest,
    current_user: str = Depends(get_current_user)
):
    """Update an existing alert."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert = await db["alerts"].find_one({"id": alert_id, "user_id": current_user})
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        update_fields = {"updated_at": datetime.utcnow()}

        if request.name is not None:
            update_fields["name"] = request.name
        if request.description is not None:
            update_fields["description"] = request.description
        if request.condition is not None:
            update_fields["condition"] = request.condition.model_dump()
        if request.target_group_ids is not None:
            update_fields["target_group_ids"] = request.target_group_ids
            if request.target_group_ids:
                groups = await db["client_groups"].find(
                    {"id": {"$in": request.target_group_ids}, "user_id": current_user},
                    {"name": 1}
                ).to_list(None)
                update_fields["target_group_names"] = [g["name"] for g in groups]
            else:
                update_fields["target_group_names"] = []
        if request.notification_channels is not None:
            update_fields["notification_channels"] = request.notification_channels
        if request.type is not None:
            update_fields["type"] = request.type
        if request.frequency is not None:
            update_fields["frequency"] = request.frequency
        if request.status is not None:
            update_fields["status"] = request.status

        await db["alerts"].update_one({"id": alert_id}, {"$set": update_fields})
        updated = await db["alerts"].find_one({"id": alert_id})

        return {"success": True, "alert": mongo_to_dict(updated), "message": "Alert updated"}


@router.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: str, current_user: str = Depends(get_current_user)):
    """Delete an alert."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        result = await db["alerts"].delete_one({"id": alert_id, "user_id": current_user})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Alert not found")

        return {"success": True, "message": "Alert deleted"}


@router.post("/api/alerts/{alert_id}/snooze")
async def snooze_alert(
    alert_id: str,
    request: SnoozeAlertRequest,
    current_user: str = Depends(get_current_user)
):
    """Snooze a triggered alert for N hours."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert = await db["alerts"].find_one({"id": alert_id, "user_id": current_user})
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        snooze_until = datetime.utcnow() + timedelta(hours=request.hours)

        await db["alerts"].update_one(
            {"id": alert_id},
            {"$set": {
                "status": "paused",
                "snoozed_until": snooze_until,
                "updated_at": datetime.utcnow()
            }}
        )

        return {
            "success": True,
            "message": f"Alert snoozed until {snooze_until.isoformat()}",
            "snoozed_until": snooze_until.isoformat()
        }


@router.post("/api/alerts/{alert_id}/evaluate")
async def evaluate_alert_now(alert_id: str, current_user: str = Depends(get_current_user)):
    """Manually trigger evaluation of a single alert."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert = await db["alerts"].find_one({"id": alert_id, "user_id": current_user})
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        eval_result = await evaluate_alert(alert, mongo_client)

        update = {
            "last_evaluated_at": datetime.utcnow(),
            "last_eval_result":  eval_result,
            "current_value":     eval_result.get("current_value", 0.0),
            "progress_pct":      eval_result.get("progress_pct", 0.0),
            "updated_at":        datetime.utcnow(),
        }

        if eval_result["triggered"]:
            update["status"] = "triggered"
            update["last_triggered_at"] = datetime.utcnow()
            update["$inc"] = {"trigger_count": 1}

            # Save notification
            await db["alert_notifications"].insert_one({
                "alert_id": alert_id,
                "user_id": current_user,
                "message": eval_result["message"],
                "current_value": eval_result["current_value"],
                "triggered_at": datetime.utcnow(),
                "read": False
            })
        else:
            # Reset to active if it was triggered and is now OK
            if alert.get("status") == "triggered":
                update["status"] = "active"

        # Handle $inc separately
        inc = update.pop("$inc", None)
        await db["alerts"].update_one({"id": alert_id}, {"$set": update})
        if inc:
            await db["alerts"].update_one({"id": alert_id}, {"$inc": inc})

        return {
            "success": True,
            "alert_id": alert_id,
            "evaluation": eval_result
        }


@router.get("/api/alerts/notifications")
async def get_alert_notifications(
    unread_only: bool = True,
    current_user: str = Depends(get_current_user)
):
    """Get in-app notifications for triggered alerts."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        query = {"user_id": current_user}
        if unread_only:
            query["read"] = False

        notifications = await db["alert_notifications"].find(query).sort("triggered_at", -1).limit(50).to_list(None)

        return {
            "notifications": [mongo_to_dict(n) for n in notifications],
            "unread_count": await db["alert_notifications"].count_documents({"user_id": current_user, "read": False})
        }


@router.post("/api/alerts/notifications/mark-read")
async def mark_notifications_read(current_user: str = Depends(get_current_user)):
    """Mark all notifications as read."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await db["alert_notifications"].update_many(
            {"user_id": current_user, "read": False},
            {"$set": {"read": True}}
        )
        return {"success": True}
