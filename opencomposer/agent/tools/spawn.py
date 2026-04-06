"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from opencomposer.agent.tools.base import Tool, tool_parameters
from opencomposer.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from opencomposer.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        label=StringSchema("Optional short label for the task (for display)"),
        task_id=StringSchema("Optional task ID to bind to this subagent run", nullable=True),
        required=["task"],
    )
)
class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"
        key = f"{self.__class__.__name__}:{id(self)}"
        self._origin_channel_ctx: ContextVar[str] = ContextVar(
            f"{key}:origin_channel",
            default=self._origin_channel,
        )
        self._origin_chat_ctx: ContextVar[str] = ContextVar(
            f"{key}:origin_chat",
            default=self._origin_chat_id,
        )
        self._session_key_ctx: ContextVar[str] = ContextVar(
            f"{key}:session_key",
            default=self._session_key,
        )

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel_ctx.set(channel)
        self._origin_chat_ctx.set(chat_id)
        self._session_key_ctx.set(session_key or f"{channel}:{chat_id}")

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "If task_id is provided, the bound task will automatically track "
            "subagent progress and final status. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful."
        )

    async def execute(
        self,
        task: str,
        label: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        return await self._manager.spawn(
            task=task,
            label=label,
            task_id=task_id,
            origin_channel=self._origin_channel_ctx.get(),
            origin_chat_id=self._origin_chat_ctx.get(),
            session_key=self._session_key_ctx.get(),
        )
