"""
ai/team/route.py
----------------
One reply's route: which agents took part, in what order, what each was asked,
which tools it used, what it answered, and what it cost. This is the `route`
field of the chat response (CHAT ROUTE CONTRACT v1) and the payload of every
`event: route` on /api/chat/stream.

A `Turn` lives for one user reply. The agent the user addressed gets the first
step; every agent asked during the reply gets the next one, in the order it was
first involved. A reply an agent wrote alone has a one-step route.
"""

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ai.team import definitions as defs

# ── Limits (design section 4) ─────────────────────────────────────────────────
MAX_ASKS_PER_REPLY = 3
# Longest chain: main agent -> Birdy -> other main agent -> one sub-agent.
# Depth counts hops from the agent the user addressed (depth 0).
MAX_DEPTH = 3

# ── Time (Vercel kills the request at 300 s; the MCP token minted at the start
# of the top-level run lives 280 s, ai/mcp_client.py) ────────────────────────
TURN_BUDGET_SECONDS = 250        # the whole reply, including the parent's last answer
PARENT_RESERVE_SECONDS = 40      # kept back for the parent to answer after its last ask
ASK_TIMEOUT_SECONDS = 100        # one ask, at most
MIN_ASK_SECONDS = 15             # less time left than this: don't start an ask
FINAL_ANSWER_SECONDS = 30        # less time left than this: no more tools, the agent answers now
NESTED_MAX_TOOL_ITERATIONS = 6   # a delegated run's tool loop (the top level keeps 12)

LIMIT_ASKS, LIMIT_DEPTH, LIMIT_REPEAT = "asks", "depth", "repeat"

SUMMARY_MAX = 60

# Plain labels for the route's tool pills. Anything missing falls back to the name in words.
TOOL_LABELS = {
    "get_client_groups": "Client list",
    "get_account_summary": "Account summary",
    "compare_periods": "Period comparison",
    "get_alerts": "Alerts",
    "get_campaign_insights": "Campaign figures",
    "get_adset_insights": "Ad set figures",
    "get_ad_insights": "Ad figures",
    "get_meta_insights_live": "Live Meta figures",
    "get_meta_insights_monthly": "Monthly Meta figures",
    "get_facebook_leads": "Meta leads",
    "get_meta_leads_live": "Live Meta leads",
    "get_metrics_by_day_windows": "Figures by window",
    "get_daily_metrics": "Daily figures",
    "get_closes_by_ad": "Closes by ad",
    "get_tag_rollup_by_campaign": "Tags by campaign",
    "get_unified_lead_stats": "Lead totals",
    "get_ghl_opportunity_stats": "Pipeline totals",
    "get_ghl_opp_stats_windowed": "Pipeline for a period",
    "get_call_center_stats": "Call stats",
    "get_call_trend": "Calls against the period before",
    "recall_conversations": "Earlier chats",
    "propose_sub_agent": "A new sub-agent",
    "get_call_recordings_summary": "Call recordings",
    "analyze_call_recordings": "Call analysis",
    "find_ads": "Ad search",
    "list_ad_accounts": "Ad accounts",
    "list_adsets": "Ad sets",
    "list_creative_tests": "Creative tests",
    "get_creative_test": "Creative test",
    "list_lessons": "Lessons",
    "save_lesson": "Saved a lesson",
    "edit_lesson": "Edited a lesson",
    "remove_lesson": "Removed a lesson",
    "list_agent_lessons": "Agent's lessons",
    "list_sub_agent_lessons": "Sub-agents' lessons",
    "render_ui": "Chart",
    "propose_action": "Proposed a change",
}

_DETAIL_KEYS = ("date_preset", "preset", "period", "window", "days", "level", "since", "until",
                "start_date", "end_date", "year", "query", "code")


def tool_label(name: str) -> str:
    if name in TOOL_LABELS:
        return TOOL_LABELS[name]
    if name.startswith("ask_"):
        return "Asked " + name[4:].replace("_", " ").title()
    return name.replace("_", " ").capitalize()


def tool_detail(args: dict) -> Optional[str]:
    """A few words on what the call covered (its period, search, level), or None."""
    if not isinstance(args, dict):
        return None
    bits = []
    for k in _DETAIL_KEYS:
        v = args.get(k)
        if v in (None, "", [], {}):
            continue
        bits.append(f"{k.replace('_', ' ')} {v}" if k in ("days", "year", "level") else str(v))
    if args.get("start") and args.get("end"):
        bits.append(f"{args['start']} to {args['end']}")
    text = ", ".join(bits)
    return clip(text, SUMMARY_MAX) or None


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_MD = re.compile(r"[*_`#>\[\]]|\(https?://[^)]*\)|:::ui[\s\S]*?:::|\[\[viz:\d+\]\]")
_SUMMARY_LINE = re.compile(r"^\s*SUMMARY:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def split_summary(answer: str) -> tuple[str, Optional[str]]:
    """(answer without its SUMMARY line, the summary) — the delegated prompt asks for one."""
    m = None
    for m in _SUMMARY_LINE.finditer(answer or ""):
        pass
    if not m:
        return (answer or "").strip(), None
    cleaned = (answer[: m.start()] + answer[m.end():]).strip()
    return cleaned, clip(m.group(1).strip(" .`"), SUMMARY_MAX)


def summarize(text: str) -> str:
    """First sentence of an answer in plain words, at most 60 characters."""
    plain = _MD.sub("", text or "")
    plain = " ".join(plain.split())
    first = re.split(r"(?<=[.!?])\s", plain, maxsplit=1)[0] if plain else ""
    return clip(first, SUMMARY_MAX)


# ── Usage ─────────────────────────────────────────────────────────────────────

@dataclass
class Usage:
    """Token usage of one agent's run, updated after every model call, so a
    run cancelled by its timeout still counts what it spent."""
    agent: str
    depth: int
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0

    def add(self, response) -> None:
        self.model_calls += 1
        u = getattr(response, "usage", None)
        self.prompt_tokens += getattr(u, "input_tokens", 0) or 0
        self.completion_tokens += getattr(u, "output_tokens", 0) or 0


# ── Turn ──────────────────────────────────────────────────────────────────────

@dataclass
class Turn:
    """Everything shared by the runs of one user reply."""
    root_agent: str
    on_change: Optional[Callable[[dict], None]] = None
    t0: float = field(default_factory=lambda: time.monotonic())
    steps: list = field(default_factory=list)
    usages: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    asks: int = 0
    limit_hit: Optional[str] = None
    involved: bool = False      # a second agent took part (or was refused): the route is shown
    status: str = "working"
    credits_total: Optional[float] = None   # the charge actually debited, once known
    _rates: Optional[dict] = None           # credit settings, loaded once per reply
    budget: Optional[float] = None          # seconds for the whole reply; None = TURN_BUDGET_SECONDS

    # time
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def budget_seconds(self) -> float:
        return TURN_BUDGET_SECONDS if self.budget is None else self.budget

    def time_left(self) -> float:
        return self.budget_seconds() - self.elapsed()

    def must_answer(self) -> bool:
        """Too little time left for another tool round: every agent in the reply answers now."""
        return self.time_left() <= FINAL_ANSWER_SECONDS

    def ask_timeout(self) -> float:
        """Seconds the next ask may run; <= 0 when no ask may start."""
        left = self.budget_seconds() - PARENT_RESERVE_SECONDS - self.elapsed()
        t = min(ASK_TIMEOUT_SECONDS, left)
        return t if t >= MIN_ASK_SECONDS else 0

    # limits
    def hit(self, limit: str) -> None:
        if self.limit_hit is None:
            self.limit_hit = limit

    # steps
    def add_step(self, agent: str, name: str, *, question: Optional[str], asked_by: Optional[str],
                 status: str = "working") -> dict:
        step = {
            "id": "s_" + uuid.uuid4().hex[:10],
            "agent": agent,
            "name": name,
            "kind": defs.kind_of(agent),
            "status": status,
            "summary": "",
            "question": question,
            "asked_by": asked_by,
            "tools": [],
            "doing": None,      # the tool in hand right now, in plain words; null between tools
            "answer": None,
            "credits": 0.0,
            "seconds": 0.0,
        }
        self.steps.append(step)
        return step

    def step_for(self, agent: str) -> Optional[dict]:
        return next((s for s in self.steps if s["agent"] == agent), None)

    # credits
    def credits_for(self, usage: Usage) -> float:
        if not self._rates:
            return 0.0
        from credits import credits_for_usage
        return round(credits_for_usage(self._rates["rate_mode"], usage.model, usage.prompt_tokens,
                                       usage.completion_tokens, markup=self._rates["markup"]), 4)

    async def load_rates(self, db) -> None:
        if self._rates is not None:
            return
        try:
            from credits import get_credits_settings
            s = await get_credits_settings(db)
            self._rates = {"rate_mode": s["rate_mode"], "markup": s["markup"]}
        except Exception:
            self._rates = {}

    def totals(self) -> dict:
        return {
            "prompt_tokens": sum(u.prompt_tokens for u in self.usages),
            "completion_tokens": sum(u.completion_tokens for u in self.usages),
            "model_calls": sum(u.model_calls for u in self.usages),
        }

    def breakdown(self) -> list[dict]:
        """Per-agent usage for the ledger row: who spent what in this reply."""
        return [{"agent": u.agent, "depth": u.depth, "prompt_tokens": u.prompt_tokens,
                 "completion_tokens": u.completion_tokens, "model_calls": u.model_calls,
                 "credits": self.credits_for(u)} for u in self.usages]

    # output
    def to_route(self) -> Optional[dict]:
        # Always given for an agent's reply, even when it worked alone: the owner sees what the
        # agent is doing (its step, the tool in hand, the tools used) from the first moment.
        step_credits = [s["credits"] for s in self.steps]
        total = self.credits_total if self.credits_total is not None else round(sum(step_credits), 4)
        return {
            "status": self.status,
            "seconds": round(self.elapsed(), 1),
            "credits": total,
            "limit_hit": self.limit_hit,
            "steps": [dict(s, tools=[dict(t) for t in s["tools"]]) for s in self.steps],
        }

    def changed(self) -> None:
        """Tell the stream the route changed (agent started, tool finished, agent answered)."""
        if self.on_change is None:
            return
        try:
            self.on_change(self.to_route())
        except Exception:
            pass


@dataclass
class Frame:
    """One agent's run inside a reply."""
    turn: Turn
    agent: str                  # "birdy" | "media_buyer" | "sales_coach" | "sub:<id>"
    depth: int
    chain: tuple                # agent keys from the addressed agent down to this one
    step: dict
    usage: Usage
    client_group_id: Optional[str] = None
    client_name: Optional[str] = None
    sub: object = None          # the SubAgent record, for "sub:<id>" frames
    parent_main: Optional[str] = None   # for a sub-agent: its main agent's key

    def parent_is_birdy(self) -> bool:
        """Asked directly by Birdy (the chain ends ..., birdy, this agent)."""
        return len(self.chain) >= 2 and self.chain[-2] == defs.BIRDY

    @property
    def nested(self) -> bool:
        return self.depth > 0


def new_turn(agent: str, *, client_group_id=None, client_name=None, on_change=None, model=None) -> Frame:
    """The root frame of a reply addressed to `agent`."""
    turn = Turn(root_agent=agent, on_change=on_change)
    step = turn.add_step(agent, defs.NAMES[agent], question=None, asked_by=None)
    usage = Usage(agent=agent, depth=0, model=model)
    turn.usages.append(usage)
    return Frame(turn=turn, agent=agent, depth=0, chain=(agent,), step=step, usage=usage,
                 client_group_id=client_group_id, client_name=client_name)
