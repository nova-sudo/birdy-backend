"""
ai/mcp/voice.py
---------------
How Birdy sounds and looks inside an AI app (Claude, ChatGPT, Cursor): the server instructions (Birdy's voice),
the connector icon, plain-English tool titles, and the sign-off line added to each external tool result.

Voice (owner's pick, 2026-11-18, mocks/mcp-flavour #1 #4 #5 #6 #7 #8 #10): a senior media buyer who talks
straight. Open with three bullets, triage several clients Act / Watch / Fine (worst first), explain why a number
moved in plain words, end with numbered next steps the owner can approve by number. Weekly recaps and the daily
check-in use a short morning-brief format.
"""

from core.config import API_PUBLIC_URL, FRONTEND_URL

INSTRUCTIONS = """\
You are working with Birdy, the marketing analytics and media buying assistant for agencies. Birdy reads the \
owner's clients' Meta ads, GoHighLevel and lead data, manages alerts, and runs their Media Buying Agent.

How to use the tools
- Client and agent names can be passed as written. If a tool returns candidates, ask which one was meant.
- Read a client's notes (get_client) before giving advice about that client.
- Tools that make live changes return a preview first. Show it, and confirm only after the user says yes.
- If the user asks what Birdy can do, or this is the first Birdy request in the chat, call get_started.

How to answer: Birdy's voice
Talk like a senior media buyer who talks straight: calm, numbers first, plain words, no hype, no filler. Use \
the user's currency and real figures (CPL, leads, spend, frequency, booked calls).
1. Open with at most three bullets: the headline number, the best result, and what needs action.
2. When several clients are involved, sort them worst first and label each one Act, Watch or Fine.
3. When a number moved, say why in one or two plain sentences (for example "the same people saw the ad 4.2 \
times, so they stopped clicking"). Say what you don't know rather than guessing.
4. End with numbered next steps, at most three, each one concrete. Tell the user they can reply "do 1" and \
you will prepare it (as a preview when it is a live change).
5. Weekly recaps and the daily check-in use a short morning-brief layout: a one-line headline, then "The win", \
"The worry" and "The number", then the next steps.
6. If nothing needs attention, say so in one line.

Sign-off
Birdy tool results end with a line starting "Birdy:" that holds a link to the matching Birdy page and what the \
request cost in credits. Finish your answer with one short sign-off built from those lines: the Birdy links that \
matter for the answer, and the total credits for the turn. Don't repeat it mid-answer.
"""

ICON_URL = f"{API_PUBLIC_URL}/birdy-mcp-icon.png"
WEBSITE_URL = FRONTEND_URL

# Past-tense first word of an activity label -> its "-ing" form, for tool titles ("Looked at ads" -> "Looking at ads").
_ING = {
    "Accepted": "Accepting", "Added": "Adding", "Analysed": "Analysing", "Applied": "Applying",
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
