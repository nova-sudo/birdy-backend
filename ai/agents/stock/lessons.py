"""
ai/agents/stock/lessons.py
--------------------------
The stock agent's learning loop (spec section 9). Lessons reuse `agent_memory`: they come from card
edits, dismiss reasons and Slack mentions, start empty for every agency and are never mixed between
agents. The scoreboard is scored here too, and patterns in it become "Make it a rule?" suggestions.

  meaningful_edit     does an edit change the lines or the meaning, not just the wording
  lesson_from_edit    a code-written lesson from a meaningful card edit (no AI call)
  lesson_from_text    text a person wrote (dismiss reason, Slack mention) -> lesson, via memory_from_note
  lessons_for_prompt  the active lessons the writer sees: this client's and the agency's, newest 50
  keep / overrule     a person's verdict on a lesson; an overruled one no longer reaches the AI
  score_result        won / lost / no_clear_change / never_went_live from fixed numbers
  score_due           scores the scoreboard rows whose scoring window has passed
  scan_patterns       a pattern reaching `scoring.rule_after` opens one `promote` proposal
  accept_promotion    Yes on a suggestion stores it as a `promoted` lesson

Lessons are user text. They only ever reach the AI as delimited data (prompts/stock_agent.py), and
can't widen the allowed list.
"""

import difflib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from ai.agents.guardrails import agent_tz, local_today
from ai.agents.learning import (MEMORY_TEXT_MAX, NOTE_MAX, ProposalClosed, ProposalNotFound, memory_from_note)
from ai.agents.snapshot import load_ad_days
from ai.agents.stock.scoreboard import (LOST, NEVER_WENT_LIVE, NO_CLEAR_CHANGE, OUTCOMES, UNSCORED, WON)
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings, resolve_lost_spend, resolve_zones
from ai.agents.store import MEMORY, PROPOSALS, add_memory

logger = logging.getLogger(__name__)

ACTIVE, OVERRULED = "active", "overruled"
PROMOTE = "promote"
LESSONS_FOR_PROMPT = 50
FEATURE = "stock_lesson"           # the metering label for the AI call that turns text into a lesson
MAX_SCORE_ROWS = 200               # rows scored in one catch-up; the rest wait for the next batch day
UNSCORED_AFTER_DAYS = 14           # a row that still can't be judged this long after its window ends `unscored`
DATA_GRACE_DAYS = 3                # past a window's end, score even if the client's ad data never arrived
TYPO_WORD_SIMILARITY = 0.8
_MEANING_FIELDS = (("additional_changes", "the additional changes"), ("thought_process", "the reasoning"),
                   ("next_steps", "the next steps"))
_KIND_LABEL = {"add": "Add", "remove": "Remove", "revive": "Bring back"}


def _naive(dt: datetime) -> datetime:
    return dt if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------

def _words(text) -> list[str]:
    return re.sub(r"[^\w\s]", " ", str(text or "").lower()).split()


def _same_meaning(a, b) -> bool:
    """Equal once case and punctuation are ignored, or the same words with only small spelling slips.
    A number or a word that was swapped, added or dropped is a different meaning."""
    wa, wb = _words(a), _words(b)
    if wa == wb:
        return True
    if len(wa) != len(wb):
        return False
    for x, y in zip(wa, wb):
        if x == y:
            continue
        if any(c.isdigit() for c in x + y):
            return False
        if difflib.SequenceMatcher(None, x, y).ratio() < TYPO_WORD_SIMILARITY:
            return False
    return True


def _line_ids(side) -> set:
    return {ln.get("id") or (ln.get("kind"), ln.get("ad_name")) for ln in (side or {}).get("lines") or []
            if isinstance(ln, dict)}


def _changed_fields(before: dict, after: dict) -> list[tuple[str, str]]:
    return [(k, label) for k, label in _MEANING_FIELDS if not _same_meaning((before or {}).get(k), (after or {}).get(k))]


def meaningful_edit(before: dict, after: dict) -> bool:
    """True when the edit changed which lines the card has, or what its text means. Fixing a typo,
    punctuation or case, and reordering lines, are not lessons."""
    if not before:
        return False   # nothing to compare with
    after = after or {}
    return _line_ids(before) != _line_ids(after) or bool(_changed_fields(before, after))


def _describe_edit(card: dict, before: dict, after: dict) -> str:
    parts = []
    gone = [ln for ln in before.get("lines") or [] if isinstance(ln, dict) and (ln.get("id") or (ln.get("kind"), ln.get("ad_name"))) not in _line_ids(after)]
    if gone:
        parts.append("dropped " + ", ".join(f"{_KIND_LABEL.get(ln.get('kind'), 'a change')} {ln.get('ad_name') or ''}".strip()
                                            for ln in gone))
    for key, label in _changed_fields(before, after):
        parts.append(f"changed {label} to \"{' '.join(str(after.get(key) or '').split())[:60]}\"")
    name = card.get("client_name") or "a client"
    return f"On {name}'s card a person " + " and ".join(parts or ["edited the card"]) + "."


async def lesson_from_edit(db, agent: dict, card: dict, by: str | None) -> dict | None:
    """A lesson from a meaningful edit, or None. Compared with `original`, Birdy's own draft, so the
    wording a person settles on is what counts. A card has one edit lesson, updated by later edits,
    and one a person overruled stays overruled. The words are written by code: no AI call."""
    before, after = card.get("original") or {}, card
    if not meaningful_edit(before, after):
        return None
    text = _describe_edit(card, before, after)[:MEMORY_TEXT_MAX]
    existing = await db[MEMORY].find_one({"agent_id": agent["_id"], "source": "edit", "source_ref": card["_id"]})
    if existing is not None:
        if existing.get("status") == OVERRULED:
            return existing
        return await db[MEMORY].find_one_and_update(
            {"_id": existing["_id"]}, {"$set": {"text": text, "author": by}}, return_document=ReturnDocument.AFTER)
    return await add_memory(db, agent["_id"], text=text, kind="fact", source="edit", source_ref=card["_id"],
                            author=by, client_group_id=card.get("client_group_id"), status=ACTIVE)


# ---------------------------------------------------------------------------
# Text lessons
# ---------------------------------------------------------------------------

async def lesson_from_text(db, agent: dict, text: str, *, source: str, author: str | None,
                           client_group_id: str | None = None, source_ref: str | None = None) -> dict | None:
    """A lesson from words a person wrote: a dismiss reason or an @Birdy mention. One AI call shapes it
    (metered); with no credits, or if the call fails, the words themselves are kept. Never raises
    for AI or billing reasons."""
    text = (text or "").strip()[:NOTE_MAX]
    if not text:
        return None
    blocked = False
    try:
        import credits
        blocked = await credits.is_blocked(db, agent.get("user_id"))
    except Exception as e:  # noqa: BLE001 - a billing hiccup must not lose the lesson
        logger.warning("stock lessons: credits check failed for %s: %s", agent.get("user_id"), e)
    if blocked:
        return await add_memory(db, agent["_id"], text=text[:MEMORY_TEXT_MAX], kind="fact", source=source,
                                source_ref=source_ref, author=author, client_group_id=client_group_id, status=ACTIVE)
    return await memory_from_note(db, agent, text, {"_id": source_ref, "client_group_id": client_group_id},
                                  source=source, author=author, client_group_id=client_group_id,
                                  feature=FEATURE, status=ACTIVE)


async def lessons_for_prompt(db, agent_id: str, gid: str) -> list[str]:
    """What the writer is told: active lessons for this client or the whole agency, newest first, 50 at most.
    Overruled and removed lessons are left out, and so is every other agent's."""
    rows = await (db[MEMORY].find({
        "agent_id": agent_id, "active": True, "status": {"$ne": OVERRULED},
        "$or": [{"client_group_id": None}, {"client_group_id": gid}]})
        .sort("created_at", -1).limit(LESSONS_FOR_PROMPT).to_list(length=LESSONS_FOR_PROMPT))
    return [m["text"] for m in rows if m.get("text")]


async def keep_lesson(db, agent_id: str, memory_id: str) -> dict | None:
    """Keep: the lesson is active (again) and stays so. None when this agent has no such lesson."""
    return await db[MEMORY].find_one_and_update(
        {"_id": memory_id, "agent_id": agent_id},
        {"$set": {"status": ACTIVE, "active": True, "kept": True}}, return_document=ReturnDocument.AFTER)


async def overrule_lesson(db, agent_id: str, memory_id: str) -> dict | None:
    """Overrule: it stays listed, but nothing reads it any more (also not the checks that read memory filters)."""
    return await db[MEMORY].find_one_and_update(
        {"_id": memory_id, "agent_id": agent_id},
        {"$set": {"status": OVERRULED, "active": False, "overruled_at": _utcnow()}},
        return_document=ReturnDocument.AFTER)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_result(ad_spend, ad_leads, baseline_cpl, lost_spend, settings) -> str | None:
    """How an Add did over its scoring window, from fixed numbers (`settings` is the scoring block, or all settings):

      never_went_live  no spend at all: an execution problem, not a verdict on the ad
      lost             spent `lost_spend` or more with at most `lose_max_leads` leads; or, with any lead at all,
                       a CPL above `lose_cpl_multiple` times the baseline (spec section 3)
      won              at least `win_min_leads` leads at or under the baseline CPL
      no_clear_change  anything else

    None when it can't be told: no usable baseline CPL, or no spend limit, for a case that needs one.
    Nothing is invented for it."""
    scoring = {**DEFAULT_SETTINGS["scoring"], **((settings or {}).get("scoring", settings) or {})}
    spend, leads = float(ad_spend or 0), int(ad_leads or 0)
    if spend <= 0:
        return NEVER_WENT_LIVE
    few = leads <= scoring["lose_max_leads"]
    if few and lost_spend is not None and spend >= lost_spend:
        return LOST
    cpl = spend / leads if leads else None
    if isinstance(baseline_cpl, (int, float)) and not isinstance(baseline_cpl, bool) and baseline_cpl > 0:
        if cpl is not None and cpl > scoring["lose_cpl_multiple"] * baseline_cpl:
            return LOST
        if cpl is not None and leads >= scoring["win_min_leads"] and cpl <= baseline_cpl:
            return WON
        return None if few and lost_spend is None else NO_CLEAR_CHANGE
    # No baseline: only the spend limit can say anything, and under it there is nothing more to judge.
    return NO_CLEAR_CHANGE if few and lost_spend is not None else None


async def score_due(db, agent: dict, now_utc: datetime, *, deadline: float | None = None) -> list[dict]:
    """Score every row whose first `scoring.min_days` days live are over, in the ad account's own timezone,
    on the client's own campaigns only. Rows already scored, undone, cleared or another agent's are never
    touched. A row waits until the client's ad history has synced past the window's last day: a day with
    no spend writes no row, so the sync marker (as in the alerts) is what says the data is in. After
    DATA_GRACE_DAYS without it, a row with no spend in the data ends `unscored`, never never_went_live or
    lost. A row that can't be judged (no baseline, no spend limit) keeps its reason and ends `unscored`
    after UNSCORED_AFTER_DAYS. At most MAX_SCORE_ROWS rows get a result per call; rows left waiting don't
    count against that. Stops at `deadline`. Returns the rows it scored (not the unscored ones)."""
    from ai.agents.stock.alerts import ACCOUNT_TZ_FIELD, _history_state
    from ai.agents.stock.checks import scoped_days

    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    min_days = int(settings["scoring"]["min_days"])
    rows = await db[OUTCOMES].find({"agent_id": agent["_id"], "result": None, "live_since": {"$ne": None}}) \
        .sort("live_since", 1).to_list(length=None)
    groups: dict[str, dict] = {}
    history: dict[tuple, str] = {}
    ready: dict[str, list] = {}
    for row in rows:
        gid = row.get("client_group_id")
        if gid not in groups:
            groups[gid] = await db["client_groups"].find_one({"id": gid, "user_id": agent.get("user_id")}) or {}
        tz = groups[gid].get(ACCOUNT_TZ_FIELD) or agent_tz(agent)
        today = local_today(now_utc, tz)
        start = local_today(_naive(row["live_since"]), tz)
        end = start + timedelta(days=min_days - 1)
        if end >= today:
            continue   # the account's last day of the window is still running, or not here yet
        if (gid, end) not in history:
            history[(gid, end)] = await _history_state(db, gid, end, tz)
        synced = history[(gid, end)] == "ready"
        if not synced and today <= end + timedelta(days=DATA_GRACE_DAYS):
            continue
        ready.setdefault(gid, []).append((row, start, end, today, synced))

    scored, finished = [], 0
    for gid, items in ready.items():
        if _expired(deadline) or finished >= MAX_SCORE_ROWS:
            break
        group = groups[gid]
        days = scoped_days(await load_ad_days(db, agent.get("user_id"), gid, min(i[1] for i in items),
                                              max(i[2] for i in items)), group)
        for row, start, end, today, synced in items:
            if _expired(deadline) or finished >= MAX_SCORE_ROWS:
                break
            mine = [d for d in days.get(row.get("ad_id"), []) if start <= d.day <= end]
            spend, leads = round(sum(d.spend for d in mine), 2), sum(d.leads for d in mine)
            if not synced and spend <= 0:
                result, reason = UNSCORED, "the ad data for the window never arrived"
            else:
                zones = resolve_zones(settings, gid, row.get("currency"))
                lost_spend = resolve_lost_spend(settings, zones) if zones else None
                result = score_result(spend, leads, row.get("baseline_cpl"), lost_spend, settings)
                reason = None
                if result is None:
                    reason = ("no baseline CPL to compare against" if not row.get("baseline_cpl")
                              else "no spend limit: the client's zones aren't set")
                    if today > end + timedelta(days=UNSCORED_AFTER_DAYS):
                        result = UNSCORED
                    else:
                        await db[OUTCOMES].update_one({"_id": row["_id"], "result": None},
                                                      {"$set": {"unscored_reason": reason}})
                        continue
            fields = {"spend": spend, "leads": leads, "cpl": round(spend / leads, 2) if leads else None,
                      "result": result, "scored_at": _naive(now_utc)}
            if reason:
                fields["unscored_reason"] = reason
            update = {"$set": fields} if reason else {"$set": fields, "$unset": {"unscored_reason": ""}}
            res = await db[OUTCOMES].update_one({"_id": row["_id"], "result": None}, update)
            if res.modified_count == 1:
                finished += 1
                if result != UNSCORED:
                    scored.append({**row, **fields})
    return scored


# ---------------------------------------------------------------------------
# Rule suggestions
# ---------------------------------------------------------------------------

def _treatment_name(settings: dict, tid: str) -> str:
    return next((t.get("name") for t in settings.get("treatments") or [] if t.get("id") == tid and t.get("name")), tid)


async def scan_patterns(db, agent: dict, now_utc: datetime | None = None, *, deadline: float | None = None) -> list[dict]:
    """A pattern that reaches `scoring.rule_after` opens one `promote` proposal ("Make it a rule?"):
    an ad that won in that many different accounts in one treatment, or one ad that lost that many
    times in one treatment. A pattern is suggested once, whatever happens to the suggestion, so four
    wins don't make a second one. Returns the proposals opened by this scan."""
    if _expired(deadline):
        return []
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    need = int(settings["scoring"]["rule_after"])
    if need < 1:
        return []
    rows = await db[OUTCOMES].find({"agent_id": agent["_id"], "result": {"$in": [WON, LOST]}}).sort("created_at", 1).to_list(length=None)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        for tid in r.get("treatment_ids") or []:
            if r.get("creative_key"):
                groups.setdefault((r["result"], tid, r["creative_key"]), []).append(r)
    opened = []
    for (result, tid, key), group in groups.items():
        count = len({g.get("client_group_id") for g in group}) if result == WON else len(group)
        if count < need:
            continue
        name = next((g.get("ad_name") for g in reversed(group) if g.get("ad_name")), key)
        tname = _treatment_name(settings, tid)
        text = (f"{name} won in {count} accounts in {tname}. Prefer it when picking ads for {tname}." if result == WON
                else f"{name} lost {count} times in {tname}. Avoid it when picking ads for {tname}.")[:MEMORY_TEXT_MAX]
        pattern = f"{'win' if result == WON else 'lose'}:{tid}:{key}"
        now = _naive(now_utc) if now_utc else _utcnow()
        doc = {"_id": uuid.uuid4().hex, "status": "open", "text": text, "created_at": now,
               "evidence": {"count": count, "creative_key": key, "treatment_id": tid, "result": result,
                            "outcome_ids": [g["_id"] for g in group]}}
        # One per pattern for good, even with two scans at once: the unique match decides.
        res = await db[PROPOSALS].update_one({"agent_id": agent["_id"], "kind": PROMOTE, "pattern_key": pattern},
                                             {"$setOnInsert": doc}, upsert=True)
        if res.upserted_id is not None:
            opened.append(await db[PROPOSALS].find_one({"_id": doc["_id"]}))
    return opened


async def accept_promotion(db, agent: dict, proposal_id: str, *, by: str | None, now: datetime | None = None):
    """Yes on "Make it a rule?": stores the suggestion as a `promoted` lesson and closes it. It guides the
    AI and changes no fixed check or rule. Returns (proposal, lesson). Raises ProposalNotFound / ProposalClosed."""
    now = now or _utcnow()
    key = {"_id": proposal_id, "agent_id": agent["_id"], "kind": PROMOTE}
    claimed = await db[PROPOSALS].find_one_and_update(
        {**key, "status": "open"}, {"$set": {"status": "accepted", "decided_at": _naive(now), "decided_by": by}},
        return_document=ReturnDocument.AFTER)
    if claimed is None:
        found = await db[PROPOSALS].find_one(key)
        if found is None:
            raise ProposalNotFound(proposal_id)
        raise ProposalClosed(found.get("status"))
    try:
        lesson = await add_memory(db, agent["_id"], text=claimed["text"], kind="fact", source="promoted",
                                  source_ref=proposal_id, author=by, status=ACTIVE)
    except BaseException:
        await db[PROPOSALS].update_one({"_id": proposal_id}, {"$set": {"status": "open"},
                                                              "$unset": {"decided_at": "", "decided_by": ""}})
        raise
    return claimed, lesson
