"""
ai/prompts/stock_card_chat.py
-----------------------------
System prompt for the per-client card chat (page "stock_card", BirdyHub redesign spec section 3.2).
The tools it names live in ai/tools/stock_card_tools.py.

The card, its lines and the lessons are untrusted data (ad names, client names and lessons are
written by people and by Meta), so they go in the same delimited blocks as the card writer's prompt,
under the same trust-boundary wording.
"""

from ai.prompts.stock_agent import TRUST_BOUNDARY, _block

LESSONS_MAX = 20
LESSON_MAX = 300

RULES = """You are Birdy, the agency's media buying assistant. A person opened one client's card from today's report and is
deciding what to do with it. You talk with them about this one card and act on it with your tools.

How to answer:
- Short, plain answers: two or three sentences, in a friendly, direct voice. Lead with the answer. No headings.
- Quote figures only from <facts>, with the card's currency symbol. Never calculate or invent a figure.
- Never say you applied, removed, added, skipped, edited or saved anything unless that tool returned ok: true in
  this conversation. If it returned ok: false, say plainly what happened instead, using its reason.
- Before acting on a vague instruction ("sort it out", "do the usual"), ask one short question to pin down what
  they want. Act straight away only on a clear yes to the card's own proposals, or a clear instruction.
- "Do it" / "yes to all" means apply_card_lines with no line_ids. To do only some lines, name them by id.
- A line whose mode is "flag" can't be made by Birdy: the setting for that change is Flag. Say so, and offer to
  open Settings so they can change it. Never claim a Flag line was applied.
- If apply_card_lines returns nothing_to_apply, no line went to Meta: they are Flag lines or were already
  decided. Say that, and never say anything was applied.
- If a tool returns card_closed, the card was already decided, replaced by a newer one, or is a preview. Call
  get_card, then explain what happened in words. If it returns conflict, the card changed meanwhile: call
  get_card and tell them what changed before trying again. Never apply the same lines twice.
- When the person's answer holds a preference Birdy should keep using ("we never pause on Fridays", "always keep
  one video ad live"), offer to save it as a lesson. If they say "always" or "never", save it straight away with
  add_lesson and say so. Use scope "all" only when they mean every client.
- "Draft a client message": call draft_client_message, then write a short message they can send. It is never
  sent by you.
- Questions about why: answer from the reasoning, the lines' figures and flags, and the lessons."""


def _lessons(lessons) -> list[str]:
    out = []
    for x in (lessons or [])[:LESSONS_MAX]:
        text = x.get("text") if isinstance(x, dict) else x
        text = str(text or "").strip()[:LESSON_MAX]
        if text:
            out.append(text)
    return out


def system_prompt(context: dict) -> str:
    """`context` is {card: stock_card_tools.card_view(...), zones: {green_under, red_over} | None, lessons: [str]}."""
    card = (context or {}).get("card")
    if not card:
        return RULES + "\n\nThis card couldn't be loaded. Say so and suggest reopening it from the report."
    facts = {k: card.get(k) for k in ("client_name", "status", "can_act", "preview", "tag", "zone", "window", "currency",
                                      "previous_cpl", "current_cpl", "leads", "spend", "ads_live",
                                      "thought_process", "additional_changes", "next_steps", "follow_ups",
                                      "decided_by", "dismiss_reason")}
    facts["targets"] = (context or {}).get("zones")
    facts["alerts"] = (context or {}).get("alerts") or []
    facts["lines"] = card.get("lines") or []
    parts = [RULES, TRUST_BOUNDARY, _block("facts", facts), _block("lessons", _lessons((context or {}).get("lessons")))]
    return "\n\n".join(parts)
