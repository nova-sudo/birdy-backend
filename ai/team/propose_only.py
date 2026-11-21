"""
ai/team/propose_only.py
-----------------------
Read-and-propose for a whole reply, root run included. Set only by the MCP asks (ai/mcp/team_mcp.py),
where the "user message" is written by an assistant outside Birdy: there the agent reads and proposes,
and the owner approves in Birdy. A root run under it gets what a nested run gets: its read tools, no
write or lesson-writing tool (a call to one is refused), and propose_action. The web chat and Slack
never set it, so they behave as before.

Request-scoped: a ContextVar, set by the caller around the reply's task and reset in a finally. The
reply's own tasks (asks to other agents) copy it, and they are nested runs anyway.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_propose_only: ContextVar[bool] = ContextVar("team_propose_only", default=False)

PROMPT_NOTE = """
## Asked from outside Birdy
This question comes through an assistant outside Birdy. Here you cannot make changes yourself: you only
read. For anything that should change, call `propose_action` (or `propose_sub_agent` for a new
sub-agent) and say in your answer that it is a proposal waiting for the owner's approval in Birdy.
"""


def active() -> bool:
    return _propose_only.get()


def restricted(frame) -> bool:
    """Whether this run reads and proposes only: a nested run always, any run under propose-only."""
    return bool(frame.nested or _propose_only.get())


@contextmanager
def propose_only():
    token = _propose_only.set(True)
    try:
        yield
    finally:
        _propose_only.reset(token)
