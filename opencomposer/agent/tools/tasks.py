"""Task CRUD tools shared by the main agent and subagents."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from opencomposer.agent.task_store import TASK_STATUSES, TaskStore
from opencomposer.agent.tools.base import Tool, tool_parameters
from opencomposer.agent.tools.schema import ArraySchema, ObjectSchema, StringSchema, tool_parameters_schema

_DEFAULT_SESSION_KEY = "cli:direct"
_STATUS_SCHEMA = StringSchema(
    "Task status.",
    enum=list(TASK_STATUSES),
)
_METADATA_SCHEMA = ObjectSchema(
    description=(
        "Optional metadata object to attach to the task. "
        "When updating, keys with null values are removed."
    ),
    additional_properties=True,
)


class _TaskTool(Tool):
    """Base class for session-scoped task tools."""

    def __init__(self, store: TaskStore | None = None, *, session_key: str = _DEFAULT_SESSION_KEY):
        if store is None:
            raise ValueError("Task tools require an explicit TaskStore")
        self._store = store
        self._default_session_key = session_key
        self._session_key_ctx: ContextVar[str] = ContextVar(
            f"{self.__class__.__name__}:{id(self)}:session_key",
            default=session_key,
        )

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        self._session_key_ctx.set(session_key or f"{channel}:{chat_id}")

    @property
    def concurrency_safe(self) -> bool:
        return True

    @property
    def session_key(self) -> str:
        return self._session_key_ctx.get() or self._default_session_key or _DEFAULT_SESSION_KEY


@tool_parameters(
    tool_parameters_schema(
        subject=StringSchema("A brief title for the task"),
        description=StringSchema("What needs to be done"),
        active_form=StringSchema(
            "Present continuous form shown while the task is in progress",
            nullable=True,
        ),
        metadata=_METADATA_SCHEMA,
        required=["subject", "description"],
    )
)
class TaskCreateTool(_TaskTool):
    @property
    def name(self) -> str:
        return "task_create"

    @property
    def description(self) -> str:
        return "Create a task in the current session task list."

    async def execute(
        self,
        subject: str,
        description: str,
        active_form: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        task = await self._store.create_task(
            self.session_key,
            subject=subject,
            description=description,
            active_form=active_form,
            metadata=metadata,
        )
        return f"Task #{task.id} created successfully: {task.subject}"


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("The ID of the task to retrieve"),
        required=["task_id"],
    )
)
class TaskGetTool(_TaskTool):
    @property
    def name(self) -> str:
        return "task_get"

    @property
    def description(self) -> str:
        return "Retrieve a task by ID from the current session task list."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, task_id: str, **kwargs: Any) -> str:
        task = await self._store.get_task(self.session_key, task_id)
        if task is None:
            return "Task not found"

        lines = [
            f"Task #{task.id}: {task.subject}",
            f"Status: {task.status}",
            f"Description: {task.description}",
        ]
        if task.owner:
            lines.append(f"Owner: {task.owner}")
        if task.active_form:
            lines.append(f"Active form: {task.active_form}")
        if task.blocked_by:
            lines.append(f"Blocked by: {', '.join(f'#{item}' for item in task.blocked_by)}")
        if task.blocks:
            lines.append(f"Blocks: {', '.join(f'#{item}' for item in task.blocks)}")
        if task.metadata:
            lines.append(f"Metadata: {task.metadata}")
        return "\n".join(lines)


@tool_parameters(tool_parameters_schema())
class TaskListTool(_TaskTool):
    @property
    def name(self) -> str:
        return "task_list"

    @property
    def description(self) -> str:
        return "List all tasks for the current session."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        tasks = await self._store.list_tasks(self.session_key)
        if not tasks:
            return "No tasks found"

        completed = {task.id for task in tasks if task.status == "completed"}
        lines = []
        for task in tasks:
            owner = f" ({task.owner})" if task.owner else ""
            blockers = [item for item in task.blocked_by if item not in completed]
            blocked = (
                f" [blocked by {', '.join(f'#{item}' for item in blockers)}]"
                if blockers
                else ""
            )
            lines.append(f"#{task.id} [{task.status}] {task.subject}{owner}{blocked}")
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("The ID of the task to update"),
        subject=StringSchema("New subject for the task", nullable=True),
        description=StringSchema("New description for the task", nullable=True),
        active_form=StringSchema(
            "Present continuous form shown while the task is in progress",
            nullable=True,
        ),
        status=_STATUS_SCHEMA,
        owner=StringSchema("New owner for the task", nullable=True),
        add_blocks=ArraySchema(
            StringSchema("Task ID that this task blocks"),
            description="Task IDs that this task blocks",
        ),
        add_blocked_by=ArraySchema(
            StringSchema("Task ID that blocks this task"),
            description="Task IDs that block this task",
        ),
        metadata=_METADATA_SCHEMA,
        required=["task_id"],
    )
)
class TaskUpdateTool(_TaskTool):
    @property
    def name(self) -> str:
        return "task_update"

    @property
    def description(self) -> str:
        return "Update fields on a task in the current session task list."

    async def execute(
        self,
        task_id: str,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            existing = await self._store.get_task(self.session_key, task_id)
            if existing is None:
                return "Task not found"
            updated = await self._store.update_task(
                self.session_key,
                task_id,
                subject=subject,
                description=description,
                active_form=active_form,
                status=status,
                owner=owner,
                add_blocks=add_blocks,
                add_blocked_by=add_blocked_by,
                metadata=metadata,
            )
        except (KeyError, ValueError) as exc:
            return f"Error: {exc}"

        changed: list[str] = []
        if subject is not None and subject != existing.subject:
            changed.append("subject")
        if description is not None and description != existing.description:
            changed.append("description")
        if active_form is not None and active_form != existing.active_form:
            changed.append("active_form")
        if status is not None and status != existing.status:
            changed.append("status")
        if owner is not None and (owner.strip() or None) != existing.owner:
            changed.append("owner")
        if add_blocks:
            changed.append("blocks")
        if add_blocked_by:
            changed.append("blocked_by")
        if metadata:
            changed.append("metadata")

        if not changed:
            return f"Task #{updated.id} unchanged"
        return f"Task #{updated.id} updated successfully: {', '.join(changed)}"


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("The ID of the task to delete"),
        required=["task_id"],
    )
)
class TaskDeleteTool(_TaskTool):
    @property
    def name(self) -> str:
        return "task_delete"

    @property
    def description(self) -> str:
        return "Delete a task from the current session task list."

    async def execute(self, task_id: str, **kwargs: Any) -> str:
        deleted = await self._store.delete_task(self.session_key, task_id)
        if not deleted:
            return "Task not found"
        return f"Task #{task_id} deleted successfully"


def build_task_tools(
    *,
    session_key: str = _DEFAULT_SESSION_KEY,
    store: TaskStore | None = None,
) -> list[Tool]:
    """Build the shared task CRUD tools."""
    if store is None:
        raise ValueError("build_task_tools requires an explicit TaskStore")
    shared = store
    return [
        TaskCreateTool(shared, session_key=session_key),
        TaskGetTool(shared, session_key=session_key),
        TaskUpdateTool(shared, session_key=session_key),
        TaskDeleteTool(shared, session_key=session_key),
        TaskListTool(shared, session_key=session_key),
    ]
