"""Agent core module."""

from opencomposor.agent.context import ContextBuilder
from opencomposor.agent.hook import AgentHook, AgentHookContext, CompositeHook
from opencomposor.agent.loop import AgentLoop
from opencomposor.agent.memory import Consolidator, Dream, MemoryStore
from opencomposor.agent.skills import SkillsLoader
from opencomposor.agent.subagent import SubagentManager

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
