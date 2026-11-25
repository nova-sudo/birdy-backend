"""
ai/team
-------
Birdy's agent team (design: briefs/2026-11-20-agent-hierarchy-design.md).

Three levels: Birdy (orchestrator), main agents (Marketing Guru, Sales Coach) and
sub-agents under a main agent. A chat turn addressed to one of them runs
`ai.orchestrator.run_chat(agent=...)`; an agent asks another through an "ask"
tool, which runs a nested, bounded `run_chat` for the child (ai/team/delegation.py).

- definitions.py  who each agent is: name, instructions, tools, memory
- route.py        the per-reply route (steps, limits, credits) the chat returns
- delegation.py   the ask / propose tools, limits, nested runs
- sub_agents.py   the one import sub-agent records come through
"""
