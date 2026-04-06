"""Subagent manager for background task execution."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from opencomposer.agent.hook import AgentHook, AgentHookContext
from opencomposer.agent.task_store import TaskStore
from opencomposer.utils.prompt_templates import render_template
from opencomposer.agent.runner import AgentRunSpec, AgentRunner
from opencomposer.agent.skills import BUILTIN_SKILLS_DIR
from opencomposer.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from opencomposer.agent.tools.registry import ToolRegistry
from opencomposer.agent.tools.search import GlobTool, GrepTool
from opencomposer.agent.tools.shell import ExecTool
from opencomposer.agent.tools.tasks import build_task_tools
from opencomposer.agent.tools.web import WebFetchTool, WebSearchTool
from opencomposer.bus.events import InboundMessage
from opencomposer.bus.queue import MessageBus
from opencomposer.config.paths import get_workspace_tasks_dir
from opencomposer.config.schema import ExecToolConfig, WebToolsConfig
from opencomposer.providers.base import LLMProvider


class _SubagentHook(AgentHook):
    """Logging-only hook for subagent execution."""

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        web_config: "WebToolsConfig | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        task_store: TaskStore | None = None,
    ):
        from opencomposer.config.schema import ExecToolConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.web_config = web_config or WebToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.runner = AgentRunner(provider)
        self.task_store = task_store or TaskStore(root_dir=get_workspace_tasks_dir(workspace))
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {subagent_id, ...}
        self._bound_tasks: dict[str, str] = {}  # subagent_id -> task_id

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        task_id: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        bound_task_id = task_id.strip() if isinstance(task_id, str) and task_id.strip() else None
        if bound_task_id is not None:
            if not session_key:
                return "Error: task_id binding requires a session context"
            bound_task = await self.task_store.get_task(session_key, bound_task_id)
            if bound_task is None:
                return f"Error: Task #{bound_task_id} not found in the current session"

        subagent_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id}
        if bound_task_id is not None:
            self._bound_tasks[subagent_id] = bound_task_id

        bg_task = asyncio.create_task(
            self._run_subagent(
                subagent_id,
                task,
                display_label,
                origin,
                session_key,
                bound_task_id=bound_task_id,
            )
        )
        self._running_tasks[subagent_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(subagent_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(subagent_id, None)
            self._bound_tasks.pop(subagent_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(subagent_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", subagent_id, display_label)
        return f"Subagent [{display_label}] started (id: {subagent_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        subagent_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        session_key: str | None,
        *,
        bound_task_id: str | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", subagent_id, label)

        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            allowed_dir = self.workspace if self.restrict_to_workspace else None
            extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
            tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
            tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(GlobTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(GrepTool(workspace=self.workspace, allowed_dir=allowed_dir))
            if self.exec_config.enable:
                tools.register(ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    path_append=self.exec_config.path_append,
                ))
            if self.web_config.enable:
                tools.register(WebSearchTool(config=self.web_config.search, proxy=self.web_config.proxy))
                tools.register(WebFetchTool(proxy=self.web_config.proxy))
            for tool in build_task_tools(session_key=session_key or 'cli:direct', store=self.task_store):
                tools.register(tool)
            await self._sync_bound_task(
                session_key,
                bound_task_id,
                status="in_progress",
                subagent_id=subagent_id,
                owner=f"subagent:{subagent_id}",
            )
            system_prompt = self._build_subagent_prompt()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=self.model,
                max_iterations=15,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=_SubagentHook(subagent_id),
                max_iterations_message="Task completed but no final response was generated.",
                error_message=None,
                fail_on_tool_error=True,
            ))
            if result.stop_reason == "tool_error":
                await self._sync_bound_task(
                    session_key,
                    bound_task_id,
                    status="failed",
                    subagent_id=subagent_id,
                    owner=f"subagent:{subagent_id}",
                )
                await self._announce_result(
                    subagent_id,
                    label,
                    task,
                    self._format_partial_progress(result),
                    origin,
                    "error",
                    session_key=session_key,
                )
                return
            if result.stop_reason == "error":
                await self._sync_bound_task(
                    session_key,
                    bound_task_id,
                    status="failed",
                    subagent_id=subagent_id,
                    owner=f"subagent:{subagent_id}",
                )
                await self._announce_result(
                    subagent_id,
                    label,
                    task,
                    result.error or "Error: subagent execution failed.",
                    origin,
                    "error",
                    session_key=session_key,
                )
                return
            final_result = result.final_content or "Task completed but no final response was generated."

            logger.info("Subagent [{}] completed successfully", subagent_id)
            await self._sync_bound_task(
                session_key,
                bound_task_id,
                status="completed",
                subagent_id=subagent_id,
                owner=f"subagent:{subagent_id}",
            )
            await self._announce_result(
                subagent_id,
                label,
                task,
                final_result,
                origin,
                "ok",
                session_key=session_key,
            )

        except asyncio.CancelledError:
            logger.info("Subagent [{}] cancelled", subagent_id)
            await self._sync_bound_task(
                session_key,
                bound_task_id,
                status="cancelled",
                subagent_id=subagent_id,
                owner=f"subagent:{subagent_id}",
            )
            raise
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", subagent_id, e)
            await self._sync_bound_task(
                session_key,
                bound_task_id,
                status="failed",
                subagent_id=subagent_id,
                owner=f"subagent:{subagent_id}",
            )
            await self._announce_result(
                subagent_id,
                label,
                task,
                error_msg,
                origin,
                "error",
                session_key=session_key,
            )

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        *,
        session_key: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=session_key,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    async def _sync_bound_task(
        self,
        session_key: str | None,
        task_id: str | None,
        *,
        status: str,
        subagent_id: str | None = None,
        owner: str | None = None,
    ) -> None:
        if not session_key or not task_id:
            return
        try:
            await self.task_store.update_task(
                session_key,
                task_id,
                status=status,
                owner=owner,
                metadata={"subagent_id": subagent_id} if subagent_id else None,
            )
        except KeyError:
            logger.warning(
                "Bound task {} not found for session {}; skipping status {}",
                task_id,
                session_key,
                status,
            )
        except ValueError as exc:
            logger.warning("Failed to update bound task {}: {}", task_id, exc)

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(self) -> str:
        """Build a focused system prompt for the subagent."""
        from opencomposer.agent.context import ContextBuilder
        from opencomposer.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        skills_summary = SkillsLoader(self.workspace).build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(self.workspace),
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def bound_task_ids_for_session(self, session_key: str) -> set[str]:
        """Return bound task IDs for currently running subagents in the session."""
        return {
            task_id
            for subagent_id in self._session_tasks.get(session_key, set())
            if (task_id := self._bound_tasks.get(subagent_id))
        }

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
