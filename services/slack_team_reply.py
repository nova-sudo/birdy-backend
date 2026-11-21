"""
services/slack_team_reply.py
----------------------------
What both Slack entry points (routers/slack_events.py for messages,
routers/slack_interactions.py for :::ui form submits) share now that Slack
talks to the agent team (ai/team).

Every Slack turn goes to Birdy the orchestrator (SLACK_AGENT). run_chat does
the rest exactly as it does for the web chat: one debit for the whole reply
(every agent it involved, with the per-agent breakdown on the ledger row), the
archive row with the route and proposals, and the team's limits (asks per
reply, depth, the reply's time budget). A message that opens by naming an
agent ("media buyer, ...") still goes to Birdy, which asks that agent itself.

The reply is the usual answer blocks (services/slack_block_formatter.py), then:
  - one section per proposal (title + what it would change), and one context
    line saying they wait for approval in Birdy, linking to the page where the
    owning agent's proposals are approved. No approve buttons in Slack.
  - one context line built from the route: "Birdy asked Sales Coach · 12s ·
    41 credits" (alone: "Birdy · 6s · 38 credits").
"""

from core.config import FRONTEND_URL
from ai.team import definitions as team_defs
from services.slack_block_formatter import _MAX_BLOCKS, _prose_to_mrkdwn, build_blocks_from_reply
from services.slack_interaction_store import create_pending_interaction

SLACK_AGENT = team_defs.BIRDY

MAX_PROPOSALS_SHOWN = 5
PROPOSAL_DETAIL_MAX = 300
# Where the owner approves an agent's proposals in the app: that agent's Suggestions tab
# (birdy-beta src/components/agency/sections.js, the PendingProposals list).
PROPOSAL_PATHS = {
    team_defs.MEDIA_BUYER: "/media-buyer/suggestions",
    team_defs.SALES_COACH: "/sales-coach/suggestions",
}


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _seconds(value) -> str | None:
    try:
        s = float(value)
    except (TypeError, ValueError):
        return None
    s = max(1, round(s))
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _credits(value) -> str | None:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    shown = f"{c:,.0f}" if c >= 10 else f"{round(c, 1):g}"
    return f"{shown} credit" + ("" if shown == "1" else "s")


def route_line(route: dict | None) -> str | None:
    """"Birdy asked Sales Coach · 12s · 41 credits", or "Birdy · 6s · 38 credits" when it worked alone.
    Agents the route skipped (a limit stopped the ask) are left out; one that was asked but gave
    no answer says so."""
    if not isinstance(route, dict):
        return None
    steps = [s for s in route.get("steps") or [] if isinstance(s, dict)]
    if not steps:
        return None
    root = steps[0].get("name") or team_defs.NAMES.get(steps[0].get("agent"), "Birdy")
    asked = []
    for s in steps[1:]:
        if s.get("status") == "skipped":
            continue
        name = s.get("name") or team_defs.NAMES.get(s.get("agent"), s.get("agent") or "an agent")
        if s.get("status") == "failed":
            name += " (no answer)"
        if name not in asked:
            asked.append(name)
    head = f"{root} asked {', '.join(asked)}" if asked else root
    parts = [head, _seconds(route.get("seconds")), _credits(route.get("credits"))]
    return " · ".join(p for p in parts if p)


def route_block(route: dict | None) -> dict | None:
    line = route_line(route)
    if not line:
        return None
    return {"type": "context", "block_id": "team_route", "elements": [{"type": "mrkdwn", "text": line}]}


def _proposal_link(proposals: list[dict]) -> str:
    owners = list(dict.fromkeys(p.get("agent") for p in proposals if p.get("agent") in PROPOSAL_PATHS))
    if not owners:
        return f"<{FRONTEND_URL}|Open Birdy>"
    return " · ".join(f"<{FRONTEND_URL}{PROPOSAL_PATHS[o]}|Open {team_defs.NAMES[o]}'s suggestions>"
                      for o in owners)


def proposal_blocks(proposals: list[dict] | None) -> list[dict]:
    """One section per proposal and one context line: they wait for the owner's approval in Birdy."""
    proposals = [p for p in proposals or [] if isinstance(p, dict) and p.get("title")]
    if not proposals:
        return []
    blocks: list[dict] = [{"type": "divider"}]
    for i, p in enumerate(proposals[:MAX_PROPOSALS_SHOWN]):
        owner = team_defs.NAMES.get(p.get("agent"))
        title = f"*Proposal{f' for {owner}' if owner else ''}: {_clip(p['title'], 150)}*"
        detail = _clip(p.get("detail"), PROPOSAL_DETAIL_MAX)
        text = title + (f"\n{_prose_to_mrkdwn(detail)}" if detail else "")
        blocks.append({"type": "section", "block_id": f"team_proposal_{i}", "text": {"type": "mrkdwn", "text": text}})
    more = len(proposals) - MAX_PROPOSALS_SHOWN
    waits = ("Nothing has changed yet. " + ("This waits" if len(proposals) == 1 else "These wait")
             + " for your approval in Birdy" + (f" ({more} more there)" if more > 0 else "")
             + ": " + _proposal_link(proposals))
    blocks.append({"type": "context", "block_id": "team_proposals_note", "elements": [{"type": "mrkdwn", "text": waits}]})
    return blocks


def build_team_reply(result: dict) -> tuple[list[dict], str, list[dict]]:
    """(blocks, fallback_text, ui_pending) for a team reply: the answer, its proposals, the route line."""
    blocks, fallback_text, ui_pending = build_blocks_from_reply(result["reply"], iid_prefix=result["session_id"])
    tail = proposal_blocks(result.get("proposals"))
    line = route_block(result.get("route"))
    if line:
        tail.append(line)
    # The answer gives way so the tail always fits in Slack's per-message block limit.
    blocks = blocks[: _MAX_BLOCKS - len(tail)] + tail
    n = len([p for p in result.get("proposals") or [] if isinstance(p, dict) and p.get("title")])
    if n:
        fallback_text += f" ({n} proposal{'s' if n != 1 else ''} waiting for your approval in Birdy)"
    return blocks, fallback_text, ui_pending


async def prepare_reply(db, result: dict, *, team_id: str, channel_id: str, thread_ts: str,
                        slack_user_id: str | None, birdy_user_id: str) -> tuple[list[dict], str]:
    """The blocks and fallback text to post, after saving the pending record each :::ui form needs."""
    blocks, fallback_text, ui_pending = build_team_reply(result)
    for pending in ui_pending:
        await create_pending_interaction(
            db, pending["iid"],
            team_id=team_id, channel_id=channel_id, thread_ts=thread_ts,
            slack_user_id=slack_user_id, birdy_user_id=birdy_user_id,
            session_id=result["session_id"], fields=pending["fields"], mode=pending["mode"],
        )
    return blocks, fallback_text
