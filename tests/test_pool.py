"""Tests for runner pool concurrency and priority queue."""

from __future__ import annotations

import asyncio

import pytest

from sandclaude.db.store import (
    create_task,
    get_queued_by_priority,
    get_task,
    init_db,
    update_task,
)
from sandclaude.models import TaskPriority, TaskStatus
from sandclaude.runner.pool import (
    LocalScheduler,
    Scheduler,
    get_scheduler,
    reset_runner_fn,
    set_runner_fn,
    submit_task,
)


@pytest.fixture(autouse=True)
async def _setup(tmp_path):
    import sandclaude.db.store as store

    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()
    yield
    reset_runner_fn()


async def test_3_concurrent_4th_queued():
    """Submit 4 tasks, verify 3 run in parallel and 4th queues."""
    concurrent = 0
    peak = 0
    timeline: list[dict] = []
    t0 = asyncio.get_event_loop().time()

    async def mock_runner(task):
        nonlocal concurrent, peak
        await update_task(
            task.id, status=TaskStatus.setup, started_at="now", container_id=f"c-{task.id}"
        )
        await update_task(task.id, status=TaskStatus.running)

        concurrent += 1
        if concurrent > peak:
            peak = concurrent
        timeline.append(
            {
                "task": task.id,
                "event": "start",
                "time": asyncio.get_event_loop().time() - t0,
                "concurrent": concurrent,
            }
        )

        await asyncio.sleep(0.3)

        concurrent -= 1
        timeline.append(
            {
                "task": task.id,
                "event": "end",
                "time": asyncio.get_event_loop().time() - t0,
                "concurrent": concurrent,
            }
        )

        await update_task(
            task.id,
            status=TaskStatus.completed,
            completed_at="now",
            tokens_input=100,
            tokens_output=50,
            total_cost_usd=0.01,
        )
        return {"success": True}

    set_runner_fn(mock_runner)

    import sandclaude.config as cfg

    cfg.settings.max_concurrent = 3

    # Reset semaphore
    import sandclaude.runner.pool as pool_mod

    pool_mod._semaphore = None

    # Submit 4 tasks
    for i in range(1, 5):
        task = await create_task(
            task_id=f"task-{i}", repo=".", prompt=f"T{i}", model="m", max_turns=5
        )
        await submit_task(task)

    # Wait for all to complete
    await asyncio.sleep(1.5)

    # Verify
    assert peak <= 3, f"Peak concurrent was {peak}, expected <= 3"

    for i in range(1, 5):
        t = await get_task(f"task-{i}")
        assert t is not None
        assert t.status == TaskStatus.completed

    # Verify task-4 started after at least one of tasks 1-3 ended
    t4_start = next((e for e in timeline if e["task"] == "task-4" and e["event"] == "start"), None)
    first_end = min(
        (e for e in timeline if e["event"] == "end" and e["task"] != "task-4"),
        key=lambda e: e["time"],
        default=None,
    )

    assert t4_start is not None
    assert first_end is not None
    assert t4_start["time"] >= first_end["time"]


async def test_priority_ordering():
    """Verify get_queued_by_priority returns high before low."""
    # This tests the DB ordering which the pool relies on for drain order.
    # The pool's drain_queue() picks from get_queued_by_priority().
    await create_task(
        task_id="t-low", repo=".", prompt="L", model="m", max_turns=5, priority=TaskPriority.low
    )
    await create_task(
        task_id="t-high", repo=".", prompt="H", model="m", max_turns=5, priority=TaskPriority.high
    )
    await create_task(
        task_id="t-norm", repo=".", prompt="N", model="m", max_turns=5, priority=TaskPriority.normal
    )

    queued = await get_queued_by_priority()
    assert queued[0].id == "t-high"
    assert queued[1].id == "t-norm"
    assert queued[2].id == "t-low"


async def test_cancelled_task_not_executed():
    """A task cancelled while queued should not be executed when a slot opens."""
    executed: list[str] = []

    async def mock_runner(task):
        executed.append(task.id)
        await update_task(
            task.id,
            status=TaskStatus.setup,
            started_at="now",
            container_id=f"c-{task.id}",
        )
        await update_task(task.id, status=TaskStatus.running)
        await asyncio.sleep(0.1)
        await update_task(
            task.id,
            status=TaskStatus.completed,
            completed_at="now",
        )
        return {"success": True}

    set_runner_fn(mock_runner)

    import sandclaude.config as cfg
    import sandclaude.runner.pool as pool_mod

    cfg.settings.max_concurrent = 1  # Only 1 slot
    pool_mod._semaphore = None

    # Submit task-1 (will run immediately, occupying the slot)
    t1 = await create_task(task_id="task-1", repo=".", prompt="T1", model="m", max_turns=5)
    await submit_task(t1)

    # Submit task-2 (will wait in queue for the slot)
    t2 = await create_task(task_id="task-2", repo=".", prompt="T2", model="m", max_turns=5)
    await submit_task(t2)

    # Cancel task-2 before it gets a slot
    await update_task("task-2", status=TaskStatus.cancelled, completed_at="now")

    # Wait for everything to finish
    await asyncio.sleep(1.0)

    # task-1 should have run, task-2 should NOT have run
    assert "task-1" in executed
    assert "task-2" not in executed

    t2_final = await get_task("task-2")
    assert t2_final is not None
    assert t2_final.status == TaskStatus.cancelled


# ── v0.4.0: Scheduler protocol tests ──────────────────────────────


def test_local_scheduler_implements_protocol():
    """LocalScheduler must satisfy the Scheduler protocol."""
    assert isinstance(LocalScheduler(), Scheduler)


def test_get_scheduler_returns_scheduler():
    """get_scheduler() should return a Scheduler-compatible instance."""
    scheduler = get_scheduler()
    assert isinstance(scheduler, Scheduler)


async def test_scheduler_stats():
    """Scheduler.stats() should return pool metrics."""
    scheduler = get_scheduler()
    stats = await scheduler.stats()
    assert "max_concurrent" in stats
    assert "active" in stats
    assert "queued" in stats
    assert stats["max_concurrent"] >= 1
