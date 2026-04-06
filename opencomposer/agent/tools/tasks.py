"""Task CRUD tools shared by the main agent and subagents."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import Any

from opencomposer.agent.task_store import TASK_STATUSES, TaskStore
from opencomposer.agent.tools.base import Tool, tool_parameters
from opencomposer.agent.tools.schema import ArraySchema, BooleanSchema, IntegerSchema, ObjectSchema, StringSchema, tool_parameters_schema

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
        properties={
            "subject": StringSchema("A brief title for the task"),
            "description": StringSchema("What needs to be done"),
            "active_form": StringSchema(
                "Present continuous form shown while the task is in progress",
                nullable=True,
            ),
            "metadata": _METADATA_SCHEMA,
        },
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
        if task.result is not None:
            lines.append("Result: available")
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("The ID of the task to wait for"),
        timeout_seconds=IntegerSchema(
            300,
            description="Maximum number of seconds to wait before returning",
            minimum=0,
        ),
        required=["task_id"],
    )
)
class TaskWaitTool(_TaskTool):
    _TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

    @property
    def name(self) -> str:
        return "task_wait"

    @property
    def description(self) -> str:
        return "Wait for a task to reach a terminal status or until the timeout expires."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        task_id: str,
        timeout_seconds: int = 300,
        **kwargs: Any,
    ) -> str:
        timeout = max(0, int(timeout_seconds))
        deadline = time.monotonic() + timeout
        poll_interval = 0.5

        while True:
            task = await self._store.get_task(self.session_key, task_id)
            if task is None:
                return "Task not found"

            if task.status in self._TERMINAL_STATUSES:
                suffix = " Result is available via task_get_result." if task.result is not None else ""
                return f"Task #{task.id} finished with status: {task.status}.{suffix}"

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (
                    f"Timed out after {timeout}s waiting for task #{task.id}. "
                    f"Current status: {task.status}"
                )

            await asyncio.sleep(min(poll_interval, remaining))


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema("The ID of the task whose stored result should be returned"),
        required=["task_id"],
    )
)
class TaskGetResultTool(_TaskTool):
    @property
    def name(self) -> str:
        return "task_get_result"

    @property
    def description(self) -> str:
        return "Retrieve the stored result for a task in the current session."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, task_id: str, **kwargs: Any) -> str:
        task = await self._store.get_task(self.session_key, task_id)
        if task is None:
            return "Task not found"
        if task.result is None:
            return f"Task #{task.id} has no stored result yet. Current status: {task.status}"
        return f"Task #{task.id} result:\n{task.result}"


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
        properties={
            "task_id": StringSchema("The ID of the task to update"),
            "subject": StringSchema("New subject for the task", nullable=True),
            "description": StringSchema("New description for the task", nullable=True),
            "active_form": StringSchema(
                "Present continuous form shown while the task is in progress",
                nullable=True,
            ),
            "status": _STATUS_SCHEMA,
            "owner": StringSchema("New owner for the task", nullable=True),
            "add_blocks": ArraySchema(
                StringSchema("Task ID that this task blocks"),
                description="Task IDs that this task blocks",
            ),
            "add_blocked_by": ArraySchema(
                StringSchema("Task ID that blocks this task"),
                description="Task IDs that block this task",
            ),
            "metadata": _METADATA_SCHEMA,
        },
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
        task_id=StringSchema("The ID of the task to delete", nullable=True),
        task_ids=ArraySchema(
            StringSchema("A task ID to delete"),
            description="Delete multiple tasks by ID in one call",
            nullable=True,
        ),
        before_task_id=StringSchema(
            "Delete all tasks with IDs ordered before this task ID (does not delete the target task)",
            nullable=True,
        ),
        clear_all=BooleanSchema(
            description="Delete every task in the current session task list",
            default=False,
        ),
    )
)
class TaskDeleteTool(_TaskTool):
    @property
    def name(self) -> str:
        return "task_delete"

    @property
    def description(self) -> str:
        return "Delete tasks from the current session task list: a single task, multiple tasks, all tasks before an ID, or the entire list."

    async def execute(
        self,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
        before_task_id: str | None = None,
        clear_all: bool = False,
        **kwargs: Any,
    ) -> str:
        normalized_task_id = task_id.strip() if isinstance(task_id, str) else ""
        normalized_ids = [item.strip() for item in (task_ids or []) if isinstance(item, str) and item.strip()]
        normalized_before_id = before_task_id.strip() if isinstance(before_task_id, str) else ""

        modes_selected = sum(
            [
                1 if (normalized_task_id or normalized_ids) else 0,
                1 if normalized_before_id else 0,
                1 if clear_all else 0,
            ]
        )
        if modes_selected != 1:
            return (
                "Error: Choose exactly one delete mode: task_id/task_ids, before_task_id, or clear_all=true"
            )

        if clear_all:
            removed = await self._store.clear_session(self.session_key)
            return f"Deleted {removed} task(s) from the current session"

        if normalized_before_id:
            try:
                deleted = await self._store.delete_tasks_before(self.session_key, normalized_before_id)
            except KeyError as exc:
                return f"Error: {exc}"
            if not deleted:
                return f"No tasks exist before task #{normalized_before_id}"
            return (
                f"Deleted {len(deleted)} task(s) before task #{normalized_before_id}: "
                + ", ".join(f"#{task}" for task in deleted)
            )

        delete_ids: list[str] = []
        if normalized_task_id:
            delete_ids.append(normalized_task_id)
        delete_ids.extend(normalized_ids)
        ordered_delete_ids = list(dict.fromkeys(delete_ids))
        deleted = await self._store.delete_task_ids(self.session_key, ordered_delete_ids)
        if not deleted:
            return "Task not found"
        if len(ordered_delete_ids) == 1 and len(deleted) == 1:
            return f"Task #{deleted[0]} deleted successfully"
        missing = [task for task in ordered_delete_ids if task not in set(deleted)]
        message = f"Deleted {len(deleted)} task(s): " + ", ".join(f"#{task}" for task in deleted)
        if missing:
            message += " | Not found: " + ", ".join(f"#{task}" for task in missing)
        return message


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
        TaskWaitTool(shared, session_key=session_key),
        TaskGetResultTool(shared, session_key=session_key),
        TaskUpdateTool(shared, session_key=session_key),
        TaskDeleteTool(shared, session_key=session_key),
        TaskListTool(shared, session_key=session_key),
    ]
