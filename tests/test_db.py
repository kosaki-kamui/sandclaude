"""Tests for database module."""

import pytest

from sandclaude.db.store import (
    cleanup_old_tasks,
    count_active,
    create_task,
    delete_task,
    get_orphaned,
    get_queued_by_priority,
    get_task,
    init_db,
    list_tasks,
    update_task,
)
from sandclaude.models import TaskPriority, TaskStatus


@pytest.fixture(autouse=True)
async def _init(tmp_path):
    import sandclaude.config as cfg
    import sandclaude.db.store as store

    cfg.settings.data_dir = tmp_path
    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()


async def test_create_and_get():
    task = await create_task(
        task_id="task-abc",
        repo="https://github.com/test/repo",
        prompt="Fix bugs",
        model="claude-sonnet-4-5",
        max_turns=10,
    )
    assert task.id == "task-abc"
    assert task.status == TaskStatus.queued
    assert task.repo == "https://github.com/test/repo"

    retrieved = await get_task("task-abc")
    assert retrieved is not None
    assert retrieved.id == task.id


async def test_list_tasks():
    await create_task(task_id="t1", repo=".", prompt="A", model="m", max_turns=5)
    await create_task(task_id="t2", repo=".", prompt="B", model="m", max_turns=5)

    tasks = await list_tasks()
    assert len(tasks) == 2
    ids = sorted(t.id for t in tasks)
    assert ids == ["t1", "t2"]


async def test_update_status():
    await create_task(task_id="t-up", repo=".", prompt="X", model="m", max_turns=5)
    await update_task("t-up", status=TaskStatus.running, container_id="cid-123")

    task = await get_task("t-up")
    assert task is not None
    assert task.status == TaskStatus.running
    assert task.container_id == "cid-123"


async def test_update_completion():
    await create_task(task_id="t-done", repo=".", prompt="Y", model="m", max_turns=5)
    await update_task(
        "t-done",
        status=TaskStatus.completed,
        completed_at="2026-01-01T01:00:00Z",
        tokens_input=5000,
        tokens_output=1500,
        total_cost_usd=0.42,
    )
    task = await get_task("t-done")
    assert task is not None
    assert task.status == TaskStatus.completed
    assert task.tokens_input == 5000
    assert abs((task.total_cost_usd or 0) - 0.42) < 0.001


async def test_priority_queue():
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
    assert len(queued) == 3
    assert queued[0].id == "t-high"
    assert queued[1].id == "t-norm"
    assert queued[2].id == "t-low"


async def test_count_active():
    await create_task(task_id="t-a1", repo=".", prompt="1", model="m", max_turns=5)
    await create_task(task_id="t-a2", repo=".", prompt="2", model="m", max_turns=5)

    assert await count_active() == 0

    await update_task("t-a1", status=TaskStatus.setup)
    assert await count_active() == 1

    await update_task("t-a2", status=TaskStatus.running)
    assert await count_active() == 2

    await update_task("t-a1", status=TaskStatus.completed, completed_at="now")
    assert await count_active() == 1


async def test_orphan_detection():
    await create_task(task_id="t-o1", repo=".", prompt="O1", model="m", max_turns=5)
    await update_task("t-o1", status=TaskStatus.running, container_id="dead-1")

    await create_task(task_id="t-o2", repo=".", prompt="O2", model="m", max_turns=5)
    # stays queued - not an orphan

    orphans = await get_orphaned()
    assert len(orphans) == 1
    assert orphans[0].id == "t-o1"


async def test_notify_config():
    await create_task(
        task_id="t-notify",
        repo=".",
        prompt="test",
        model="m",
        max_turns=5,
        notify_webhook="https://hooks.slack.com/test",
        notify_on=["completed", "failed"],
    )
    task = await get_task("t-notify")
    assert task is not None
    assert task.notify_webhook == "https://hooks.slack.com/test"
    assert task.notify_on == '["completed", "failed"]'


async def test_nonexistent_task():
    assert await get_task("nonexistent") is None


async def test_delete_task(tmp_path):
    await create_task(task_id="t-del", repo=".", prompt="delete me", model="m", max_turns=5)
    # Create fake output files
    task_dir = tmp_path / "tasks" / "t-del"
    task_dir.mkdir(parents=True)
    (task_dir / "diff.patch").write_text("fake diff")
    (task_dir / "audit.json").write_text("{}")

    assert await delete_task("t-del")
    assert await get_task("t-del") is None
    assert not task_dir.exists()


async def test_delete_nonexistent():
    assert not await delete_task("nonexistent")


async def test_delete_task_path_boundary(tmp_path):
    """Verify delete_task won't remove dirs outside the tasks/ base."""
    await create_task(task_id="t-boundary", repo=".", prompt="boundary", model="m", max_turns=5)

    # Create a directory outside the tasks/ tree with the same name
    outside_dir = tmp_path / "other" / "t-boundary"
    outside_dir.mkdir(parents=True)
    (outside_dir / "important.txt").write_text("do not delete")

    # The real task dir should be cleaned up
    task_dir = tmp_path / "tasks" / "t-boundary"
    task_dir.mkdir(parents=True)
    (task_dir / "diff.patch").write_text("fake")

    assert await delete_task("t-boundary")
    assert not task_dir.exists()
    # The outside directory must be untouched
    assert outside_dir.exists()
    assert (outside_dir / "important.txt").read_text() == "do not delete"


async def test_cleanup_old_tasks(tmp_path):
    from datetime import datetime, timedelta, timezone

    import aiosqlite

    import sandclaude.db.store as store

    await create_task(task_id="t-old", repo=".", prompt="old", model="m", max_turns=5)
    await update_task("t-old", status=TaskStatus.completed, completed_at="now")
    # Backdate the created_at to 60 days ago
    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    async with aiosqlite.connect(store.DB_PATH) as db:
        await db.execute("UPDATE tasks SET created_at = ? WHERE id = 't-old'", (old_date,))
        await db.commit()

    await create_task(task_id="t-new", repo=".", prompt="new", model="m", max_turns=5)
    await update_task("t-new", status=TaskStatus.completed, completed_at="now")

    count = await cleanup_old_tasks(30)
    assert count == 1
    assert await get_task("t-old") is None
    assert await get_task("t-new") is not None
