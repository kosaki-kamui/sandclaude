"""
Runner Pool - concurrent task execution with pluggable scheduler backend.

v0.4.0: Introduces the Scheduler protocol so the in-process semaphore
backend (LocalScheduler) can be swapped for Redis or other distributed
backends in a future release without changing callers.

Module-level functions (submit_task, drain_queue, get_pool_stats) delegate
to a singleton scheduler instance. Tests can swap the backend via
set_scheduler() or inject a custom runner via set_runner_fn().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.models import Task, TaskStatus
from sandclaude.runner.container import run_task_in_container
from sandclaude.runner.webhook import send_webhook

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduler protocol — implement this to add a new backend (e.g. Redis)
# ---------------------------------------------------------------------------


@runtime_checkable
class Scheduler(Protocol):
    """Abstract scheduler interface for task execution backends.

    Implementations must be async-safe and handle:
    - Slot tracking (max concurrency)
    - Queue drain (priority ordering)
    - Double-schedule prevention
    """

    async def submit(self, task: Task) -> None:
        """Submit a task for execution. Non-blocking."""
        ...

    async def drain(self) -> None:
        """Start queued tasks if slots are available."""
        ...

    async def stats(self) -> dict[str, int]:
        """Return pool statistics (max_concurrent, active, queued)."""
        ...


# ---------------------------------------------------------------------------
# LocalScheduler — in-process semaphore backend (default)
# ---------------------------------------------------------------------------


class LocalScheduler:
    """Single-process scheduler using asyncio.Semaphore.

    Queue drain order: high priority first, then normal, then low.
    Within same priority, FIFO by creation time.
    """

    def __init__(self) -> None:
        self._semaphore: asyncio.Semaphore | None = None
        self._scheduled: set[str] = set()
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._runner_fn: Callable[[Task], Coroutine[Any, Any, dict]] = run_task_in_container

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(settings.max_concurrent)
        return self._semaphore

    def _spawn(self, coro: Coroutine) -> None:  # type: ignore[type-arg]
        """Create a tracked background task that cleans up after itself."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def submit(self, task: Task) -> None:
        """Submit a task to the pool. Non-blocking - returns immediately."""
        if task.id in self._scheduled:
            return
        self._scheduled.add(task.id)
        self._spawn(self._run_with_semaphore(task))

    async def _run_with_semaphore(self, task: Task) -> None:
        """Acquire a slot, run the task, then release and drain the queue."""
        sem = self._get_semaphore()
        try:
            async with sem:
                fresh = await db.get_task(task.id)
                if not fresh or fresh.status not in (TaskStatus.queued, TaskStatus.setup):
                    status = fresh.status.value if fresh else "deleted"
                    logger.info("Skipping task %s: status is %s", task.id, status)
                    return

                try:
                    await self._runner_fn(task)
                except Exception as exc:
                    logger.error("Runner failed for task %s: %s", task.id, exc)

                completed_task = await db.get_task(task.id)
                if completed_task:
                    try:
                        await send_webhook(completed_task)
                    except Exception as exc:
                        logger.error("Webhook failed for task %s: %s", task.id, exc)
        finally:
            self._scheduled.discard(task.id)
            await self.drain()

    async def drain(self) -> None:
        """Start queued tasks if slots are available."""
        active = await db.count_active()
        available = settings.max_concurrent - active
        if available <= 0:
            return

        queued = await db.get_queued_by_priority()
        to_start = [t for t in queued if t.id not in self._scheduled][:available]
        for task in to_start:
            self._scheduled.add(task.id)
            self._spawn(self._run_with_semaphore(task))

    async def stats(self) -> dict[str, int]:
        return {
            "max_concurrent": settings.max_concurrent,
            "active": await db.count_active(),
            "queued": len(await db.get_queued_by_priority()),
        }


# ---------------------------------------------------------------------------
# Module-level singleton and compatibility shims
# ---------------------------------------------------------------------------

_scheduler: Scheduler = LocalScheduler()


def get_scheduler() -> Scheduler:
    return _scheduler


def set_scheduler(scheduler: Scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


# Pluggable runner function for testing (LocalScheduler only)
def set_runner_fn(fn: Callable[[Task], Coroutine[Any, Any, dict]]) -> None:
    if isinstance(_scheduler, LocalScheduler):
        _scheduler._runner_fn = fn


def reset_runner_fn() -> None:
    global _scheduler
    _scheduler = LocalScheduler()


# Module-level functions that delegate to the active scheduler.
# These preserve the existing API so callers don't need to change.
async def submit_task(task: Task) -> None:
    await _scheduler.submit(task)


async def drain_queue() -> None:
    await _scheduler.drain()


async def get_pool_stats() -> dict[str, int]:
    return await _scheduler.stats()
