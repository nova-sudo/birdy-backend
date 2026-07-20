"""
ai/suggestions/agents
=====================
The subagent registry. Adding a new analyzer = write a class implementing the
``Subagent`` protocol (ai/suggestions/contracts.py) and append an instance to
``REGISTRY``. The orchestrator runs everything here; nothing else changes.
"""

from ai.suggestions.agents.useless_ad_purger import UselessAdPurger

# Order is display/priority order when findings tie on severity.
REGISTRY = [
    UselessAdPurger(),
]

__all__ = ["REGISTRY", "UselessAdPurger"]
