"""
ai/agents/sub_agents
--------------------
Sub-agents under a main agent (Media Buyer, Sales Coach). See core.py for the model and lifecycle,
tools.py for the tool-list merge point, schedule.py for scheduled runs, propose_tool.py for the
main agents' proposal tool. Every function takes `db` first and the owner's `user_id` second.
"""

from ai.agents.sub_agents.core import (  # noqa: F401
    ACTIVE, ALL_CLIENTS, KIND_CUSTOM, KIND_SPECIALIST, PAUSED, PROPOSED, SUB_AGENTS, SubAgentError,
    add_sub_agent_lesson, approve_sub_agent, delete_sub_agent, delete_sub_agent_lesson, get_sub_agent,
    list_parent_lessons, list_sub_agent_lessons, list_sub_agents, pause_sub_agent, propose_sub_agent,
    reject_sub_agent, resume_sub_agent, to_api, to_api_many, update_sub_agent,
)
from ai.agents.sub_agents.tools import MEDIA_BUYER, PARENTS, SALES_COACH, parent_tool_names  # noqa: F401
