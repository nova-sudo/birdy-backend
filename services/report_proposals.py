"""
services/report_proposals.py
----------------------------
A new Slack report made from the in-app chat waits for the owner's approval, with a preview
(the owner's pick 1A). Any agent's `create_slack_report` in its chat, and a delegated agent's
`propose_action` with kind create_slack_report, run every check a create runs (the fields, the
template, the account's report limit and the agent's area, services/report_owner.py) and store a
`kind: "slack_report"` proposal instead of the report:

    {id, agent: the report's owner, title: its name, detail, kind: "slack_report",
     payload: {name, owner, trigger, channel_id, channel_name, enabled, template},   (cleaned)
     sends_to: {channel_id, channel_name, default_channel}, schedule_label, status: "pending",
     proposed_by, client_group_id}

The app's card loads the preview itself (POST /api/report-designer/preview with the template and
owner). Approving it (routers/chat.apply_proposal) creates the report here, deterministically, with
no agent run: the checks run again first, since the account may have changed meanwhile.
"""

import uuid

from pydantic import ValidationError

from services import report_blocks as rt
from services import report_owner
from services import slack_report_ops as ops

KIND = "slack_report"
FIELDS = ("name", "trigger", "channel_id", "channel_name", "enabled", "template")
PROPOSED_NOTE = ("Proposed; waiting for the owner's approval. Nothing is saved yet: the owner sees the report "
                 "with a live Slack preview below your answer and approves or declines it there. Tell them it "
                 "is waiting for their approval, and don't call create_slack_report again for it.")


def error_text(e: ops.ReportError) -> str:
    """A refusal in words an agent acts on: a template's errors listed, else the detail."""
    d = e.detail
    if isinstance(d, dict) and d.get("errors"):
        return ("The report content isn't valid, so nothing was saved. Fix these and call again: "
                + "; ".join(str(x) for x in d["errors"]))
    return str(d)


def body_of(owner: str, fields: dict) -> ops.ReportBody:
    """The create request, from a tool's arguments or a proposal's payload; a bad value is a ReportError."""
    try:
        return ops.ReportBody(**{k: fields[k] for k in FIELDS if fields.get(k) is not None}, owner=owner)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in e.errors())
        raise ops.ReportError(400, f"Invalid report: {problems}")


def check_area(owner: str, name, template) -> None:
    why = report_owner.refusal(owner, report_owner.report_area(template if isinstance(template, dict) else None,
                                                               name))
    if why:
        raise ops.ReportError(403, why)


async def build(db, user_id: str, owner: str, fields: dict, *, proposed_by: str | None = None,
                client_group_id: str | None = None) -> dict:
    """The pending proposal for a report `owner` would create; ReportError when a create would refuse it.
    Nothing is written."""
    check_area(owner, fields.get("name"), fields.get("template"))
    report = await ops.prepare_report(db, user_id, body_of(owner, fields))
    payload = {"name": report["name"], "owner": owner, "trigger": report["trigger"],
               "channel_id": report.get("channel_id"), "channel_name": report.get("channel_name"),
               "enabled": report.get("enabled", True), "template": report["template"]}
    return {
        "id": "p_" + uuid.uuid4().hex[:12],
        "agent": owner,
        "title": report["name"],
        "detail": "; ".join(rt.describe(report["template"]))[:1000],
        "kind": KIND,
        "payload": payload,
        "sends_to": await ops.sends_to(db, user_id, payload["channel_id"], payload["channel_name"]),
        "schedule_label": ops.schedule_label(report["trigger"]),
        "status": "pending",
        "proposed_by": proposed_by or owner,
        "client_group_id": client_group_id,
    }


async def apply(db, user_id: str, proposal: dict) -> dict:
    """Create the approved report: {"ok": True, "report": {id, owner, name}}. ReportError when it no longer
    passes (the report limit was reached, a client was removed, ...)."""
    payload = proposal.get("payload") or {}
    owner = payload.get("owner") or proposal.get("agent")
    if owner not in report_owner.NAMES:
        raise ops.ReportError(422, "proposal_has_no_owner")
    check_area(owner, payload.get("name"), payload.get("template"))
    made = (await ops.create_report(db, user_id, body_of(owner, payload)))["report"]
    return {"ok": True, "report": {"id": made["id"], "owner": made["owner"], "name": made["name"]}}
