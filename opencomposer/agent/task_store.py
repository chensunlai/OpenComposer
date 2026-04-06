"""Persistent task storage shared by the main agent and subagents."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from opencomposer.config.paths import get_tasks_dir
from opencomposer.utils.helpers import ensure_dir, safe_filename, _write_text_atomic

TASK_STATUSES = ("pending", "in_progress", "completed", "failed", "cancelled")
_DEFAULT_SESSION_KEY = "cli:direct"
_COUNTER_FILE = ".counter"

_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


@dataclass(slots=True)
class TaskRecord:
    """Persistent task entry."""

    id: str
    subject: str
    description: str
    status: str = "pending"
    active_form: str | None = None
    owner: str | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    result_updated_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        status = str(data.get("status") or "pending")
        if status not in TASK_STATUSES:
            status = "pending"
        return cls(
            id=str(data["id"]),
            subject=str(data.get("subject") or ""),
            description=str(data.get("description") or ""),
            status=status,
            active_form=data.get("active_form"),
            owner=data.get("owner"),
            blocks=[str(item) for item in data.get("blocks") or []],
            blocked_by=[str(item) for item in data.get("blocked_by") or []],
            metadata=dict(data.get("metadata") or {}),
            result=str(data.get("result")) if data.get("result") is not None else None,
            result_updated_at=(
                str(data.get("result_updated_at"))
                if data.get("result_updated_at") is not None
                else None
            ),
            created_at=str(data.get("created_at") or datetime.now().isoformat()),
            updated_at=str(data.get("updated_at") or datetime.now().isoformat()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStore:
    """File-backed task store with per-session task lists."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self._root_dir = ensure_dir(root_dir or get_tasks_dir())

    async def create_task(
        self,
        session_key: str,
        *,
        subject: str,
        description: str,
        active_form: str | None = None,
        owner: str | None = None,
        status: str = "pending",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        if status not in TASK_STATUSES:
            raise ValueError(f"Unsupported task status: {status}")

        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            self._validate_dependencies(tasks, blocks or [], blocked_by or [])
            task_id = self._allocate_task_id(session_key)
            now = datetime.now().isoformat()
            task = TaskRecord(
                id=task_id,
                subject=subject.strip(),
                description=description.strip(),
                status=status,
                active_form=active_form.strip() if active_form else None,
                owner=owner.strip() if isinstance(owner, str) and owner.strip() else owner,
                blocks=self._dedupe_ids(blocks or []),
                blocked_by=self._dedupe_ids(blocked_by or []),
                metadata=dict(metadata or {}),
                created_at=now,
                updated_at=now,
            )
            tasks[task_id] = task
            self._sync_relationships(tasks, task)
            self._persist_tasks(session_key, tasks)
            return task

    async def get_task(self, session_key: str, task_id: str) -> TaskRecord | None:
        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            task = tasks.get(str(task_id))
            return TaskRecord.from_dict(task.to_dict()) if task else None

    async def list_tasks(self, session_key: str) -> list[TaskRecord]:
        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            return [TaskRecord.from_dict(task.to_dict()) for task in self._sorted_tasks(tasks)]

    async def update_task(
        self,
        session_key: str,
        task_id: str,
        *,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            task = tasks.get(str(task_id))
            if task is None:
                raise KeyError(f"Task {task_id} not found")

            if status is not None and status not in TASK_STATUSES:
                raise ValueError(f"Unsupported task status: {status}")

            self._validate_dependencies(tasks, add_blocks or [], add_blocked_by or [], current_id=task.id)

            if subject is not None:
                task.subject = subject.strip()
            if description is not None:
                task.description = description.strip()
            if active_form is not None:
                task.active_form = active_form.strip() or None
            if status is not None:
                task.status = status
            if owner is not None:
                task.owner = owner.strip() or None
            if add_blocks:
                task.blocks = self._dedupe_ids([*task.blocks, *add_blocks])
            if add_blocked_by:
                task.blocked_by = self._dedupe_ids([*task.blocked_by, *add_blocked_by])
            if metadata:
                merged = dict(task.metadata)
                for key, value in metadata.items():
                    if value is None:
                        merged.pop(key, None)
                    else:
                        merged[key] = value
                task.metadata = merged

            task.updated_at = datetime.now().isoformat()
            self._sync_relationships(tasks, task)
            self._persist_tasks(session_key, tasks)
            return TaskRecord.from_dict(task.to_dict())

    async def delete_task(self, session_key: str, task_id: str) -> bool:
        deleted = await self.delete_task_ids(session_key, [task_id])
        return bool(deleted)

    async def delete_task_ids(self, session_key: str, task_ids: list[str] | set[str]) -> list[str]:
        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            deleted = self._delete_task_ids_locked(tasks, {str(task_id) for task_id in task_ids})
            if deleted:
                self._persist_tasks(session_key, tasks)
            return deleted

    async def delete_tasks_before(self, session_key: str, task_id: str) -> list[str]:
        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            target = tasks.get(str(task_id))
            if target is None:
                raise KeyError(f"Task {task_id} not found")

            target_key = self._task_sort_key(target.id)
            delete_ids = {
                candidate.id
                for candidate in self._sorted_tasks(tasks)
                if self._task_sort_key(candidate.id) < target_key
            }
            deleted = self._delete_task_ids_locked(tasks, delete_ids)
            if deleted:
                self._persist_tasks(session_key, tasks)
            return deleted

    async def clear_session(self, session_key: str) -> int:
        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            removed = len(tasks)
            list_dir = self._task_list_dir(session_key)
            for path in list_dir.glob("*.json"):
                path.unlink(missing_ok=True)
            self._counter_path(session_key).unlink(missing_ok=True)
            try:
                list_dir.rmdir()
            except OSError:
                pass
            return removed

    async def set_task_result(self, session_key: str, task_id: str, result: str | None) -> TaskRecord:
        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            task = tasks.get(str(task_id))
            if task is None:
                raise KeyError(f"Task {task_id} not found")

            task.result = result
            task.result_updated_at = datetime.now().isoformat() if result is not None else None
            task.updated_at = datetime.now().isoformat()
            self._persist_tasks(session_key, tasks)
            return TaskRecord.from_dict(task.to_dict())

    async def transition_tasks(
        self,
        session_key: str,
        *,
        from_statuses: set[str] | None = None,
        to_status: str,
    ) -> list[str]:
        if to_status not in TASK_STATUSES:
            raise ValueError(f"Unsupported task status: {to_status}")

        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            changed: list[str] = []
            now = datetime.now().isoformat()
            for task in self._sorted_tasks(tasks):
                if from_statuses and task.status not in from_statuses:
                    continue
                if task.status == to_status:
                    continue
                task.status = to_status
                task.updated_at = now
                changed.append(task.id)
            if changed:
                self._persist_tasks(session_key, tasks)
            return changed

    async def transition_task_ids(
        self,
        session_key: str,
        task_ids: set[str] | list[str],
        *,
        to_status: str,
        from_statuses: set[str] | None = None,
    ) -> list[str]:
        if to_status not in TASK_STATUSES:
            raise ValueError(f"Unsupported task status: {to_status}")

        target_ids = {str(task_id) for task_id in task_ids}
        if not target_ids:
            return []

        async with self._lock_for(session_key):
            tasks = self._load_tasks(session_key)
            changed: list[str] = []
            now = datetime.now().isoformat()
            for task_id in sorted(target_ids, key=lambda value: (int(value), value) if value.isdigit() else (10**9, value)):
                task = tasks.get(task_id)
                if task is None:
                    continue
                if from_statuses and task.status not in from_statuses:
                    continue
                if task.status == to_status:
                    continue
                task.status = to_status
                task.updated_at = now
                changed.append(task.id)
            if changed:
                self._persist_tasks(session_key, tasks)
            return changed

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        key = (
            str(self._root_dir.resolve(strict=False)),
            self._normalize_session_key(session_key),
        )
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = asyncio.Lock()
        return lock

    def _normalize_session_key(self, session_key: str | None) -> str:
        return session_key or _DEFAULT_SESSION_KEY

    def _task_list_dir(self, session_key: str | None) -> Path:
        return ensure_dir(self._root_dir / safe_filename(self._normalize_session_key(session_key)))

    def _task_path(self, session_key: str | None, task_id: str) -> Path:
        return self._task_list_dir(session_key) / f"{safe_filename(str(task_id))}.json"

    def _counter_path(self, session_key: str | None) -> Path:
        return self._task_list_dir(session_key) / _COUNTER_FILE

    def _load_tasks(self, session_key: str | None) -> dict[str, TaskRecord]:
        tasks: dict[str, TaskRecord] = {}
        for path in self._task_list_dir(session_key).glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            task = TaskRecord.from_dict(data)
            tasks[task.id] = task
        return tasks

    def _persist_tasks(self, session_key: str | None, tasks: dict[str, TaskRecord]) -> None:
        list_dir = self._task_list_dir(session_key)
        expected = {f"{safe_filename(task_id)}.json" for task_id in tasks}
        for task in tasks.values():
            payload = json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
            _write_text_atomic(self._task_path(session_key, task.id), payload + "\n")
        for path in list_dir.glob("*.json"):
            if path.name not in expected:
                path.unlink(missing_ok=True)

    def _allocate_task_id(self, session_key: str | None) -> str:
        counter_path = self._counter_path(session_key)
        current = 0
        if counter_path.exists():
            try:
                current = int(counter_path.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                current = 0
        if current <= 0:
            for path in self._task_list_dir(session_key).glob("*.json"):
                stem = path.stem
                if stem.isdigit():
                    current = max(current, int(stem))
        next_value = current + 1
        _write_text_atomic(counter_path, f"{next_value}\n")
        return str(next_value)

    @staticmethod
    def _dedupe_ids(task_ids: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in task_ids:
            value = str(item)
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

    @staticmethod
    def _sorted_tasks(tasks: dict[str, TaskRecord]) -> list[TaskRecord]:
        return sorted(tasks.values(), key=lambda task: TaskStore._task_sort_key(task.id))

    @staticmethod
    def _task_sort_key(task_id: str) -> tuple[int, str]:
        return (int(task_id), task_id) if task_id.isdigit() else (10**9, task_id)

    @staticmethod
    def _delete_task_ids_locked(tasks: dict[str, TaskRecord], delete_ids: set[str]) -> list[str]:
        if not delete_ids:
            return []

        removed_ids: list[str] = []
        for task_id in sorted(delete_ids, key=TaskStore._task_sort_key):
            removed = tasks.pop(task_id, None)
            if removed is not None:
                removed_ids.append(removed.id)

        if not removed_ids:
            return []

        removed_set = set(removed_ids)
        now = datetime.now().isoformat()
        for other in tasks.values():
            next_blocks = [item for item in other.blocks if item not in removed_set]
            next_blocked_by = [item for item in other.blocked_by if item not in removed_set]
            if next_blocks != other.blocks or next_blocked_by != other.blocked_by:
                other.blocks = next_blocks
                other.blocked_by = next_blocked_by
                other.updated_at = now

        return removed_ids

    def _validate_dependencies(
        self,
        tasks: dict[str, TaskRecord],
        blocks: list[str],
        blocked_by: list[str],
        current_id: str | None = None,
    ) -> None:
        for rel_id in [*blocks, *blocked_by]:
            rel_key = str(rel_id)
            if rel_key == current_id:
                raise ValueError("A task cannot depend on itself")
            if rel_key not in tasks:
                raise KeyError(f"Related task {rel_key} not found")

    def _sync_relationships(self, tasks: dict[str, TaskRecord], task: TaskRecord) -> None:
        task.blocks = [item for item in self._dedupe_ids(task.blocks) if item != task.id]
        task.blocked_by = [item for item in self._dedupe_ids(task.blocked_by) if item != task.id]

        for other in tasks.values():
            if other.id == task.id:
                continue
            if task.id in other.blocked_by and other.id not in task.blocks:
                other.blocked_by = [item for item in other.blocked_by if item != task.id]
            if task.id in other.blocks and other.id not in task.blocked_by:
                other.blocks = [item for item in other.blocks if item != task.id]

        for blocked_id in task.blocks:
            other = tasks[blocked_id]
            if task.id not in other.blocked_by:
                other.blocked_by.append(task.id)
                other.blocked_by = self._dedupe_ids(other.blocked_by)
                other.updated_at = datetime.now().isoformat()

        for blocker_id in task.blocked_by:
            other = tasks[blocker_id]
            if task.id not in other.blocks:
                other.blocks.append(task.id)
                other.blocks = self._dedupe_ids(other.blocks)
                other.updated_at = datetime.now().isoformat()
