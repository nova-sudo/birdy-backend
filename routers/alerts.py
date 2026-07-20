import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from core.constants import METRIC_LABELS
from core.database import DB_NAME
from core.models import CreateAlertRequest, UpdateAlertRequest, SnoozeAlertRequest, SnoozeClientRequest
from core.utils import mongo_to_dict
from dependencies import get_mongo_client, get_current_user
from services.alert_service import evaluate_alert, format_condition_display

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Fan-out helper ────────────────────────────────────────────────────────────

def _expand_alert(alert: dict) -> list[dict]:
    """
    For per-client triggered alerts, fan out into one virtual row per triggered
    client.  Each virtual row is a shallow copy of the alert dict plus extra
    _virtual_* fields the frontend reads.

    For every other alert (total mode, or per-client but not yet triggered)
    the list is returned unchanged so the caller can treat all cases uniformly.
    """
    if (
        alert.get("tracking_mode") != "per_client"
        or alert.get("status") != "triggered"
    ):
        return [alert]

    per_client_results: list[dict] = alert.get("per_client_results", [])

    # Respect per-client snoozes: filter out clients whose snooze is still active
    now = datetime.utcnow()
    snoozed_clients: dict = alert.get("snoozed_clients", {})

    triggered_clients = [
        r for r in per_client_results
        if r.get("triggered") and not _is_client_snoozed(r.get("group_id", ""), snoozed_clients, now)
    ]

    if not triggered_clients:
        # All triggered clients are snoozed — show the parent row as active-ish
        return [alert]

    virtual_rows = []
    for client in triggered_clients:
        row = dict(alert)   # shallow copy — safe, we only read these on the frontend
        row["_virtual"]         = True
        row["_client_id"]       = client.get("group_id", "")
        row["_client_name"]     = client.get("group_name", client.get("client_name", "Unknown"))
        row["_client_value"]    = client.get("current_value", client.get("value", 0.0))
        row["_client_progress"] = client.get("progress_pct", 100.0)
        # Stable composite key for React list rendering
        row["_virtual_id"]      = f"{alert['id']}::{row['_client_id']}"
        virtual_rows.append(row)

    return virtual_rows


def _is_client_snoozed(client_id: str, snoozed_clients: dict, now: datetime) -> bool:
    """Return True if this client_id has an active snooze on the alert doc."""
    snooze_until_str = snoozed_clients.get(client_id)
    if not snooze_until_str:
        return False
    try:
        snooze_until = datetime.fromisoformat(snooze_until_str.replace("Z", "+00:00"))
        # Make both offset-naive for comparison
        snooze_until = snooze_until.replace(tzinfo=None)
        return now < snooze_until
    except (ValueError, AttributeError):
        return False


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/alerts")
async def list_alerts(current_user: str = Depends(get_current_user)):
    """
    Return all alerts for the current user, split by status.

    Per-client alerts that are in 'triggered' status are fanned out into one
    virtual row per triggered client group so the frontend can render them as
    individual table rows with client-specific values and actions.
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        alerts = await db["alerts"].find({"user_id": current_user}).sort("created_at", -1).to_list(None)

        result = []
        for a in alerts:
            d = mongo_to_dict(a)
            d["condition_display"] = format_condition_display(d.get("condition", {}))
            d["metric_label"]      = METRIC_LABELS.get(d.get("condition", {}).get("metric", ""), "Unknown")
            d.setdefault("current_value",      0.0)
            d.setdefault("progress_pct",       0.0)
            d.setdefault("tracking_mode",      "total")
            d.setdefault("per_client_results", [])
            d.setdefault("snoozed_clients",    {})
            result.append(d)

        active    = []
        triggered = []
        paused    = []

        for alert in result:
            status = alert.get("status", "active")
            expanded = _expand_alert(alert)

            if status == "triggered":
                triggered.extend(expanded)
            elif status == "paused":
                paused.extend(expanded)
            else:
                active.extend(expanded)

        return {
            "alerts":    result,        # flat unmodified list (useful for debugging)
            "active":    active,
            "triggered": triggered,
            "paused":    paused,
            "counts": {
                "total":     len(result),
                "active":    len(active),
                "triggered": len(triggered),
                "paused":    len(paused),
            }
        }


@router.post("/api/alerts")
async def create_alert(request: CreateAlertRequest, current_user: str = Depends(get_current_user)):
    """Create a new alert."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert_id = f"alert_{current_user}_{int(datetime.utcnow().timestamp() * 1000)}"

        # Alerts only ever target active clients
        group_names = []
        target_group_ids = request.target_group_ids or []
        if target_group_ids:
            groups = await db["client_groups"].find(
                {"id": {"$in": target_group_ids}, "user_id": current_user, "client_status": {"$ne": "Inactive"}},
                {"name": 1, "id": 1}
            ).to_list(None)
            target_group_ids = [g["id"] for g in groups]
            group_names = [g["name"] for g in groups]

        alert_doc = {
            "id":                    alert_id,
            "user_id":               current_user,
            "name":                  request.name,
            "description":           request.description or "",
            "type":                  request.type or "warning",
            "condition":             request.condition.model_dump(),
            "target_group_ids":      target_group_ids,
            "target_group_names":    group_names,
            "notification_channels": request.notification_channels or ["in_app"],
            "frequency":             request.frequency or "daily",
            "tracking_mode":         request.tracking_mode or "total",
            "status":                "active",
            "last_triggered_at":     None,
            "last_evaluated_at":     None,
            "trigger_count":         0,
            "snoozed_until":         None,
            # per-client state
            "per_client_results":    [],
            "snoozed_clients":       {},
            "created_at":            datetime.utcnow(),
            "updated_at":            datetime.utcnow(),
        }

        await db["alerts"].insert_one(alert_doc)
        logger.info(f"Created alert {alert_id} for user {current_user} (tracking_mode={alert_doc['tracking_mode']})")

        # Auto-evaluate immediately
        try:
            eval_result = await evaluate_alert(alert_doc, mongo_client)
            update = {
                "last_evaluated_at":  datetime.utcnow(),
                "last_eval_result":   eval_result,
                "current_value":      eval_result.get("current_value", 0.0),
                "progress_pct":       eval_result.get("progress_pct", 0.0),
                "per_client_results": eval_result.get("per_client_results", []),
                "updated_at":         datetime.utcnow(),
            }
            if eval_result.get("triggered"):
                update["status"]            = "triggered"
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
            if request.target_group_ids:
                groups = await db["client_groups"].find(
                    {"id": {"$in": request.target_group_ids}, "user_id": current_user, "client_status": {"$ne": "Inactive"}},
                    {"name": 1, "id": 1}
                ).to_list(None)
                update_fields["target_group_ids"] = [g["id"] for g in groups]
                update_fields["target_group_names"] = [g["name"] for g in groups]
            else:
                update_fields["target_group_ids"] = []
                update_fields["target_group_names"] = []
        if request.notification_channels is not None:
            update_fields["notification_channels"] = request.notification_channels
        if request.type is not None:
            update_fields["type"] = request.type
        if request.frequency is not None:
            update_fields["frequency"] = request.frequency
        if request.status is not None:
            update_fields["status"] = request.status
        if request.tracking_mode is not None:
            update_fields["tracking_mode"] = request.tracking_mode

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
    """Snooze an entire alert for N hours (pauses all clients)."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert = await db["alerts"].find_one({"id": alert_id, "user_id": current_user})
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        snooze_until = datetime.utcnow() + timedelta(hours=request.hours)

        await db["alerts"].update_one(
            {"id": alert_id},
            {"$set": {
                "status":        "paused",
                "snoozed_until": snooze_until,
                "updated_at":    datetime.utcnow(),
            }}
        )

        return {
            "success":       True,
            "message":       f"Alert snoozed until {snooze_until.isoformat()}",
            "snoozed_until": snooze_until.isoformat(),
        }


@router.post("/api/alerts/{alert_id}/snooze-client")
async def snooze_alert_client(
    alert_id: str,
    request: SnoozeClientRequest,
    current_user: str = Depends(get_current_user)
):
    """
    Snooze a single client within a per-client alert.

    The alert itself stays active and continues evaluating all other clients.
    The snoozed client's virtual row will be hidden from the triggered list
    until the snooze expires; the next evaluation will re-surface it if it is
    still above the threshold.
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert = await db["alerts"].find_one({"id": alert_id, "user_id": current_user})
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        if alert.get("tracking_mode") != "per_client":
            raise HTTPException(
                status_code=400,
                detail="snooze-client is only valid for per_client tracking mode alerts. "
                       "Use /snooze to snooze the whole alert."
            )

        snooze_until = datetime.utcnow() + timedelta(hours=request.hours)

        # Store as a nested map: snoozed_clients.{client_id} = ISO timestamp
        await db["alerts"].update_one(
            {"id": alert_id},
            {"$set": {
                f"snoozed_clients.{request.client_id}": snooze_until.isoformat(),
                "updated_at": datetime.utcnow(),
            }}
        )

        logger.info(
            f"Snoozed client {request.client_id} on alert {alert_id} "
            f"for {request.hours}h (until {snooze_until.isoformat()})"
        )

        return {
            "success":       True,
            "message":       f"Client snoozed until {snooze_until.isoformat()}",
            "client_id":     request.client_id,
            "snoozed_until": snooze_until.isoformat(),
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
            "last_evaluated_at":  datetime.utcnow(),
            "last_eval_result":   eval_result,
            "current_value":      eval_result.get("current_value", 0.0),
            "progress_pct":       eval_result.get("progress_pct", 0.0),
            # Always persist the latest per-client breakdown so _expand_alert
            # can build correct virtual rows on the next list_alerts call
            "per_client_results": eval_result.get("per_client_results", []),
            "updated_at":         datetime.utcnow(),
        }

        if eval_result["triggered"]:
            update["status"]            = "triggered"
            update["last_triggered_at"] = datetime.utcnow()
            update["$inc"]              = {"trigger_count": 1}

            # In per-client mode, create one notification per triggered client
            per_client = eval_result.get("per_client_results", [])
            if per_client:
                for pc in per_client:
                    if pc.get("triggered"):
                        await db["alert_notifications"].insert_one({
                            "alert_id":      alert_id,
                            "user_id":       current_user,
                            "message":       f"[{pc['group_name']}] {pc['message']}",
                            "current_value": pc.get("current_value", pc.get("value", 0.0)),
                            "group_id":      pc.get("group_id"),
                            "group_name":    pc.get("group_name"),
                            "triggered_at":  datetime.utcnow(),
                            "read":          False,
                        })
            else:
                await db["alert_notifications"].insert_one({
                    "alert_id":      alert_id,
                    "user_id":       current_user,
                    "message":       eval_result["message"],
                    "current_value": eval_result.get("current_value", 0.0),
                    "triggered_at":  datetime.utcnow(),
                    "read":          False,
                })
        else:
            if alert.get("status") == "triggered":
                update["status"] = "active"

        inc = update.pop("$inc", None)
        await db["alerts"].update_one({"id": alert_id}, {"$set": update})
        if inc:
            await db["alerts"].update_one({"id": alert_id}, {"$inc": inc})

        return {
            "success":    True,
            "alert_id":   alert_id,
            "evaluation": eval_result,
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
            "unread_count":  await db["alert_notifications"].count_documents(
                {"user_id": current_user, "read": False}
            ),
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

@router.post("/api/alerts/{alert_id}/dismiss-client")
async def dismiss_alert_client(
    alert_id: str,
    request: SnoozeClientRequest,
    current_user: str = Depends(get_current_user)
):
    """
    Permanently remove a single client's triggered entry from per_client_results.
    The alert will re-trigger for that client on the next evaluation if the
    condition is still met.
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        alert = await db["alerts"].find_one({"id": alert_id, "user_id": current_user})
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        if alert.get("tracking_mode") != "per_client":
            raise HTTPException(
                status_code=400,
                detail="dismiss-client is only valid for per_client tracking mode alerts.",
            )

        # Pull out just this client from per_client_results
        await db["alerts"].update_one(
            {"id": alert_id},
            {
                "$pull": {"per_client_results": {"group_id": request.client_id}},
                "$set": {"updated_at": datetime.utcnow()},
            }
        )

        # If no triggered clients remain, flip the alert back to active
        updated = await db["alerts"].find_one({"id": alert_id})
        remaining_triggered = [
            r for r in (updated.get("per_client_results") or [])
            if r.get("triggered")
        ]
        if not remaining_triggered and updated.get("status") == "triggered":
            await db["alerts"].update_one(
                {"id": alert_id},
                {"$set": {"status": "active", "updated_at": datetime.utcnow()}}
            )

        logger.info(f"Dismissed client {request.client_id} from alert {alert_id}")
        return {"success": True, "client_id": request.client_id, "message": "Triggered entry removed"}