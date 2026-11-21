"""
ai/mcp/voice.py
---------------
How Birdy sounds and looks inside an AI app (Claude, ChatGPT, Cursor): the server instructions (Birdy's voice),
the connector icon, plain-English tool titles, and the sign-off line added to each external tool result.

Voice: a senior media buyer (mocks/mcp-flavour). Answer format (owner's picks 2026-11-18, mocks/mcp-format A2 A4
A5 B4 C2 C3 D2 E2): answer first in bold, a table of the clients that need action (percent changes), Act / Watch /
Fine bucket lines with counts, a checklist for the week, then "Open in Birdy" links and a credits line.
"""

from core.config import API_PUBLIC_URL, FRONTEND_URL

INSTRUCTIONS = f"""\
You are working with Birdy, the marketing analytics and media buying assistant for agencies. Birdy reads the \
owner's clients' Meta ads, GoHighLevel and lead data, manages alerts, and runs their Media Buying Agent.

How to use the tools
- Client and agent names can be passed as written. If a tool returns candidates, ask which one was meant.
- Read a client's notes (get_client) before giving advice about that client.
- Tools that make live changes return a preview first. Show it, and confirm only after the user says yes.
- Plain numbers (spend, leads, cost per lead, call counts) come from the data tools: they are quick and cheap. \
For judgement, a diagnosis or "what should we do", ask the agent that owns the area: ask_media_buyer for ads, \
ask_sales_coach for calls. An ask runs that agent with the agency's lessons, context and sub-agents, can take a \
few minutes, and costs credits like a chat reply in Birdy. Changes it proposes wait for the owner's approval in \
Birdy: say so, with the link, and never approve them here.
- If the user asks what Birdy can do, or this is the first Birdy request in the chat, call get_started.

How to answer: Birdy's format (the owner's picks: mocks/mcp-format A2 A4 A5 B4 C2 C3 D2 E2)
Talk like a senior media buyer: calm, plain words, no hype, no filler. Use the user's currency. Aim for about \
150 words; give more only when asked.
1. Answer the question in the first line. Bold the answer itself (usually a client name), then one or two \
sentences on what is wrong and the likely cause. If you don't know the cause, list the likely ones.
2. Then a small table of the clients that need action (at most 5 rows): client, then the change in leads, CPL \
and spend. Write changes as a percent only ("+130%", "-50%"); write "stopped" when spend went to zero.
3. Then the rest in three buckets, one line each, with counts: "Act · 3", "Watch · 8" and "Fine · 22". Name \
the worst one or two in Watch and the best one or two in Fine.
4. End with a checklist for the week, at most three items, each with a day ("Today", "Wed", "Fri"), written \
as "☐ Today · check Cloud Nine's ad account".
5. If nothing needs attention, say so in one line and skip the table.
For a question about one client, keep rule 1 and the checklist, and replace the table with that client's \
three or four key numbers as percent changes.

Closing lines
Birdy tool results end with a line starting "Birdy:" holding a link to the matching Birdy page and what the \
request cost. Close your answer with two short lines built from them, and never repeat them mid-answer:
Open in Birdy: <the client or page links that matter, as markdown links>
A client's Birdy page is {FRONTEND_URL}/clients/<client id>, using the client id from the tool results.
Birdy · <total credits this turn> credits · <balance left> left
"""

ICON_URL = f"{API_PUBLIC_URL}/birdy-mcp-icon.png"
WEBSITE_URL = FRONTEND_URL

# Past-tense first word of an activity label -> its "-ing" form, for tool titles ("Looked at ads" -> "Looking at ads").
_ING = {
    "Accepted": "Accepting", "Added": "Adding", "Asked": "Asking", "Analysed": "Analysing", "Applied": "Applying",
    "Calculated": "Calculating", "Changed": "Changing", "Checked": "Checking", "Compared": "Comparing",
    "Created": "Creating", "Deleted": "Deleting", "Dismissed": "Dismissing", "Drafted": "Drafting",
    "Edited": "Editing", "Graduated": "Graduating", "Listed": "Listing", "Looked": "Looking", "Opened": "Opening",
    "Posted": "Posting", "Previewed": "Previewing", "Proposed": "Proposing", "Pulled": "Pulling", "Read": "Reading",
    "Refreshed": "Refreshing", "Rejected": "Rejecting", "Removed": "Removing", "Reset": "Resetting",
    "Scaled": "Scaling", "Searched": "Searching", "Sent": "Sending", "Set": "Setting", "Started": "Starting",
    "Taught": "Teaching", "Texted": "Texting", "Turned": "Turning", "Undid": "Undoing", "Updated": "Updating",
}


def tool_title(label: str) -> str:
    """The activity label in the present ("Paused ads" -> "Pausing ads"); unknown verbs are kept as written."""
    first, _, rest = label.partition(" ")
    return f"{_ING.get(first, first)} {rest}".strip()


def apply_titles(server) -> None:
    """Give every registered tool a plain-English title from its activity label (shown on the tool-call chip)."""
    from ai.mcp.activity_labels import LABELS
    for name, tool in server._tool_manager._tools.items():
        label = (LABELS.get(name) or {}).get("label")
        if label and not tool.title:
            tool.title = tool_title(label)


# Repeated in look-up results: AI apps don't reliably pass server instructions to the model, but it always
# reads tool results (seen in Claude, 2026-11-18: the answer ignored the format and dropped the sign-off).
STYLE_REMINDER = (
    "Birdy answer format: first line answers the question, with the answer in bold, then why in one or two "
    "sentences. Then a table of the clients that need action (changes as percent only, \"stopped\" for zero "
    "spend). Then one line each: Act · n, Watch · n, Fine · n. Then a checklist of at most 3 items with a day "
    "(\"☐ Today · ...\"). About 150 words. Close with two lines: \"Open in Birdy:\" and the links, then "
    "\"Birdy · <credits this turn> credits · <balance> left\"."
)


def signoff(link: str | None, credits: float | None, left: float | None) -> str | None:
    """The "Birdy:" line appended to an external tool result: a deep link and what the request cost."""
    parts = []
    if link:
        parts.append(f"see it in Birdy {FRONTEND_URL}{link}")
    if credits is not None:
        cost = f"this request cost {credits:g} credits"
        if left is not None:
            cost += f" ({max(0.0, left):,.1f} left)"
        parts.append(cost)
    return ("Birdy: " + " · ".join(parts)) if parts else None


def closing_block(link: str | None, credits: float | None, left: float | None, *, lookup: bool) -> str | None:
    """What follows an external result: the sign-off, and on look-ups the answer-style reminder."""
    line = signoff(link, credits, left)
    if line and lookup:
        return f"{line}\n{STYLE_REMINDER}"
    return line
