"""
ai/suggestions
==============
Birdy's proactive suggestion engine.

An *orchestrator* runs a set of pluggable *subagents* (analyzers) over a client's
performance data for a given time window (weekly / monthly), each emitting typed
``Finding`` objects. Findings carry pre-computed, deterministic evidence (the real
numbers) plus an optional machine-executable ``Action`` (e.g. pause these ads). A
grounded LLM *composer* turns each finding into human-facing copy without ever
being given the ability to invent numbers, and the orchestrator persists the
result as a suggestion and writes to the activity feed.

The framework (contracts, store, orchestrator, activity log, apply pipeline) is
fixed; *how* any individual subagent decides is an implementation detail behind
the ``Subagent`` protocol — so new analyzers (creative fatigue, budget pacing,
scaling opportunities, …) plug in without touching the plumbing.
"""
