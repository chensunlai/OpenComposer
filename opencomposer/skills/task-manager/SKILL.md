---
name: task-manager
description: Use the session task system to split work, sequence tasks, delegate bounded subtasks to subagents, and clean up task state after completion or interruption.
always: true
---

# Task Manager

Use tasks only when explicit state helps. Tasks are a working plan, not a rigid script.

## When To Use Tasks

Create tasks when the work has multiple steps, dependencies, likely follow-up, delegation potential, or a need to report progress.

Skip tasks for:
- one short reply
- one obvious tool call
- conversational turns where task state adds noise

## How To Split Work

- Split by outcomes, not micro-actions.
- Keep the number of tasks small.
- Make each task understandable from `subject` and `description`.
- Use `blocked_by` and `blocks` only for real dependencies.
- The main agent may complete tasks sequentially without any subagent.

Good task shapes: `inspect logs`, `check config`, `apply fix and verify`.
Bad task shapes: `open terminal`, `run grep`, `read line 14`.

## Core Flow

1. Create the smallest useful set with `task_create`.
2. Mark the active task `in_progress` with `task_update`.
3. Finish it with `completed`, `failed`, or `cancelled`.
4. Use `task_list` or `task_get` when you need to re-orient.
5. Unless the user says otherwise, keep executing until reachable tasks are done or a material issue means you should stop and report back.

Statuses:
- `pending`: not started or paused
- `in_progress`: actively worked on now
- `completed`: finished
- `failed`: attempted but blocked by an unresolved problem
- `cancelled`: intentionally stopped or no longer relevant

Do not leave stale tasks in `in_progress`.

Default after planning: continue executing; stop and update the user when a material issue blocks continuation.

## Adapt The Plan

If new information changes the situation, do not follow the old plan blindly.

- Update the current task first.
- Reorder, update, add, or delete downstream tasks to match reality.
- Cancel or delete tasks that are no longer relevant.
- If scope, risk, or expected output changed, tell the user.

Prefer an accurate current plan over a stale consistent-looking one.

## When To Use A Subagent

Delegate only when the sub-work is clearly bounded, independently checkable, and can run without blocking your immediate next step.

Keep work with the main agent when it is tiny, tightly coupled, or needed right away.

When delegating:
1. Create or identify the task first.
2. Call `spawn(..., task_id="<id>")`; when `task_id` is bound, the subagent uses that task's subject and description as its assignment, so do not duplicate the task text.
3. Give the subagent a concrete assignment and expected deliverable.
4. If your next step truly depends on that result, use `task_wait(task_id=..., timeout_seconds=...)` with a bounded timeout.
5. Read the stored output with `task_get_result(task_id=...)` when you need the subagent's final result.

Bound subagent tasks may move automatically through `in_progress`, `completed`, `failed`, or `cancelled`, and their final output is stored on the task instead of being auto-pushed back as a message. If you do not bind `task_id`, the subagent follows the older auto-reply behavior.

## Cleanup And Interruptions

- Keep tasks while they are still useful for coordination or reporting.
- After resolved child tasks are no longer needed, remove them with `task_delete`.
- Use `task_delete(task_id=...)`, `task_delete(task_ids=[...])`, `task_delete(before_task_id=...)`, or `task_delete(clear_all=true)` as appropriate.
- If interrupted or reprioritized, update the current task before switching.
- If work should resume later, move it back to `pending` and record the pause reason in `metadata`.
- If a partial result matters, keep it in `description` or `metadata`.
- If the interruption changes the plan, rewrite later tasks before continuing.
- If `task_wait` times out, re-plan based on the current status or tell the user what is still pending.
