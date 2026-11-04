"""
ai/agents/stock/slack.py
------------------------
The stock agent in Slack (spec section 8): the run's summary with a reply per card, urgent alerts, the
buttons, the edit form and @Birdy replies.

Posting. `publish_run` and `post_urgent_alerts` run repeatedly, so what has been posted is stored straight
after each post: `run.slack.ts`, `card.slack.ts` and `run.urgent_posted_keys`. A repeat posts only what has no
ts, and updates the summary. A kill between a post and its save can repeat that one message. Both bound their
own time. A failed publish doesn't hold the run: it finishes, the report shows the error, and `retry_unposted_runs`
(every agents tick) posts what's missing, marked late, slowly for 24 hours or as soon as a dead token is reconnected.
That runs outside the run's state machine, under the run's claim, and never counts against `run.attempts`.

Who pressed. A button only says which Slack team it came from. That maps to one Birdy account, and every card
and run is looked up under that account, so a press from anywhere else finds nothing and is ignored. Anyone in
the channel's workspace may press (spec section 8), and `decided_by` records them: the payload's `user.name`, else
`<@id>`, with `by_id`. In a Slack Connect channel someone from another organisation can see these buttons, so a press
whose `user.team_id` isn't the workspace's is refused, with a note.

Answering in time. Slack wants an answer in 3 s, and on Vercel nothing is promised after the response (see
routers/slack_events.py). So:
  Apply, Apply all   atomic claim in the request, with a hard budget. What doesn't fit stays queued for the cron.
  Undo, Dismiss      can take longer than Slack allows, so the press is stored as a row in SLACK_ACTIONS and
                     answered. The route tries the work straight after the response (`finish_action`). If that
                     never runs, the per-minute cron does it. The row is claimed, so it runs once. A frozen claim is
                     taken over after ACTION_RECLAIM. A press is never dropped silently: `busy` keeps its row, and
                     one that can't happen tells the person, as a note and on the card.

Keeping messages true. `guarded_update` marks a card `slack.needs_update` on every write, whichever path made it
(Slack, the dashboard, Auto, the cron), and `sync_card_messages` updates those messages each minute. A press also
refreshes its own message at once when that fits in the time left.

Open questions. Birdy can't read ordinary replies without `channels:history`, which spec section 8 rules out, so
it can't ask a question and hear the answer. `open_questions` stays empty, as the runner passes it. What a person
tells Birdy comes through @Birdy and reaches the writer as a lesson.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

import services.report_blocks as rb
import services.report_template as rt
from ai.agents.stock import lessons
from ai.agents.stock.alerts import _alert_key
from ai.agents.stock.apply import (STUCK_AFTER, apply_card, apply_run, dismiss_card, has_newer_card, recover_stale_undo,
                                   undo_line)
from ai.agents.stock.cards import (DROPPABLE_STATES, STATE_APPLIED, STATE_APPLYING, STATE_BLOCKED, STATE_FAILED, STATE_FLAGGED,
                                   STATE_NEEDS_FILE, STATE_PROPOSED, STATE_QUEUED, STATE_UNDOING, STATE_UNDONE,
                                   STATUS_CLOSING, STATUS_DISMISSED, STATUS_FLAGGED, STATUS_OPEN, STATUS_SUPERSEDED,
                                   _name_pattern, guarded_update)
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
from ai.agents.stock.templates import (_VERBS, BUSY_STATES, FREQUENCY, assemble, card_buttons, card_values, make_context,
                                       render_kind_safe, report_url, rows_from_cards, summary_buttons, summary_values)
from ai.agents.store import AGENTS, CARDS, MEMORY, RUNS, SLACK_ACTIONS
from services.slack_bot_service import (DEAD_TOKEN_ERRORS, get_decrypted_bot_token_for_user, get_slack_bot_status,
                                        get_user_id_by_team_id, mark_needs_reconnect)

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://birdy-beta.vercel.app").rstrip("/")
PREVIEW, ALERT_ONLY = "preview", "alert_only"
OPEN_REPORT, APPLY_ALL, CARD_APPLY, CARD_EDIT, CARD_DISMISS = ("agent_open_report", "agent_run_apply_all", "agent_card_apply",
                                                               "agent_card_edit", "agent_card_dismiss")
CARD_UNDO_PREFIX = "agent_card_undo:"
LESSON_UNDO = "lesson_undo"
EDIT_CALLBACK = "agent_card_edit"
_ACTION_IDS = (OPEN_REPORT, APPLY_ALL, CARD_APPLY, CARD_EDIT, CARD_DISMISS, LESSON_UNDO)

SLACK_TIMEOUT_S = 10          # one Slack call; a posting loop checks its deadline between calls
PUBLISH_MAX_S = 60            # one publish, when the tick's deadline doesn't say less
URGENT_TIMEOUT_S = 20         # the alerts pass waits on this, and a kill here counts against the run
DEADLINE_MARGIN_S = 10        # how far past the tick's deadline an urgent post may run
RATE_LIMIT_MAX_WAIT_S = 10    # a longer Retry-After waits for the next tick
PRESS_DEADLINE_S = 2.5        # a press is answered by this, whatever it hit. Slack gives up at 3 s
ANSWER_RESERVE_S = 0.9        # of that, kept for updating the message and sending a note once the work is cut off
APPLY_BUDGET_S = 1.5          # of what's left. What doesn't fit stays queued for the cron
APPLY_ALL_BUDGET_S = 1.4      # queues only: apply_run starts no line on a budget under MIN_TIME_FOR_LINE_S
REFRESH_BUDGET_S = 0.6        # a message not updated in this time is updated by the cron
EDIT_OPEN_BUDGET_S = 2.0      # Slack's trigger for the form is gone after 3 s anyway
NOTE_BUDGET_S = 0.4           # an ephemeral note while Slack is waiting for the answer
ACTION_RECLAIM = timedelta(seconds=90)  # a claimed press older than this is frozen. With the minute cron, about 2 minutes.
                                        # apply.UNDO_SEND_WINDOW is what lets this be short of Vercel's 300 s
ACTION_TRIES = 5              # a stored Undo or Dismiss, before the person is told it didn't happen
ACTION_MAX_AGE = timedelta(minutes=10)  # and a press this old isn't what the person wants any more
ACTION_BATCH = 20
ACTION_UPDATE_S = 1.5         # the follow-ups of a stored press: the cron's sync repeats either
ACTION_TELL_S = 1.0
PRESS_CAP_S = 5.0             # of the 10 s cron tick, so one Undo fits apply.MIN_TIME_FOR_LINE_S. The rest is for Meta's lines
SYNC_CALL_S = 2.0             # one message in the cron's update pass
SYNC_MAX_TIMEOUTS = 2         # that many and Slack is down: the pass stops, the messages stay due
RETRY_MINUTES = (15, 30, 60, 120, 240)  # a run's unposted messages, then every 4 hours
RETRY_FOR = timedelta(hours=24)
RETRY_BATCH = 50
SLACK_CLAIM = "slack"         # run.claim_kind while retry_unposted_runs holds a run
SUMMARY_GONE = {"message_not_found", "cant_update_message"}
UPDATE_TRIES = 5              # a card's chat.update, before the message is left as it is
SYNC_BATCH = 50

# Slack's answer when it refuses the blocks themselves: a template's fault, so the default goes instead.
REFUSED_BLOCKS = {"invalid_blocks", "invalid_blocks_format", "msg_too_long", "msg_blocks_too_long", "invalid_arguments"}
GONE_MESSAGE = {"message_not_found", "channel_not_found", "cant_update_message", "is_archived", "not_in_channel"}
STOPS_THE_PASS = {"channel_not_found", "not_in_channel", "is_archived", "ratelimited", "missing_scope"} | DEAD_TOKEN_ERRORS
NO_CHANNEL_ACCESS = ("Birdy can't post to this Slack channel. For a private channel, invite the Birdy bot to it, "
                     "or choose another channel in the agent's settings.")
NOT_CONNECTED = "Slack isn't connected, so this didn't reach Slack. The report and its actions are in Birdy."
NO_CHANNEL = "No Slack channel is chosen for this agent, so this didn't reach Slack. The report and its actions are in Birdy."
NEEDS_RECONNECT = "Slack needs reconnecting: it refused Birdy's connection. Reconnect it in Settings."
OUT_OF_TIME = "Birdy ran out of time posting to Slack. It carries on next tick."
TEMPLATE_NOTICE = ("This template couldn't be sent on {at}, so Birdy used the default layout for it. "
                   "Edit or reset the template to clear this.")
NOTICES = "slack_template_notices"  # on the agent: settings.slack only accepts what a person can set
DIDNT_HAPPEN = "Birdy couldn't do that, so it didn't happen. Try again from the report."
STILL_SETTLING = "Birdy couldn't confirm that yet. The card updates once it knows what happened in Meta."
OUTSIDER = "Only people in the workspace that connected Birdy can do this."
OUTSIDER_MENTION = "People outside this workspace can't teach this agent. Birdy didn't record that."
FORM_DIDNT_OPEN = "The edit form didn't open. Press Edit again."
POSTED_LATE = "Posted late: Slack couldn't take this when it was ready."
REFUSED_ANSWERS = {"not_found": "That card is no longer available.", "not_undoable": "This can't be undone.",
                   "busy": "Birdy is working on this right now. Try again in a moment.",
                   "already": "That was already done."}
_STATE_WORDS = {STATE_APPLIED: "applied", STATE_QUEUED: "queued", STATE_APPLYING: "applying", STATE_UNDOING: "undoing",
                STATE_UNDONE: "undone", STATE_FAILED: "failed", STATE_BLOCKED: "blocked",
                STATE_NEEDS_FILE: "needs its video", STATE_FLAGGED: "flag only"}
_MENTION_TOKEN = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
_MENTION_ONLY = re.compile(r"^<@[UW][A-Z0-9]{2,20}>$")
MAX_TEXT = 3000               # Slack's limit on a section's text and on a plain text input
SHORT_NAME = 3                # a client name this short is matched in exact case in what people say
ASK_NAMES_MAX = 20


class SlackPublishError(Exception):
    """A run's message didn't reach Slack. `retry`: trying again later could help. `reconnect`: nothing can until the
    token is replaced. `posted`: messages that did get there this time, so the attempt made progress."""

    def __init__(self, message: str, *, retry: bool = True, posted: int = 0, reconnect: bool = False):
        super().__init__(message)
        self.retry = retry and not reconnect
        self.posted = posted
        self.reconnect = reconnect


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _settings(agent: dict) -> dict:
    return merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})


def _esc(text) -> str:
    return rt._escape_all(str(text if text is not None else ""))


# ---------------------------------------------------------------------------
# Talking to Slack
# ---------------------------------------------------------------------------

def _code(e: SlackApiError) -> str | None:
    response = getattr(e, "response", None)
    try:
        return response.get("error")
    except AttributeError:
        return None


async def _client_for(db, user_id: str) -> AsyncWebClient | None:
    """None without an install, or with one Slack has already refused: asking again can't help."""
    status = await get_slack_bot_status(db, user_id)
    if not status or status.get("needs_reconnect"):
        return None
    token = await get_decrypted_bot_token_for_user(db, user_id)
    return AsyncWebClient(token=token, timeout=SLACK_TIMEOUT_S) if token else None


async def _note_api_error(db, user_id: str, e: SlackApiError) -> str | None:
    code = _code(e)
    if code in DEAD_TOKEN_ERRORS:
        await mark_needs_reconnect(db, user_id, code)
    return code


async def _call(fn, **kw):
    """One Slack call, waiting out a short rate limit once. Posting one message per card can hit it."""
    try:
        return await fn(**kw)
    except SlackApiError as e:
        if _code(e) != "ratelimited":
            raise
        headers = getattr(e.response, "headers", None) or {}
        try:
            wait = float(headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            wait = 1.0
        if wait > RATE_LIMIT_MAX_WAIT_S:
            raise
        await asyncio.sleep(wait)
        return await fn(**kw)


def _failure(e: SlackApiError, code: str | None, posted: int = 0) -> SlackPublishError:
    if code in DEAD_TOKEN_ERRORS:
        return SlackPublishError(NEEDS_RECONNECT, reconnect=True, posted=posted)
    if code in ("channel_not_found", "not_in_channel", "is_archived"):  # someone has to change something first
        return SlackPublishError(NO_CHANNEL_ACCESS, retry=False, posted=posted)
    return SlackPublishError(f"Slack refused the message: {code or e}", posted=posted)


async def _bounded(awaitable, seconds: float) -> None:
    """For work that a later tick repeats if it doesn't fit, while Slack is waiting for the answer. Never raises."""
    try:
        await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError:
        logger.info("stock slack: left a step for later after %ss", seconds)
    except Exception as e:  # noqa: BLE001 - the press itself has already been stored
        logger.warning("stock slack: a follow-up step failed: %s", e)


def _left(deadline: float) -> float:
    return deadline - time.monotonic()


async def _tell(db, user_id: str, channel: str | None, slack_user: str | None, text: str, thread_ts: str | None = None) -> bool:
    """A note only the person who pressed sees. Best effort: True if Slack took it."""
    if not channel or not slack_user:
        return False
    try:
        client = await _client_for(db, user_id)
        if client:
            await client.chat_postEphemeral(channel=channel, user=slack_user, text=_esc(text),
                                            **({"thread_ts": thread_ts} if thread_ts else {}))
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("stock slack: couldn't send a note: %s", e)
    return False


async def _note_on_card(db, card_id: str, text: str | None) -> None:
    """What a person has to be told, where they'll see it if a note can't reach them. Not a change to the card, so it
    leaves the version alone and an open edit form stays good."""
    await db[CARDS].update_one({"_id": card_id}, {"$set": {"press_note": text, "slack.needs_update": True}})


async def _tell_or_note(db, card_id: str | None, user_id: str, where: tuple, text: str, seconds: float) -> None:
    """Tell the person who pressed in the time left, or leave it on the card. Never raises."""
    channel, slack_user, thread_ts = where
    told = False
    try:
        told = await asyncio.wait_for(_tell(db, user_id, channel, slack_user, text, thread_ts), max(seconds, 0.01))
    except asyncio.TimeoutError:
        logger.info("stock slack: a note didn't go out in time")
    if not told and card_id:
        try:
            await _note_on_card(db, card_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("stock card %s: couldn't keep a note: %s", card_id, e)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _context(kind: str, agent: dict):
    """No chart groups: their labels aren't escaped for a template, and an agent message has no use for them."""
    currency = (_settings(agent).get("zones") or {}).get("currency") or "GBP"
    return make_context(FREQUENCY[kind], now_utc=datetime.now(timezone.utc), currency=currency, groups=[])


def _saved_template(agent: dict, kind: str) -> dict | None:
    return (_settings(agent).get("slack") or {}).get("templates", {}).get(kind)


async def _note_template_failure(db, agent: dict, kind: str) -> None:
    """Shown in the agent's Slack template settings until the template is saved or reset."""
    now = _utcnow()
    logger.warning("stock agent %s: its %s template failed, the default was used", agent["_id"], kind)
    notice = {"message": TEMPLATE_NOTICE.format(at=now.strftime("%d %b at %H:%M UTC")), "at": now}
    await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {f"{NOTICES}.{kind}": notice}})


async def _deliver(db, agent: dict, kind: str, render, send):
    """Send one message. `render(template)` gives (blocks, text, fell_back). A saved template that can't be
    rendered, or that Slack refuses, is replaced by the default and noted: a template never costs a report."""
    saved = _saved_template(agent, kind)
    blocks, text, fell_back = render(saved)
    if fell_back:
        await _note_template_failure(db, agent, kind)
    try:
        return await send(blocks, text)
    except SlackApiError as e:
        if saved is None or fell_back or _code(e) not in REFUSED_BLOCKS:
            raise
        await _note_template_failure(db, agent, kind)
        blocks, text, _ = render(None)
        return await send(blocks, text)


def _with_extras(blocks: list, extra: dict | None, buttons: dict | None) -> list:
    """The template's blocks, then what code adds. Those are never what gets cut when a message is too long."""
    if extra is None:
        return assemble(blocks, buttons)
    return assemble([*blocks[:rb.MAX_BLOCKS - 2], extra], buttons)


# ---------------------------------------------------------------------------
# The card message
# ---------------------------------------------------------------------------

def _shown_who(decision: dict | None) -> str:
    """A mention stays one, and a name can't become one. From the dashboard `by` is an account id, not a name."""
    decision = decision or {}
    if decision.get("source") == "dashboard":
        return "someone in Birdy"
    by = str(decision.get("by") or "someone")
    return by if _MENTION_ONLY.match(by) else _esc(by)


def _decision_text(card: dict) -> str | None:
    decision = card.get("decided_by")
    if not decision:
        return None
    if card.get("status") == STATUS_DISMISSED:
        return f"*Dismissed by {_shown_who(decision)}*"
    if decision.get("actor") == "birdy":
        return "*Applied automatically by Birdy*"
    return f"*Approved by {_shown_who(decision)}*"


def _line_result(line: dict) -> str | None:
    state = line.get("state")
    word = _STATE_WORDS.get(state)
    why = line.get("note") or line.get("undo_error") or line.get("error")
    if state == STATE_PROPOSED or word is None or (state == STATE_FLAGGED and not why):
        return None
    if state in (STATE_UNDONE, STATE_UNDOING) and line.get("undo_decision"):
        word += f" by {_shown_who(line['undo_decision'])}"
    verb = _VERBS.get(line.get("kind"), "Change")
    return f"• {verb} *{_esc(line.get('ad_name') or 'ad')}*: {word}" + (f". {_esc(why)}" if why else "")


def _results_block(card: dict) -> dict | None:
    """Who decided and what became of each line, with any line's note (say, that its mode is now Flag). Added by
    code, so a saved template can't lose it."""
    note = f"⚠️ {_esc(card['press_note'])}" if card.get("press_note") else None
    rows = [_decision_text(card), *(_line_result(ln) for ln in card.get("lines") or []), note]
    rows = [r for r in rows if r]
    if not rows:
        return None
    return {"type": "section", "text": {"type": "mrkdwn", "text": rb._cut("\n".join(rows), MAX_TEXT)}}


async def _card_renderer(db, agent: dict, card: dict):
    has_newer = await has_newer_card(db, card)

    def render(template):
        blocks, text, fell_back = render_kind_safe("card", template, card_values(card, FRONTEND_URL), None,
                                                   _context("card", agent))
        return _with_extras(blocks, _results_block(card), card_buttons(card, has_newer=has_newer)), text, fell_back

    return render


async def _post_card(db, run: dict, agent: dict, client, channel: str, card: dict) -> None:
    thread_ts = run["slack"]["ts"]

    async def send(blocks, text):
        return await _call(client.chat_postMessage, channel=channel, thread_ts=thread_ts, blocks=blocks, text=text)

    resp = await _deliver(db, agent, "card", await _card_renderer(db, agent, card), send)
    await db[CARDS].update_one({"_id": card["_id"]}, {"$set": {"slack": {
        "channel_id": channel, "ts": resp.get("ts"), "thread_ts": thread_ts, "needs_update": False,
        "posted_at": _utcnow()}}})
    # A press in Birdy while this was on its way isn't in the message, so it's still due.
    await db[CARDS].update_one({"_id": card["_id"], "version": {"$ne": card.get("version")}},
                               {"$set": {"slack.needs_update": True}})


async def _mark_update_failed(db, card: dict) -> None:
    tries = ((card.get("slack") or {}).get("update_tries") or 0) + 1
    await db[CARDS].update_one({"_id": card["_id"]}, {"$set": {"slack.update_tries": tries,
                                                               "slack.needs_update": tries < UPDATE_TRIES}})


async def update_card_message(db, card: dict) -> bool:
    """Bring a posted card's message up to date, from the stored card. Never raises. False when there's nothing
    to update or it failed, in which case the card stays due unless trying again can't help."""
    fresh = await db[CARDS].find_one({"_id": card["_id"]})
    slack = (fresh or {}).get("slack") or {}
    if not slack.get("ts") or fresh.get("preview") or fresh.get("status") == STATUS_CLOSING:
        return False
    never = {"$set": {"slack.needs_update": False}}
    try:
        client = await _client_for(db, fresh["user_id"])
        agent = await db[AGENTS].find_one({"_id": fresh.get("agent_id"), "user_id": fresh["user_id"]})
        if client is None or agent is None:
            await db[CARDS].update_one({"_id": fresh["_id"]}, never)
            return False

        async def send(blocks, text):
            return await _call(client.chat_update, channel=slack.get("channel_id"), ts=slack["ts"], blocks=blocks, text=text)

        await _deliver(db, agent, "card", await _card_renderer(db, agent, fresh), send)
    except SlackApiError as e:
        code = await _note_api_error(db, fresh["user_id"], e)
        logger.warning("stock card %s: Slack update failed: %s", fresh["_id"], code or e)
        if code in GONE_MESSAGE or code in DEAD_TOKEN_ERRORS:
            await db[CARDS].update_one({"_id": fresh["_id"]}, never)
        else:
            await _mark_update_failed(db, fresh)
        return False
    except Exception as e:  # noqa: BLE001 - a message is never worth failing what called this
        logger.warning("stock card %s: Slack update failed: %s", fresh["_id"], e, exc_info=True)
        await _mark_update_failed(db, fresh)
        return False
    # Only if nothing was written meanwhile: a later change still needs its own update.
    await db[CARDS].update_one({"_id": fresh["_id"], "version": fresh.get("version")},
                               {"$set": {"slack.needs_update": False, "slack.update_tries": 0}})
    return True


async def sync_card_messages(db, deadline: float) -> int:
    """Each minute: every posted card written to since its message was last updated, and those cards' summaries.
    Returns how many cards were updated."""
    query = {"slack.needs_update": True, "slack.ts": {"$nin": [None]}}
    cards = await db[CARDS].find(query).sort("updated_at", 1).limit(SYNC_BATCH).to_list(length=SYNC_BATCH)
    updated, runs, timeouts = 0, set(), 0

    async def timed(step) -> bool | None:
        try:
            return await asyncio.wait_for(step, min(SYNC_CALL_S, max(_left(deadline), 0.01)))
        except asyncio.TimeoutError:
            return None

    for card in cards:
        if time.monotonic() >= deadline or timeouts >= SYNC_MAX_TIMEOUTS:
            break
        done = await timed(update_card_message(db, card))
        timeouts += done is None
        if done:
            updated += 1
            runs.add(card.get("run_id"))
    for run_id in runs:
        if time.monotonic() >= deadline or timeouts >= SYNC_MAX_TIMEOUTS:
            break
        timeouts += await timed(refresh_summary(db, run_id)) is None
    return updated


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

def _open_alerts(run: dict) -> list[dict]:
    """Info alerts such as prepaid_no_topups and spend_data_late aren't open alerts."""
    return [a for a in run.get("alerts") or [] if isinstance(a, dict) and a.get("severity") != "info"]


def _all_clean(run: dict, cards: list[dict]) -> bool:
    summary = run.get("summary") or {}
    return (run.get("kind") == ALERT_ONLY and not cards and not _open_alerts(run)
            and not any(summary.get(k) for k in ("could_not_check", "no_card", "waiting", "missing", "health_unread",
                                                 "stale", "reconnect")))


def _all_clear_line(run: dict) -> str:
    n = len(run.get("clients") or {})
    return f"All clear for {run.get('local_date')}: {n} {'client' if n == 1 else 'clients'} checked, nothing needs you."


async def _client_names(db, run: dict) -> dict:
    gids = list(run.get("clients") or {})
    named = {a.get("client_group_id") for a in run.get("alerts") or [] if isinstance(a, dict)} - {None}
    rows = db["client_groups"].find({"id": {"$in": [*gids, *named]}, "user_id": run.get("user_id")}, {"id": 1, "name": 1})
    return {g["id"]: g.get("name") async for g in rows}


def _not_finished_lines(run: dict, names: dict) -> list[str]:
    """What the summary must not leave out: clients it says nothing about otherwise."""
    summary = run.get("summary") or {}

    def name(gid):
        return names.get(gid) or "A client"

    rows = [f"⏳ {name(g)}: health not read yet" for g in summary.get("health_unread") or []]
    rows += [f"⚠️ {name(c.get('client_group_id'))}: couldn't be checked. {c.get('reason') or ''}".rstrip()
             for c in summary.get("could_not_check") or []]
    rows += [f"⚪ {name(c.get('client_group_id'))}: {c.get('reason') or 'no card today'}" for c in summary.get("no_card") or []]
    rows += [f"⏳ {name(g)}: still waiting for data" for g in summary.get("waiting") or []]
    rows += [f"⚠️ {name(g)}: not finished" for g in summary.get("missing") or []]
    if len(rows) > 15:
        rows = [*rows[:15], f"and {len(rows) - 15} more, see the report"]
    if summary.get("stale"):
        rows.append("⚠️ Some ad statuses couldn't be refreshed, so stored ones were used")
    return rows


def _summary_notes_block(run: dict) -> dict | None:
    """What code adds under the summary, so a saved template can't lose it."""
    notes = []
    if run.get("slack_late"):
        notes.append(POSTED_LATE)
    if run.get("apply_all"):
        notes.append(f"*Apply all pressed by {_shown_who(run['apply_all'])}*")
    if not notes:
        return None
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": n} for n in notes]}


async def _summary_renderer(db, run: dict, agent: dict, cards: list[dict]):
    settings = _settings(agent)
    names = await _client_names(db, run)
    batch = next((b.get("name") for b in settings.get("batches") or [] if b.get("id") == run.get("batch_id")), None)
    values = summary_values(run, cards, tag_user_id=(settings.get("slack") or {}).get("also_tag_user_id"),
                            batch_name=batch, client_names=names)
    extra_lines = _not_finished_lines(run, names)
    if extra_lines:
        known = [] if values["alerts"] == "None" else [values["alerts"]]
        values["alerts"] = "\n".join([*known, *extra_lines])
    buttons = summary_buttons(run, FRONTEND_URL, waiting=int(values["cards_waiting"]))

    def render(template):
        blocks, text, fell_back = render_kind_safe("summary", template, values, rows_from_cards(cards), _context("summary", agent))
        return _with_extras(blocks, _summary_notes_block(run), buttons), text, fell_back

    return render


async def _save_run_ts(db, run: dict, channel: str, ts: str) -> None:
    run["slack"] = {"channel_id": channel, "ts": ts}
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"slack": run["slack"]}})


async def _send_summary(db, run: dict, agent: dict, client, channel: str, cards: list[dict]) -> None:
    ts = (run.get("slack") or {}).get("ts")
    if _all_clean(run, cards):
        text = _esc(_all_clear_line(run)) + (f" {POSTED_LATE}" if run.get("slack_late") else "")
        if ts:
            await _call(client.chat_update, channel=channel, ts=ts, text=text, blocks=[])
        else:
            resp = await _call(client.chat_postMessage, channel=channel, text=text)
            await _save_run_ts(db, run, channel, resp.get("ts"))
        return

    async def send(blocks, text):
        if ts:
            return await _call(client.chat_update, channel=channel, ts=ts, blocks=blocks, text=text)
        return await _call(client.chat_postMessage, channel=channel, blocks=blocks, text=text)

    resp = await _deliver(db, agent, "summary", await _summary_renderer(db, run, agent, cards), send)
    if not ts:
        await _save_run_ts(db, run, channel, resp.get("ts"))


async def refresh_summary(db, run_id) -> bool:
    """Update a posted summary from the run and its cards as they are now. Never raises."""
    try:
        run = await db[RUNS].find_one({"_id": run_id})
        slack = (run or {}).get("slack") or {}
        if not slack.get("ts"):
            return False
        client = await _client_for(db, run["user_id"])
        agent = await db[AGENTS].find_one({"_id": run.get("agent_id"), "user_id": run["user_id"]})
        if client is None or agent is None:
            return False
        cards = await db[CARDS].find({"run_id": run_id, "status": {"$ne": STATUS_CLOSING}}).to_list(length=None)
        await _send_summary(db, run, agent, client, slack["channel_id"], cards)
        return True
    except SlackApiError as e:
        logger.warning("stock run %s: summary update failed: %s", run_id, await _note_api_error(db, (run or {}).get("user_id"), e))
    except Exception as e:  # noqa: BLE001
        logger.warning("stock run %s: summary update failed: %s", run_id, e, exc_info=True)
    return False


# ---------------------------------------------------------------------------
# Publishing a run
# ---------------------------------------------------------------------------

async def _connection(db, run: dict, agent: dict) -> tuple:
    """(client, channel), or SlackPublishError when this can't reach Slack. The run's own channel wins once it
    has one, so a run stays in one thread if the setting changes under it."""
    channel = (run.get("slack") or {}).get("channel_id") or (_settings(agent).get("slack") or {}).get("channel_id")
    if not channel:
        raise SlackPublishError(NO_CHANNEL, retry=False)
    client = await _client_for(db, run["user_id"])
    if client is None:
        status = await get_slack_bot_status(db, run["user_id"])
        if status:
            raise SlackPublishError(NEEDS_RECONNECT, reconnect=True)
        raise SlackPublishError(NOT_CONNECTED, retry=False)
    return client, channel


def _waiting_to_post(card: dict) -> bool:
    return not ((card.get("slack") or {}).get("ts") or card.get("preview")
                or card.get("status") in (STATUS_CLOSING, STATUS_SUPERSEDED))


async def _summary_step(db, run: dict, agent: dict, client, channel: str, cards: list[dict]) -> SlackPublishError | None:
    """Post or update the summary. Without one the cards have no thread, so that raises. A failed update with a thread
    to reply in comes back as the failure to report, once the cards are out. If the summary was deleted, a new one goes."""
    had_one = bool((run.get("slack") or {}).get("ts"))
    try:
        await _send_summary(db, run, agent, client, channel, cards)
        return None
    except SlackApiError as e:
        code = await _note_api_error(db, run["user_id"], e)
        if had_one and code in SUMMARY_GONE:
            run["slack"] = {"channel_id": channel, "ts": None}
            try:
                await _send_summary(db, run, agent, client, channel, cards)
                return None
            except SlackApiError as again:
                raise _failure(again, await _note_api_error(db, run["user_id"], again)) from again
        failure = _failure(e, code)
        if not had_one or code in STOPS_THE_PASS:
            raise failure from e
    except Exception as e:  # noqa: BLE001 - a network failure, say
        if not had_one:
            raise
        failure = SlackPublishError(f"The summary couldn't be updated: {e}")
    logger.warning("stock run %s: summary update failed, cards go on: %s", run["_id"], failure)
    return failure


async def publish_run(db, run: dict, agent: dict, cards: list[dict]) -> None:
    """Post the summary and one reply per card, or a single line on an all-clean alert day. `cards` are all the
    run's visible cards: a card is new when it has no `slack.ts`. On a repeat the summary is updated, never posted
    again. Raises SlackPublishError with what's still missing, which `retry_unposted_runs` tries later."""
    if run.get("kind") == PREVIEW:
        return
    deadline = time.monotonic() + PUBLISH_MAX_S
    if run.get("tick_deadline") is not None:
        deadline = min(deadline, run["tick_deadline"])
    client, channel = await _connection(db, run, agent)
    failure = await _summary_step(db, run, agent, client, channel, cards)

    posted = 0
    for card in cards:
        if not _waiting_to_post(card):
            continue
        if time.monotonic() >= deadline:
            raise SlackPublishError(OUT_OF_TIME, posted=posted)
        try:
            await _post_card(db, run, agent, client, channel, card)
            posted += 1
        except SlackApiError as e:
            code = await _note_api_error(db, run["user_id"], e)
            logger.warning("stock run %s: card %s didn't post: %s", run["_id"], card["_id"], code or e)
            failure = _failure(e, code)
            if code in STOPS_THE_PASS:
                break
        except Exception as e:  # noqa: BLE001 - one card must not cost the others theirs
            logger.error("stock run %s: card %s didn't post: %s", run["_id"], card["_id"], e, exc_info=True)
            failure = SlackPublishError(f"A card couldn't be posted: {e}")
    if failure:
        failure.posted = posted
        raise failure


# ---------------------------------------------------------------------------
# Posting late
# ---------------------------------------------------------------------------

SLACK_POSTED = {"publish_error": None, "slack_pending": False, "slack_retry_at": None, "slack_retry_until": None,
                "slack_retry_n": 0, "slack_wait_reconnect": False}


def _next_try(n: int, now: datetime, until: datetime) -> datetime:
    return min(now + timedelta(minutes=RETRY_MINUTES[min(n, len(RETRY_MINUTES) - 1)]), until)


def _waiting_look(now: datetime) -> datetime:
    """A run waiting for a reconnect is looked at every sweep. This puts it behind what's due, so waiting can't fill a batch."""
    return now + timedelta(minutes=RETRY_MINUTES[0])


def slack_attempt_fields(run: dict, now: datetime) -> dict:
    """Saved before an attempt: should it be killed, it's due again at the next step."""
    until, n = run.get("slack_retry_until") or now + RETRY_FOR, run.get("slack_retry_n") or 0
    return {"slack_pending": True, "slack_retry_until": until, "slack_retry_n": n, "slack_retry_at": _next_try(n, now, until),
            "slack_late": bool(run.get("slack_late") or run.get("slack_pending"))}


def slack_failed_fields(run: dict, error: Exception, now: datetime) -> dict:
    """After a failed attempt. A dead token waits for the reconnect. An error a person has to fix isn't retried, and
    neither is anything once the 24 hours are up. Anything else backs off, and getting further doesn't cost a step."""
    until, n = run.get("slack_retry_until") or now + RETRY_FOR, run.get("slack_retry_n") or 0
    if getattr(error, "reconnect", False):
        return {"slack_pending": True, "slack_wait_reconnect": True, "slack_retry_until": until, "slack_retry_at": _waiting_look(now)}
    if not getattr(error, "retry", True) or now >= until:
        return {"slack_pending": False, "slack_wait_reconnect": False, "slack_retry_at": None}
    return {"slack_pending": True, "slack_wait_reconnect": False, "slack_retry_until": until, "slack_retry_at": _next_try(n, now, until),
            "slack_retry_n": n if getattr(error, "posted", 0) else n + 1}


async def _newer_report(db, run: dict) -> bool:
    return await db[RUNS].find_one({"agent_id": run.get("agent_id"), "kind": {"$ne": PREVIEW}, "_id": {"$ne": run["_id"]},
                                    "as_of": {"$gt": run.get("as_of")}}, {"_id": 1}) is not None


async def _retry_run(db, run_id, now: datetime, deadline: float) -> bool:
    """One run's unposted messages, under the run's claim, so never alongside a tick working on it. True if it's all
    out now. A kill here leaves a claim of this kind, which the runner doesn't count against the run. A done run isn't
    resumed by anyone, so one left claimed by a kill during its last publish is taken here too."""
    stale = _utcnow() - STUCK_AFTER
    run = await db[RUNS].find_one_and_update(
        {"_id": run_id, "slack_pending": True, "kind": {"$ne": PREVIEW},
         "$or": [{"claimed_at": None}, {"claimed_at": {"$lte": stale}, "$or": [{"claim_kind": SLACK_CLAIM}, {"phase": "done"}]}]},
        {"$set": {"claimed_at": _utcnow(), "claim_kind": SLACK_CLAIM}}, return_document=ReturnDocument.AFTER)
    if run is None:
        return False

    async def release(**fields) -> None:
        await db[RUNS].update_one({"_id": run_id}, {"$set": {"claimed_at": None, "claim_kind": None, **fields}})

    agent = await db[AGENTS].find_one({"_id": run.get("agent_id"), "user_id": run.get("user_id")})
    if agent is None:
        await release(slack_pending=False, slack_retry_at=None, slack_wait_reconnect=False)
        return False
    if run.get("slack_wait_reconnect"):
        if await _client_for(db, run["user_id"]) is None:
            # No call to Slack. Waiting for the reconnect only ends when a newer report would make this one old news.
            over = now >= (run.get("slack_retry_until") or now) and await _newer_report(db, run)
            await release(**({"slack_pending": False, "slack_wait_reconnect": False, "slack_retry_at": None} if over
                             else {"slack_retry_at": _waiting_look(now)}))
            return False
        # Reconnected, which is what it was waiting for: a passing error from here gets the usual slow retries.
        run = {**run, "slack_retry_until": None, "slack_retry_n": 0, "slack_wait_reconnect": False}

    fields = {**slack_attempt_fields(run, now), "slack_late": True, "slack_wait_reconnect": False}
    await db[RUNS].update_one({"_id": run_id}, {"$set": fields})
    run = {**run, **fields, "tick_deadline": deadline}
    cards = await db[CARDS].find({"run_id": run_id, "status": {"$ne": STATUS_CLOSING}}).to_list(length=None)
    try:
        await publish_run(db, run, agent, cards)
    except Exception as e:  # noqa: BLE001 - the error is what the report shows
        logger.warning("stock run %s: late Slack post failed: %s", run_id, e)
        after = slack_failed_fields(run, e, now)
        if not after["slack_pending"]:
            logger.error("stock run %s: giving up on posting it to Slack: %s", run_id, e)
        await release(publish_error=str(e)[:300], **after)
        return False
    await release(**SLACK_POSTED)
    return True


async def retry_unposted_runs(db, now_utc: datetime, deadline: float) -> int:
    """Every agents tick: runs that still owe Slack messages, whatever phase they're in and whether or not the agent is
    on, since the report is already finished. Returns how many got everything out. Never a preview."""
    now = now_utc if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    query = {"slack_pending": True, "kind": {"$ne": PREVIEW}, "$or": [{"slack_retry_at": {"$lte": now}}, {"slack_wait_reconnect": True}]}
    rows = await db[RUNS].find(query, {"_id": 1}).sort("slack_retry_at", 1).limit(RETRY_BATCH).to_list(length=RETRY_BATCH)
    finished = 0
    for row in rows:
        if time.monotonic() >= deadline:
            break
        try:
            finished += await _retry_run(db, row["_id"], now, deadline)
        except Exception as e:  # noqa: BLE001 - one run must not cost the others theirs
            logger.error("stock run %s: late Slack post raised: %s", row["_id"], e, exc_info=True)
    return finished


# ---------------------------------------------------------------------------
# Urgent alerts
# ---------------------------------------------------------------------------

URGENT_CODES = ("ad_rejected",)   # the warnings that can't wait; every critical alert can't either


def _is_urgent(alert: dict) -> bool:
    return alert.get("severity") == "critical" or alert.get("code") in URGENT_CODES


def _key_text(alert: dict) -> str:
    return json.dumps(list(_alert_key(alert)))


def _urgent_timeout(run: dict) -> float:
    deadline = run.get("tick_deadline")
    if deadline is None:
        return URGENT_TIMEOUT_S
    return max(1.0, min(URGENT_TIMEOUT_S, deadline - time.monotonic() + DEADLINE_MARGIN_S))


def _urgent_text(run: dict, alert: dict, names: dict) -> str:
    name = names.get(alert.get("client_group_id"))
    head = f"🔴 *{_esc(name)}: {_esc(alert.get('title'))}*" if name else f"🔴 *{_esc(alert.get('title'))}*"
    detail = _esc(alert.get("detail") or "")
    link = f"<{report_url(FRONTEND_URL, run['_id'])}|Open report>"
    return rb._cut(f"{head}\n{detail}", MAX_TEXT - len(link) - 1).rstrip() + f"\n{link}"


async def _post_urgent(db, run: dict, agent: dict, alerts: list[dict]) -> None:
    client, channel = await _connection(db, {**run, "slack": {}}, agent)
    names = await _client_names(db, run)
    for alert in alerts:
        try:
            await _call(client.chat_postMessage, channel=channel, text=_urgent_text(run, alert, names))
        except SlackApiError as e:
            raise _failure(e, await _note_api_error(db, run["user_id"], e)) from e
        key = _key_text(alert)   # saved straight after the post: a kill here repeats this one alert at most
        run["urgent_posted_keys"] = [*(run.get("urgent_posted_keys") or []), key]
        await db[RUNS].update_one({"_id": run["_id"]}, {"$addToSet": {"urgent_posted_keys": key}})


async def post_urgent_alerts(db, run: dict, agent: dict) -> None:
    """What can't wait for the report, called with what has been read so far, however many times. Each alert goes
    out once, and the summary lists it as well. Never for a preview. It has its own timeout: a kill here counts
    against the run. Raises SlackPublishError; the runner records it, and the next pass tries what's left."""
    if run.get("kind") == PREVIEW:
        return
    posted = set(run.get("urgent_posted_keys") or [])
    alerts = [a for a in run.get("alerts") or [] if isinstance(a, dict) and _is_urgent(a) and _key_text(a) not in posted]
    if not alerts:
        return
    try:
        await asyncio.wait_for(_post_urgent(db, run, agent, alerts), timeout=_urgent_timeout(run))
    except asyncio.TimeoutError as e:
        raise SlackPublishError("Slack took too long to take the urgent alerts. Birdy tries again on its next pass.") from e


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

def is_agent_action(action_id: str | None) -> bool:
    """The buttons this module answers. A template can't use the agent_ ids (templates.RESERVED_PREFIX), and
    lesson_undo only reaches a lesson of the team that pressed it."""
    return bool(action_id) and (action_id in _ACTION_IDS or action_id.startswith(CARD_UNDO_PREFIX))


def _presser(payload: dict) -> str:
    """The name Slack sent with the press, else a mention (no users.info: it would need a new scope)."""
    user = payload.get("user") or {}
    return user.get("name") or f"<@{user.get('id')}>"


def _from_outside(payload: dict) -> bool:
    """A Slack Connect guest, whose team isn't the workspace the press came through. Slack sends no flag for this:
    see task-15-report.md for what its docs do and don't say."""
    theirs, ours = (payload.get("user") or {}).get("team_id"), (payload.get("team") or {}).get("id")
    return bool(theirs) and theirs != ours


async def _owner(db, payload: dict) -> str | None:
    team_id = (payload.get("team") or {}).get("id")
    return await get_user_id_by_team_id(db, team_id) if team_id else None


def _answer_text(res: dict) -> str:
    return res.get("reason") or REFUSED_ANSWERS.get(res.get("outcome")) or "Birdy couldn't do that."


def _where(payload: dict) -> tuple[str | None, str | None, str | None]:
    message = payload.get("message") or {}
    return ((payload.get("channel") or {}).get("id"), (payload.get("user") or {}).get("id"),
            message.get("thread_ts") or message.get("ts"))


def _presser_id(payload: dict) -> str | None:
    return (payload.get("user") or {}).get("id")


async def _store_press(db, payload: dict, kind: str, card: dict, line_id: str | None, by: str) -> str:
    """The press, stored before Slack gets its answer. A second press finds the same row."""
    channel, slack_user, thread_ts = _where(payload)
    row_id = f"{kind}:{card['_id']}:{line_id or ''}"
    try:
        await db[SLACK_ACTIONS].update_one({"_id": row_id}, {"$setOnInsert": {
            "kind": kind, "user_id": card["user_id"], "agent_id": card.get("agent_id"), "card_id": card["_id"],
            "line_id": line_id, "by": by, "by_id": slack_user, "channel": channel, "slack_user": slack_user, "thread_ts": thread_ts,
            "claimed_at": None, "tries": 0, "created_at": _utcnow()}}, upsert=True)
    except DuplicateKeyError:
        pass  # two presses at once
    return row_id


def _actions_due(now: datetime) -> dict:
    return {"$or": [{"claimed_at": None}, {"claimed_at": {"$lte": now - ACTION_RECLAIM}}]}


async def _claim_action(db, row_id: str) -> dict | None:
    now = _utcnow()
    return await db[SLACK_ACTIONS].find_one_and_update(
        {"_id": row_id, **_actions_due(now)},
        {"$set": {"claimed_at": now, "last_try_at": now, "claim": str(uuid.uuid4())}, "$inc": {"tries": 1}},
        return_document=ReturnDocument.AFTER)


async def _give_up_on_press(db, row: dict, *, settling: bool = False) -> None:
    """The press is over. They're told, even if the note can't reach them. `settling`: an undo already under way, which
    recovery finishes, and the card says how that went."""
    logger.error("stock slack: gave up on %s after %s tries", row["_id"], row.get("tries"))
    if settling:
        await _bounded(_tell(db, row["user_id"], row.get("channel"), row.get("slack_user"), STILL_SETTLING, row.get("thread_ts")),
                       ACTION_TELL_S)
        return
    await _bounded(_tell(db, row["user_id"], row.get("channel"), row.get("slack_user"), DIDNT_HAPPEN, row.get("thread_ts")),
                   ACTION_TELL_S)
    await _note_on_card(db, row["card_id"], DIDNT_HAPPEN)
    await _bounded(update_card_message(db, {"_id": row["card_id"]}), ACTION_UPDATE_S)


async def _line_state(db, card_id: str, line_id: str | None) -> str | None:
    card = await db[CARDS].find_one({"_id": card_id}, {"lines": 1})
    return next((ln.get("state") for ln in (card or {}).get("lines") or [] if ln.get("id") == line_id), None)


async def finish_action(db, mongo_client, row_id: str) -> bool:
    """Do a stored Undo or Dismiss and update its message. Runs right after Slack's answer and again from the cron, and
    only one of them gets the row. False when another has it. A frozen run leaves its row claimed, and it's taken over
    after ACTION_RECLAIM: undo_line and dismiss_card are safe to repeat, and an undo frozen mid-line is settled first
    (apply.recover_stale_undo). Thawing later can't send: the row is checked last thing before the line is claimed, and
    the line's token and send window catch the rest. `busy` keeps the row for another try. Being cut off by a caller's
    time limit costs no try. The person is told before the row goes, so a kill can repeat a note but not lose it."""
    row = await _claim_action(db, row_id)
    if row is None:
        return False
    actions, token = db[SLACK_ACTIONS], row["claim"]
    mine = {"_id": row_id, "claim": token}
    user_id, card_id, line_id = row["user_id"], row["card_id"], row.get("line_id")

    async def still_mine() -> datetime | None:
        now = _utcnow()
        return now if await actions.find_one_and_update(mine, {"$set": {"claimed_at": now}}) else None

    async def give_up() -> None:
        settling = row["kind"] == "undo" and await _line_state(db, card_id, line_id) == STATE_UNDOING
        if await actions.find_one(mine, {"_id": 1}):
            await _give_up_on_press(db, row, settling=settling)
            await actions.delete_one(mine)

    who = {"by": row["by"], "by_id": row.get("by_id"), "source": "slack"}
    try:
        if row["tries"] > ACTION_TRIES or _utcnow() - row["created_at"] > ACTION_MAX_AGE:
            await give_up()
            return True
        if row["kind"] == "undo":
            await recover_stale_undo(db, mongo_client, card_id, line_id, older_than=ACTION_RECLAIM)
            res = await undo_line(db, mongo_client, user_id, card_id, line_id, press_gate=still_mine, **who)
        else:
            res = await dismiss_card(db, user_id, card_id, **who)
        if res.get("outcome") == "taken_over":
            return True
        if res.get("outcome") == "busy":
            if row["tries"] < ACTION_TRIES:
                await actions.update_one(mine, {"$set": {"claimed_at": None}})
            else:
                await give_up()
            return True
        if not await actions.find_one(mine, {"_id": 1}):
            return True  # a takeover has it, and answers for it
        if res.get("ok"):
            await db[CARDS].update_one({"_id": card_id, "press_note": {"$nin": [None]}}, {"$set": {"press_note": None}})
        else:
            where = (row.get("channel"), row.get("slack_user"), row.get("thread_ts"))
            await _tell_or_note(db, card_id, user_id, where, _answer_text(res), ACTION_TELL_S)
        await _bounded(update_card_message(db, {"_id": card_id}), ACTION_UPDATE_S)
    except asyncio.CancelledError:
        await actions.update_one(mine, {"$set": {"claimed_at": None}, "$inc": {"tries": -1}})
        raise
    except Exception as e:  # noqa: BLE001 - the next minute's cron tries again
        logger.error("stock slack: %s failed: %s", row_id, e, exc_info=True)
        await actions.update_one(mine, {"$set": {"claimed_at": None}})
        return True
    await actions.delete_one(mine)
    return True


async def finish_pending_actions(db, mongo_client, deadline: float) -> int:
    """Presses whose work never ran after the response, or whose function died. Held to PRESS_CAP_S of the cron's tick:
    what it cuts off keeps its row and its try, and the oldest try goes first next time, so one stuck press can't starve
    the rest."""
    now, done = _utcnow(), 0
    rows = await db[SLACK_ACTIONS].find(_actions_due(now)).sort("last_try_at", 1).limit(ACTION_BATCH).to_list(length=ACTION_BATCH)

    async def work():
        nonlocal done
        for row in rows:
            if time.monotonic() >= deadline:
                return
            done += await finish_action(db, mongo_client, row["_id"])

    try:
        await asyncio.wait_for(work(), min(PRESS_CAP_S, max(_left(deadline), 0.01)))
    except asyncio.TimeoutError:
        logger.info("stock slack: stored presses used their share of the tick")
    return done


async def _ack_then_finish(db, payload: dict, kind: str, card: dict, line_id: str | None, by: str, note: str, defer) -> dict:
    row_id = await _store_press(db, payload, kind, card, line_id, by)
    channel, slack_user, thread_ts = _where(payload)
    await _bounded(_tell(db, card["user_id"], channel, slack_user, note, thread_ts), NOTE_BUDGET_S)
    if defer is not None:
        defer(row_id)
    return {}


async def _press_lesson_undo(db, payload: dict, action: dict, user_id: str) -> dict:
    memory_id = action.get("value")
    lesson = await db[MEMORY].find_one({"_id": memory_id, "source": "slack_mention"})
    agent = lesson and await db[AGENTS].find_one({"_id": lesson.get("agent_id"), "user_id": user_id})
    if not agent:
        logger.warning("stock slack: lesson undo for %s from another team, ignored", memory_id)
        return {}
    await lessons.overrule_lesson(db, agent["_id"], memory_id)
    channel, ts = (payload.get("channel") or {}).get("id"), (payload.get("message") or {}).get("ts")
    if channel and ts:
        text = f"Removed. Birdy won't use this: {_esc(lesson.get('text'))}"
        async def update():
            client = await _client_for(db, user_id)
            if client:
                await client.chat_update(channel=channel, ts=ts, text=rb._cut(text, MAX_TEXT), blocks=[])
        await _bounded(update(), REFRESH_BUDGET_S)
    return {}


def _left_out_text(n: int) -> str:
    if not n:
        return ""
    cards = "1 card isn't" if n == 1 else f"{n} cards aren't"
    return f" {cards} in Slack, so Birdy left {'it' if n == 1 else 'them'} alone. See the report."


async def _press_apply_all(db, payload: dict, mongo_client, run_id, by: str, user_id: str, deadline: float) -> dict:
    if await db[RUNS].find_one({"_id": run_id, "user_id": user_id}, {"_id": 1}) is None:
        logger.warning("stock slack: Apply all for run %s from another team, ignored", run_id)
        return {}
    # Not cut from outside: apply_run stores the press before it looks at the budget, and what's over it goes to the cron.
    res = await apply_run(db, mongo_client, user_id, run_id, by=by, by_id=_presser_id(payload), source="slack",
                          budget_s=max(0.0, min(APPLY_ALL_BUDGET_S, _left(deadline) - ANSWER_RESERVE_S)))
    channel, slack_user, thread_ts = _where(payload)
    if not res.get("ok"):
        text = _answer_text(res)
    elif res.get("outcome") == "noop":
        text = "Nothing in Slack is waiting to apply." + _left_out_text(res.get("not_in_slack"))
    else:
        text = "Applying. Each card updates as its changes go through." + _left_out_text(res.get("not_in_slack"))
    await _bounded(refresh_summary(db, run_id), min(REFRESH_BUDGET_S, _left(deadline) - NOTE_BUDGET_S))
    await _bounded(_tell(db, user_id, channel, slack_user, text, thread_ts), _left(deadline))
    return {}


async def _press_edit(db, payload: dict, card: dict, deadline: float) -> dict:
    channel, slack_user, thread_ts = _where(payload)
    busy = any(ln.get("state") in BUSY_STATES for ln in card.get("lines") or [])
    if card.get("status") not in (STATUS_OPEN, STATUS_FLAGGED) or busy:
        await _bounded(_tell(db, card["user_id"], channel, slack_user, "This card can't be edited now.", thread_ts), NOTE_BUDGET_S)
        return {}
    client = await _client_for(db, card["user_id"])
    if not (client and payload.get("trigger_id")):
        return {}
    try:  # the trigger only lasts 3 s, so this can't wait for later
        await asyncio.wait_for(client.views_open(trigger_id=payload["trigger_id"], view=edit_view(card)),
                               min(EDIT_OPEN_BUDGET_S, _left(deadline) - NOTE_BUDGET_S))
        return {}
    except SlackApiError as e:
        logger.warning("stock card %s: edit form didn't open: %s", card["_id"], await _note_api_error(db, card["user_id"], e))
    except asyncio.TimeoutError:
        logger.warning("stock card %s: edit form didn't open in time", card["_id"])
    except Exception as e:  # noqa: BLE001 - a network failure, say: they've pressed a button and need an answer
        logger.warning("stock card %s: edit form didn't open: %s", card["_id"], e)
    await _bounded(_tell(db, card["user_id"], channel, slack_user, FORM_DIDNT_OPEN, thread_ts), _left(deadline))
    return {}


async def _press_apply(db, payload: dict, mongo_client, card: dict, by: str, deadline: float) -> dict:
    """The second of two presses gets what the first one did: apply_card's claim is atomic. The claim is never cut, or
    the person would be told about work nobody has. Only the drain is held to `deadline`, and what it leaves stays queued
    for the cron. A database slow enough to pass Slack's 3 s costs the answer, not the press."""
    res = await apply_card(db, mongo_client, card["user_id"], card["_id"], by=by, by_id=_presser_id(payload), source="slack",
                           budget_s=APPLY_BUDGET_S, drain_until=deadline - ANSWER_RESERVE_S)
    text = None if res.get("ok") else _answer_text(res)
    if res.get("ok") and card.get("press_note"):
        await db[CARDS].update_one({"_id": card["_id"]}, {"$set": {"press_note": None}})
    await _bounded(update_card_message(db, card), min(REFRESH_BUDGET_S, _left(deadline) - NOTE_BUDGET_S))
    if text:
        await _tell_or_note(db, card["_id"], card["user_id"], _where(payload), text, _left(deadline))
    return {}


async def handle_card_action(payload: dict, action: dict, db, mongo_client, *, defer=None) -> dict:
    """The stock agent's buttons. Slack is waiting, so nothing here takes long: see the module docstring. `defer(row_id)`
    asks for a stored press to be worked on after the answer, and without it the cron does that. Anything that doesn't
    belong to the Slack team that sent it is ignored, and someone from another organisation is told no."""
    deadline = time.monotonic() + PRESS_DEADLINE_S
    action_id = action.get("action_id") or ""
    if action_id == OPEN_REPORT:  # a link: Slack only wants the answer
        return {}
    user_id = await _owner(db, payload)
    if not user_id:
        logger.warning("stock slack: %s from an unknown team, ignored", action_id)
        return {}
    if _from_outside(payload):
        logger.warning("stock slack: %s from outside the workspace, refused", action_id)
        channel, slack_user, thread_ts = _where(payload)
        await _bounded(_tell(db, user_id, channel, slack_user, OUTSIDER, thread_ts), NOTE_BUDGET_S)
        return {}
    if action_id == LESSON_UNDO:
        return await _press_lesson_undo(db, payload, action, user_id)
    by, value = _presser(payload), action.get("value") or ""
    if action_id == APPLY_ALL:
        return await _press_apply_all(db, payload, mongo_client, value, by, user_id, deadline)

    line_id = None
    if action_id.startswith(CARD_UNDO_PREFIX):
        value, _, line_id = value.rpartition(":")
        if not value or line_id != action_id[len(CARD_UNDO_PREFIX):]:
            logger.warning("stock slack: undo button %s with value for another line, ignored", action_id)
            return {}
    card = await db[CARDS].find_one({"_id": value, "user_id": user_id})
    if card is None:
        logger.warning("stock slack: %s on card %s from another team, ignored", action_id, value)
        return {}

    if action_id == CARD_APPLY:
        return await _press_apply(db, payload, mongo_client, card, by, deadline)
    if action_id == CARD_EDIT:
        return await _press_edit(db, payload, card, deadline)
    if action_id == CARD_DISMISS:
        return await _ack_then_finish(db, payload, "dismiss", card, None, by, "Dismissing this card.", defer)
    return await _ack_then_finish(db, payload, "undo", card, line_id, by, "Undoing. The card updates when it's done.", defer)


# ---------------------------------------------------------------------------
# The edit form
# ---------------------------------------------------------------------------

_TEXT_FIELDS = (("additional_changes", "Additional changes"), ("thought_process", "Thought process"), ("next_steps", "Next steps"))


def _text_input(block_id: str, label: str, value) -> dict:
    element = {"type": "plain_text_input", "action_id": "value", "multiline": True, "max_length": MAX_TEXT}
    if value:
        element["initial_value"] = str(value)[:MAX_TEXT]
    return {"type": "input", "block_id": block_id, "optional": True, "element": element,
            "label": {"type": "plain_text", "text": label}}


def _line_option(line: dict) -> dict:
    text = f"{_VERBS.get(line.get('kind'), 'Change')} {line.get('ad_name') or 'ad'}"
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": str(line.get("id"))}


def edit_view(card: dict) -> dict:
    """The form behind Edit: which lines stay, and the words. A line can only be dropped: what it does comes from
    the stored line. Plain text inputs show text as written, so nothing here needs escaping."""
    blocks = []
    lines = card.get("lines") or []
    if lines:
        options = [_line_option(ln) for ln in lines]
        blocks.append({"type": "input", "block_id": "lines", "optional": True,
                       "label": {"type": "plain_text", "text": "Changes to keep"},
                       "element": {"type": "multi_static_select", "action_id": "value", "options": options[:100],
                                   "initial_options": options[:100]}})
    blocks += [_text_input(key, label, card.get(key)) for key, label in _TEXT_FIELDS]
    return {"type": "modal", "callback_id": EDIT_CALLBACK, "title": {"type": "plain_text", "text": "Edit card"},
            "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({"card_id": card["_id"], "version": card.get("version") or 0}),
            "blocks": blocks}


def _form_error(message: str, field: str = "thought_process") -> dict:
    return {"response_action": "errors", "errors": {field: message}}


def _edit_patch(card: dict, values: dict) -> tuple[dict, str | None]:
    """(what the form changes, or an error). A field left alone isn't written, so a text longer than the form can
    show isn't cut by a save."""
    patch = {}
    for key, _ in _TEXT_FIELDS:
        field = (values.get(key) or {}).get("value")
        if field is None:
            continue
        typed = field.get("value") or ""
        before = str(card.get(key) or "")
        if typed != before[:MAX_TEXT]:
            patch[key] = typed
    field = (values.get("lines") or {}).get("value")
    if field is not None:
        keep = {o.get("value") for o in field.get("selected_options") or []}
        lines = card.get("lines") or []
        if any(ln.get("state") not in DROPPABLE_STATES for ln in lines if ln.get("id") not in keep):
            return {}, "A change that is being or has been applied can't be removed."
        kept = [ln for ln in lines if ln.get("id") in keep]
        if len(kept) != len(lines):
            patch["lines"] = kept
    return patch, None


async def handle_edit_submission(payload: dict, db) -> dict:
    """Save the edit form, as the report's edit does, and record what it teaches. `original` stays as Birdy wrote it."""
    gone = "This card can't be edited any more."
    if _from_outside(payload):
        logger.warning("stock slack: an edit from outside the workspace, refused")
        return _form_error(OUTSIDER)
    user_id = await _owner(db, payload)
    view = payload.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except ValueError:
        meta = {}
    card = await db[CARDS].find_one({"_id": meta.get("card_id"), "user_id": user_id}) if user_id else None
    if card is None:
        logger.warning("stock slack: edit of card %s from another team, ignored", meta.get("card_id"))
        return _form_error(gone)
    if card.get("preview") or card.get("status") not in (STATUS_OPEN, STATUS_FLAGGED):
        return _form_error(gone)
    if (card.get("version") or 0) != meta.get("version"):
        return _form_error("This card changed while you were editing it. Close this and press Edit again.")
    patch, error = _edit_patch(card, (view.get("state") or {}).get("values") or {})
    if error:
        return _form_error(error)
    if not patch:
        return {}
    if not await guarded_update(db, card, patch):
        return _form_error("This card changed while you were editing it. Close this and press Edit again.")
    by = _presser(payload)
    try:
        agent = await db[AGENTS].find_one({"_id": card.get("agent_id"), "user_id": user_id})
        if agent:
            await lessons.lesson_from_edit(db, agent, await db[CARDS].find_one({"_id": card["_id"]}), by)
    except Exception as e:  # noqa: BLE001 - a lesson is never worth failing a saved edit
        logger.warning("stock card %s: could not record a lesson from the edit: %s", card["_id"], e)
    await _bounded(update_card_message(db, card), REFRESH_BUDGET_S)
    return {}


# ---------------------------------------------------------------------------
# @Birdy
# ---------------------------------------------------------------------------

def _clean_mention(text: str) -> str:
    """The person's words: no @mentions, and Slack's escapes undone. Escaped again wherever they're shown."""
    words = _MENTION_TOKEN.sub(" ", text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return " ".join(words.split())


def _name_in_text(name: str) -> re.Pattern:
    """Whole words. A name of 3 characters or fewer has to match its case too, or "go ahead" is the client called Go."""
    pattern = _name_pattern(name)
    return re.compile(pattern.pattern) if len(name) <= SHORT_NAME else pattern


def _named_clients(text: str, names: dict) -> set:
    """Which of these clients the words name. Longest first, and what matched is used up, so "Acme London" isn't Acme too."""
    found = set()
    for gid, name in sorted(names.items(), key=lambda item: -len(item[1])):
        pattern = _name_in_text(name)
        if pattern.search(text):
            found.add(gid)
            text = pattern.sub(" ", text)
    return found


async def _names_of(db, user_id: str, gids: set | None, cards: list[dict]) -> dict:
    """{client: name} of these clients, or with None, all the agency's."""
    query = {"user_id": user_id, **({"id": {"$in": list(gids)}} if gids is not None else {})}
    names = {g["id"]: g.get("name") async for g in db["client_groups"].find(query, {"id": 1, "name": 1})}
    for card in cards:
        names[card.get("client_group_id")] = names.get(card.get("client_group_id")) or card.get("client_name")
    return {gid: " ".join(str(name).split()) for gid, name in names.items() if gid and name and str(name).strip()}


async def _thread_client(db, user_id: str, cards: list[dict], words: str) -> tuple[str | None, dict]:
    """(the client these words are about, or None to ask, and the clients to ask about or name). All of a run's cards
    share one thread, as Slack has no threads inside threads. A thread about one client is about that client, unless the
    words name another of the agency's. With several, the words have to name exactly one of the run's."""
    in_thread = {c.get("client_group_id") for c in cards} - {None}
    if len(in_thread) == 1:
        names = await _names_of(db, user_id, None, cards)
        found = _named_clients(words, names)
        if len(found) > 1:
            return None, {g: names[g] for g in found}
        return (next(iter(found)) if found else next(iter(in_thread))), names
    run = await db[RUNS].find_one({"_id": cards[0].get("run_id"), "user_id": user_id}, {"clients": 1})
    names = await _names_of(db, user_id, in_thread | set((run or {}).get("clients") or {}), cards)
    found = _named_clients(words, names)
    return (next(iter(found)) if len(found) == 1 else None), names


def _ask_which_client(names: dict) -> str:
    shown = ", ".join(_esc(n) for n in sorted(names.values())[:ASK_NAMES_MAX])
    more = " and more" if len(names) > ASK_NAMES_MAX else ""
    return rb._cut(f"Which client is this about? Birdy didn't record it. Mention @Birdy again and name one: {shown}{more}.", MAX_TEXT)


def _lesson_reply(lesson: dict | None, client_name: str | None) -> tuple[str, list | None]:
    if not (lesson and lesson.get("_id")):
        return "Birdy couldn't record that, so it won't use it. Try again in a moment.", None
    scope = _esc(client_name or "this client")
    extra = (" It stays a note, so it won't stop any change: from Slack, Birdy only makes rules about ads with a certain name "
             "in this client. Set anything wider in Birdy." if lesson.get("filter_dropped") else "")
    text = rb._cut(f"Noted for {scope}: {_esc(lesson.get('text'))}{extra}", MAX_TEXT)
    return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                  {"type": "actions", "elements": [{"type": "button", "action_id": LESSON_UNDO, "value": lesson["_id"],
                                                    "text": {"type": "plain_text", "text": "Undo"}}]}]


async def _say_in_thread(db, user_id: str, channel: str, thread_ts: str, reply: str, blocks: list | None) -> None:
    try:
        client = await _client_for(db, user_id)
        if client:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply, **({"blocks": blocks} if blocks else {}))
    except Exception as e:  # noqa: BLE001 - it's recorded either way, and the report lists it
        logger.warning("stock slack: lesson reply failed: %s", e)


def mention_from_outside(team_id: str | None, event: dict, envelope: dict) -> bool:
    """Whether an @Birdy comes from another organisation. The author's team where Slack says so, and with no word on the
    author, any externally shared channel counts as outside. Whether a Slack Connect mention carries these is undocumented."""
    author_team = event.get("user_team") or event.get("team")
    if author_team:
        return author_team != team_id
    return bool(envelope.get("is_ext_shared_channel"))


async def handle_thread_mention(db, team_id: str | None, channel: str, thread_ts: str | None, text: str, author: str,
                                *, from_outside: bool = False) -> bool:
    """@Birdy in the thread of a run's cards becomes a lesson, not a chat turn, and Birdy says what it recorded with
    an Undo, or asks which client when it can't tell, recording nothing. Someone from another organisation is told no,
    with nothing recorded. True when this was such a thread. The AI call that shapes the lesson is fine here: the events route
    already answers late and skips Slack's retries."""
    if not team_id or not thread_ts:
        return False
    user_id = await get_user_id_by_team_id(db, team_id)
    if not user_id:
        return False
    cards = await db[CARDS].find({"user_id": user_id, "slack.channel_id": channel, "slack.thread_ts": thread_ts}).to_list(length=None)
    if cards and from_outside:
        logger.warning("stock slack: a mention from outside the workspace in a card thread, refused")
        await _say_in_thread(db, user_id, channel, thread_ts, OUTSIDER_MENTION, None)
        return True
    words = _clean_mention(text)
    if not cards or not words:
        return False
    agent = await db[AGENTS].find_one({"_id": cards[0].get("agent_id"), "user_id": user_id})
    if agent is None:
        return False
    gid, names = await _thread_client(db, user_id, cards, words)
    if gid is None:
        await _say_in_thread(db, user_id, channel, thread_ts, _ask_which_client(names), None)
        return True
    card = next((c for c in cards if c.get("client_group_id") == gid), None)
    try:
        lesson = await lessons.lesson_from_text(db, agent, words, source=lessons.SLACK_MENTION, author=author, client_group_id=gid,
                                                source_ref=(card or {}).get("_id") or cards[0].get("run_id"))
    except Exception as e:  # noqa: BLE001 - it was meant for the lesson log, so it doesn't turn into a chat turn
        logger.error("stock slack: lesson from a mention failed: %s", e, exc_info=True)
        lesson = None
    reply, blocks = _lesson_reply(lesson, names.get(gid))
    await _say_in_thread(db, user_id, channel, thread_ts, reply, blocks)
    return True
