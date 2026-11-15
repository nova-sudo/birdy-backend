import logging
import time
from datetime import datetime

from core.database import DB_NAME
from dependencies import get_mongo_client
from services.alert_service import evaluate_alert

logger = logging.getLogger(__name__)


def _channels(alert: dict) -> list:
    """The alert's channels; stored unvalidated, so anything unknown is simply ignored."""
    channels = alert.get("notification_channels")
    return channels if isinstance(channels, list) else []


async def _email_owner(db, alert: dict, eval_result: dict, note_id) -> None:
    """Email the owner once per firing. Never raises, never changes the alert's status;
    the outcome is recorded on the notification row (email_sent). No retries in v1."""
    sent = False
    try:
        from services import email_service
        sent = bool(await email_service.send_alert_email(db, alert, eval_result))
    except Exception as e:
        logger.error("alert %s: email failed for user %s: %s", alert.get("id"), alert.get("user_id"), type(e).__name__)
    logger.info("alert %s: email %s for user %s", alert.get("id"), "sent" if sent else "not sent", alert.get("user_id"))
    try:
        await db["alert_notifications"].update_one({"_id": note_id}, {"$set": {"email_sent": sent}})
    except Exception as e:
        logger.error("alert %s: could not record email_sent: %s", alert.get("id"), e)


async def evaluate_all_alerts():
    """Evaluate all active alerts every 15 minutes."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]

            alerts = await db["alerts"].find({"status": {"$in": ["active", "triggered"]}}).to_list(None)

            if not alerts:
                logger.info("✅ No active alerts to evaluate")
                return

            logger.info(f"🔔 Evaluating {len(alerts)} alerts...")

            for alert in alerts:
                try:
                    # Skip snoozed alerts
                    snoozed_until = alert.get("snoozed_until")
                    if snoozed_until and datetime.utcnow() < snoozed_until:
                        continue

                    eval_result = await evaluate_alert(alert, mongo_client)

                    update = {
                        "last_evaluated_at": datetime.utcnow(),
                        "last_eval_result": eval_result,
                        "current_value": eval_result.get("current_value", 0.0),
                        "progress_pct": eval_result.get("progress_pct", 0.0),
                        "updated_at": datetime.utcnow(),
                    }

                    from services.report_events import alert_fired
                    await alert_fired(db, alert, eval_result, wait=True)

                    if eval_result["triggered"]:
                        # Same transition test report_events.alert_fired uses: an alert that stays
                        # triggered is re-evaluated every pass but fires only once.
                        newly_triggered = alert.get("status") != "triggered"
                        update["status"] = "triggered"
                        update["last_triggered_at"] = datetime.utcnow()
                        await db["alerts"].update_one({"id": alert["id"]},
                                                      {"$set": update, "$inc": {"trigger_count": 1}})
                        note = await db["alert_notifications"].insert_one({
                            "alert_id": alert["id"],
                            "user_id": alert["user_id"],
                            "message": eval_result["message"],
                            "current_value": eval_result["current_value"],
                            "triggered_at": datetime.utcnow(),
                            "read": False
                        })
                        logger.info(f"🚨 Alert triggered: {alert['name']} — {eval_result['message']}")
                        if newly_triggered and "email" in _channels(alert):
                            await _email_owner(db, alert, eval_result, note.inserted_id)
                    else:
                        if alert.get("status") == "triggered":
                            update["status"] = "active"
                        await db["alerts"].update_one({"id": alert["id"]}, {"$set": update})

                except Exception as e:
                    logger.error(f"❌ Error evaluating alert {alert.get('id')}: {e}")

            logger.info(f"✅ Alert evaluation complete ({len(alerts)} alerts checked)")

        except Exception as e:
            logger.error(f"❌ Critical error in alert evaluation job: {e}", exc_info=True)
