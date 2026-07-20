"""
ai/suggestions/contracts.py
---------------------------
The stable contract every subagent speaks. This is the part that is expensive to
change later, so it is deliberately small and generic:

  Stat      — one number for the suggestion card ({label, value, bad}).
  Action    — a machine-executable instruction (pause these ads), or None.
  Evidence  — the window + the numbers behind the call (ALWAYS computed in code).
  Finding   — what a subagent emits: severity, copy, evidence, optional action.
  Subagent  — the protocol a subagent implements: analyze(...) -> list[Finding].

Nothing here talks to the DB or an LLM. Findings are pure data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# Severity drives the card colour on the frontend (see SEVERITY_STYLE in page.jsx).
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_OPPORTUNITY = "OPPORTUNITY"
SEVERITIES = (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_OPPORTUNITY)

# Time windows the orchestrator runs subagents over.
WINDOW_WEEKLY = "weekly"
WINDOW_MONTHLY = "monthly"
WINDOWS = (WINDOW_WEEKLY, WINDOW_MONTHLY)

# Action types. The apply pipeline (routers/dashboard.py) knows how to execute
# each of these; add a new type here + an executor there to grow the surface.
ACTION_PAUSE_ADS = "pause_ads"
ACTION_ENABLE_ADS = "enable_ads"


@dataclass
class Stat:
    """One figure rendered on the suggestion card. `bad=True` shows it in red."""
    label: str
    value: str
    bad: bool = False

    def to_dict(self) -> dict:
        return {"label": self.label, "value": self.value, "bad": self.bad}


@dataclass
class Action:
    """
    A machine-executable instruction attached to a finding, or omitted for an
    advisory-only suggestion. `targets` is a list of Meta objects the action
    applies to: [{object_id, object_type, name}].
    """
    type: str
    targets: list[dict] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "targets": self.targets, "params": self.params}


@dataclass
class Evidence:
    """
    The grounding for a finding. `stats` are display-ready figures for the card;
    `raw` holds the underlying numbers for the audit log and debugging. Both are
    produced deterministically in Python — never by an LLM.
    """
    window: str
    stats: list[Stat] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def stats_as_dicts(self) -> list[dict]:
        return [s.to_dict() for s in self.stats]


@dataclass
class Finding:
    """
    What a subagent emits. `title`/`description`/`severity` may be refined by the
    composer, but must always be safe to show as-is (subagents provide a
    deterministic template so the pipeline works even with no LLM available).
    """
    agent: str
    client_group_id: str
    client_name: str
    severity: str
    title: str
    description: str
    evidence: Evidence
    action: Optional[Action] = None
    platform: str = "Meta Ads"
    confidence: float = 0.7
    # A lucide icon name the frontend maps to a component (severity is the
    # fallback if this is unknown). Kept as a plain string so it survives JSON.
    icon: str = "sparkles"
    # Explicit override; if empty, computed from agent + window + client + targets.
    dedup_key: str = ""

    def compute_dedup_key(self) -> str:
        """
        Stable identity for "the same suggestion". Intentionally excludes the
        copy (title/description) so that re-running a pass refreshes an existing
        suggestion in place instead of spawning a duplicate when only the wording
        changes. Includes the action targets so pausing a *different* set of ads
        is treated as a new finding.
        """
        if self.dedup_key:
            return self.dedup_key
        target_ids = sorted(
            str(t.get("object_id", "")) for t in (self.action.targets if self.action else [])
        )
        # Dedup by the actual object(s) acted on — NOT by client-group or window:
        #   * the same ad can appear under multiple client-groups that share one
        #     Meta ad account, so it must be ONE suggestion, not one per group;
        #   * the same ad flagged in the weekly AND monthly window is also one.
        # Advisory findings that carry no targets fall back to per-client dedup.
        if target_ids:
            basis = "|".join([self.agent, ",".join(target_ids)])
        else:
            basis = "|".join([self.agent, self.client_group_id])
        return "sug_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


@dataclass
class AnalyzerContext:
    """
    Runtime handles a subagent needs but that aren't part of a Finding. Passed to
    analyze() so subagents can read the DB (e.g. resolve a client's alert
    thresholds) without reaching for globals.
    """
    db: object
    user_id: str
    mongo_client: object = None
    config: dict = field(default_factory=dict)


@runtime_checkable
class Subagent(Protocol):
    """
    The interface a subagent implements. `name` is a stable identifier used in
    dedup keys and the activity feed. `analyze` is deterministic detection: it
    returns zero or more findings for one client + window. Whether it uses pure
    rules, an LLM, or both internally is entirely up to the implementation — the
    orchestrator only sees Findings.
    """
    name: str

    async def analyze(self, ctx, client_group: dict, window: str) -> list[Finding]:
        ...
