"""Agent core module."""

from opencomposer.agent.context import ContextBuilder
from opencomposer.agent.hook import AgentHook, AgentHookContext, CompositeHook
from opencomposer.agent.loop import AgentLoop
from opencomposer.agent.memory import Consolidator, Dream, MemoryStore
from opencomposer.agent.skills import SkillsLoader
from opencomposer.agent.subagent import SubagentManager

__all__ = [
    "AgentHook",
    "AgentHookContext",
    "AgentLoop",
    "CompositeHook",
    "ContextBuilder",
    "Dream",
    "MemoryStore",
    "SkillsLoader",
    "SubagentManager",
]
