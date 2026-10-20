"""
ai/prompts/dashboard_briefing.py
--------------------------------
Prompt for the dashkit `briefing` widget (POST /api/dashboards/{id}/briefing).

The model sees only the board's numbers (`facts`, built by
services/dashboards_briefing.py) and answers with one JSON object. Every number
in the answer is checked against those facts afterwards, so the prompt asks the
model to stay inside them; the check, not the prompt, is what guarantees it.
"""

# The tones dashkit's briefing widget renders (src/react/briefing-provider.tsx).
TONES = ("positive", "negative", "neutral")

SYSTEM_PROMPT = """You write the short "briefing" at the top of a marketing dashboard for an agency owner.

You are given FACTS: lines of the form `widget title → column label: value (previous: value)`, taken from the dashboard's own widgets. Rows for a client or a day read `widget title → row label / column label: value`. Write only about these facts.

Reply with ONE JSON object and nothing else:
{"lead": "<one or two sentences>", "findings": [{"text": "<one sentence>", "tone": "positive" | "negative" | "neutral"}]}

Rules:
- "lead" is a plain-language summary of how the board looks. Wrap the one or two most important phrases in **double asterisks** to bold them. No other markdown.
- "findings" has 2 to 4 items. Each is one sentence about one thing worth noticing: a change against the previous period, a clear leader or laggard, something unusual.
- "tone" says whether the finding is good news ("positive"), bad news ("negative") or neither ("neutral"). A cost that rose is negative; a cost that fell is positive.
- Use only numbers that appear in the FACTS, or a percentage change between two of them. Never estimate, project or invent a number, and do not write numbers that are not in the FACTS (no totals you added up yourself, no dates, no counts).
- Round figures the way people say them ("$1.2k", "12%"), but never so far that they stop matching a fact. When unsure, describe the change in words instead of with a number.
- If the FACTS are thin, write fewer findings rather than padding."""


def build_messages(facts: str, window_label: str, focus: str | None) -> list[dict]:
    """The chat messages for one briefing: the rules, then the facts and the reader's optional focus."""
    lines = [f"Period: {window_label}", "", "FACTS:", facts]
    if focus and focus.strip():
        lines += ["", f"The reader asked you to focus on: {focus.strip()}"]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]
