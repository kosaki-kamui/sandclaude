"""
Runner Pool - concurrent task execution with asyncio.Semaphore.

When a task is submitted:
1. Task ID is added to _scheduled set (prevents double-scheduling)
2. A background coroutine waits on the semaphore for a slot
3. When a slot opens, the task runs in a container
4. After completion, the slot is released and queued tasks are drained

Queue drain order: high priority first, then normal, then low.
Within same priority, FIFO by creation time.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.models import Task, TaskStatus
from sandclaude.runner.container import run_task_in_container
from sandclaude.runner.webhook import send_webhook

_semaphore: asyncio.Semaphore | None = None

# Track task IDs that are scheduled (waiting on semaphore or running).
# Prevents drain_queue() from double-scheduling a task that submit_task()
# already queued but hasn't acquired the semaphore yet.
_scheduled: set[str] = set()

# Track background asyncio tasks so they aren't garbage-collected mid-run
_background_tasks: set[asyncio.Task] = set()

# Pluggable runner function for testing
_runner_fn: Callable[[Task], Coroutine[Any, Any, dict]] = run_task_in_container


def set_runner_fn(fn: Callable[[Task], Coroutine[Any, Any, dict]]) -> None:
    global _runner_fn
    _runner_fn = fn


def reset_runner_fn() -> None:
    global _runner_fn, _scheduled
    _runner_fn = run_task_in_container
    _scheduled = set()
    _background_tasks.clear()


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent)
    return _semaphore


def _spawn(coro: Coroutine) -> None:
    """Create a tracked background task that cleans up after itself."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def submit_task(task: Task) -> None:
    """Submit a task to the pool. Non-blocking - returns immediately."""
    if task.id in _scheduled:
        return  # Already scheduled
    _scheduled.add(task.id)
    _spawn(_run_with_semaphore(task))


async def _run_with_semaphore(task: Task) -> None:
    """Acquire a slot, run the task, then release and drain the queue."""
    sem = _get_semaphore()
    try:
        async with sem:
            # Re-fetch task state after acquiring semaphore — the task may have
            # been cancelled while waiting in the queue. Without this check,
            # a cancelled task would still consume a runner slot and execute.
            fresh = await db.get_task(task.id)
            if not fresh or fresh.status not in (TaskStatus.queued, TaskStatus.setup):
                status = fresh.status.value if fresh else "deleted"
                print(f"[pool] Skipping task {task.id}: status is {status}")
                return

            try:
                await _runner_fn(task)
            except Exception as exc:
                print(f"[pool] Runner failed for task {task.id}: {exc}")

            # Send webhook notification
            completed_task = await db.get_task(task.id)
            if completed_task:
                try:
                    await send_webhook(completed_task)
                except Exception as exc:
                    print(f"[pool] Webhook failed for task {task.id}: {exc}")
    finally:
        # Always clean up and drain, even if webhook fails
        _scheduled.discard(task.id)
        await drain_queue()


async def drain_queue() -> None:
    """Start queued tasks if slots are available."""
    active = await db.count_active()
    available = settings.max_concurrent - active

    if available <= 0:
        return

    queued = await db.get_queued_by_priority()
    to_start = [t for t in queued if t.id not in _scheduled][:available]

    for task in to_start:
        _scheduled.add(task.id)
        _spawn(_run_with_semaphore(task))


async def get_pool_stats() -> dict[str, int]:
    return {
        "max_concurrent": settings.max_concurrent,
        "active": await db.count_active(),
        "queued": len(await db.get_queued_by_priority()),
    }
