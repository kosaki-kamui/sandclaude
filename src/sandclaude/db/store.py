"""
SQLite persistence via aiosqlite.
Non-blocking, compatible with FastAPI's event loop.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from sandclaude.config import settings
from sandclaude.models import Task, TaskPriority, TaskStatus

# F2: DB_PATH as a function so it always reflects current settings.data_dir.
# Can be overridden in tests by setting DB_PATH directly.
DB_PATH: Path | None = None


def _db_path() -> Path:
    """Return the current DB path, respecting test overrides."""
    if DB_PATH is not None:
        return DB_PATH
    return settings.data_dir / "tasks.db"


async def init_db() -> None:
    """Create the tasks table if it doesn't exist."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        # Enable WAL mode for concurrent read/write as documented in DESIGN_DECISIONS.md
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                repo TEXT NOT NULL,
                branch TEXT,
                prompt TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'claude-sonnet-4-5',
                max_turns INTEGER NOT NULL DEFAULT 50,
                priority TEXT NOT NULL DEFAULT 'normal',
                owner_token_hash TEXT,
                container_id TEXT,
                host_cwd TEXT,
                allowed_domains TEXT,
                notify_webhook TEXT,
                notify_on TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                tokens_input INTEGER,
                tokens_output INTEGER,
                total_cost_usd REAL,
                error TEXT
            )
        """)

        # Migration for existing DBs — add any missing columns.
        cursor = await db.execute("PRAGMA table_info(tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        migrations = {
            "owner_token_hash": "TEXT",
            "host_cwd": "TEXT",
            "allowed_domains": "TEXT",
            "notify_webhook": "TEXT",
            "notify_on": "TEXT",
        }
        for col_name, col_type in migrations.items():
            if col_name not in cols:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
        await db.commit()


async def create_task(
    *,
    task_id: str,
    repo: str,
    prompt: str,
    branch: str | None = None,
    model: str = "claude-sonnet-4-5",
    max_turns: int = 50,
    priority: TaskPriority = TaskPriority.normal,
    owner_token_hash: str | None = None,
    host_cwd: str | None = None,
    allowed_domains: list[str] | None = None,
    notify_webhook: str | None = None,
    notify_on: list[str] | None = None,
) -> Task:
    now = datetime.now(timezone.utc).isoformat()
    allowed_domains_json = json.dumps(allowed_domains) if allowed_domains else None
    notify_on_json = json.dumps(notify_on) if notify_on else None
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO tasks
               (id, status, repo, branch, prompt, model, max_turns, priority,
                owner_token_hash, host_cwd, allowed_domains, notify_webhook, notify_on, created_at)
               VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                repo,
                branch,
                prompt,
                model,
                max_turns,
                priority.value,
                owner_token_hash,
                host_cwd,
                allowed_domains_json,
                notify_webhook,
                notify_on_json,
                now,
            ),
        )
        await db.commit()
    # F6: Explicitly handle the case where get_task returns None
    task = await get_task(task_id)
    if task is None:
        raise RuntimeError(f"Failed to read back task {task_id} after creation")
    return task


async def get_task(task_id: str) -> Task | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        return _row_to_task(row) if row else None


async def list_tasks() -> list[Task]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [_row_to_task(r) for r in rows]


async def list_tasks_for_owner(
    owner_token_hash: str, *, include_unowned: bool = False
) -> list[Task]:
    """List tasks owned by this token hash.

    If include_unowned is True, also returns legacy tasks with NULL owner_token_hash.
    This should only be True for the primary server token.
    """
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        if include_unowned:
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE owner_token_hash = ? OR owner_token_hash IS NULL "
                "ORDER BY created_at DESC",
                (owner_token_hash,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE owner_token_hash = ? ORDER BY created_at DESC",
                (owner_token_hash,),
            )
        rows = await cursor.fetchall()
        return [_row_to_task(r) for r in rows]


async def update_task(
    task_id: str,
    *,
    status: TaskStatus | None = None,
    container_id: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    total_cost_usd: float | None = None,
    error: str | None = None,
) -> None:
    sets: list[str] = []
    vals: list[object] = []

    if status is not None:
        sets.append("status = ?")
        vals.append(status.value)
    if container_id is not None:
        sets.append("container_id = ?")
        vals.append(container_id)
    if started_at is not None:
        sets.append("started_at = ?")
        vals.append(started_at)
    if completed_at is not None:
        sets.append("completed_at = ?")
        vals.append(completed_at)
    if tokens_input is not None:
        sets.append("tokens_input = ?")
        vals.append(tokens_input)
    if tokens_output is not None:
        sets.append("tokens_output = ?")
        vals.append(tokens_output)
    if total_cost_usd is not None:
        sets.append("total_cost_usd = ?")
        vals.append(total_cost_usd)
    if error is not None:
        sets.append("error = ?")
        vals.append(error)

    if not sets:
        return

    vals.append(task_id)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
        await db.commit()


async def update_task_if_status(
    task_id: str,
    *,
    expected_statuses: list[TaskStatus],
    status: TaskStatus,
    completed_at: str | None = None,
    error: str | None = None,
) -> bool:
    """Conditionally update a task only if its current status matches one of expected_statuses.

    Returns True if the update was applied (row matched), False otherwise.
    This prevents race conditions where a concurrent status change (e.g., task
    completing while being cancelled) would be clobbered.
    """
    sets = ["status = ?"]
    vals: list[object] = [status.value]
    if completed_at is not None:
        sets.append("completed_at = ?")
        vals.append(completed_at)
    if error is not None:
        sets.append("error = ?")
        vals.append(error)

    expected_values = [s.value for s in expected_statuses]
    placeholders = ",".join("?" for _ in expected_values)
    vals.extend(expected_values)
    vals.append(task_id)

    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE status IN ({placeholders}) AND id = ?",
            vals,
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_queued_by_priority() -> list[Task]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM tasks WHERE status = 'queued'
            ORDER BY
                CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 WHEN 'low' THEN 2 END,
                created_at ASC
        """)
        rows = await cursor.fetchall()
        return [_row_to_task(r) for r in rows]


async def count_active() -> int:
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('setup', 'running')")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_orphaned() -> list[Task]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tasks WHERE status IN ('running', 'setup')")
        rows = await cursor.fetchall()
        return [_row_to_task(r) for r in rows]


async def delete_task(task_id: str) -> bool:
    """Delete a task and its output files. Returns True if task existed."""
    task = await get_task(task_id)
    if not task:
        return False
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
    # Remove output directory with defensive path check
    tasks_base = (settings.data_dir / "tasks").resolve()
    task_dir = (settings.data_dir / "tasks" / task_id).resolve()
    if task_dir != tasks_base and task_dir.is_relative_to(tasks_base) and task_dir.exists():
        shutil.rmtree(task_dir)
    return True


async def cleanup_old_tasks(retention_days: int) -> int:
    """Delete terminal tasks older than retention_days. Returns count deleted."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM tasks "
            "WHERE status IN ('completed', 'failed', 'cancelled') AND created_at < ?",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        task_ids = [row["id"] for row in rows]

    if not task_ids:
        return 0

    # Batch DB delete in a single connection
    async with aiosqlite.connect(_db_path()) as db:
        placeholders = ",".join("?" for _ in task_ids)
        await db.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
        await db.commit()

    # Clean up output directories
    tasks_base = (settings.data_dir / "tasks").resolve()
    count = 0
    for tid in task_ids:
        task_dir = (settings.data_dir / "tasks" / tid).resolve()
        if task_dir != tasks_base and task_dir.is_relative_to(tasks_base) and task_dir.exists():
            shutil.rmtree(task_dir)
        count += 1
    return count


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        status=TaskStatus(row["status"]),
        repo=row["repo"],
        branch=row["branch"],
        prompt=row["prompt"],
        model=row["model"],
        max_turns=row["max_turns"],
        priority=TaskPriority(row["priority"]),
        owner_token_hash=row["owner_token_hash"],
        container_id=row["container_id"],
        host_cwd=row["host_cwd"],
        allowed_domains=row["allowed_domains"],
        notify_webhook=row["notify_webhook"],
        notify_on=row["notify_on"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        tokens_input=row["tokens_input"],
        tokens_output=row["tokens_output"],
        total_cost_usd=row["total_cost_usd"],
        error=row["error"],
    )
