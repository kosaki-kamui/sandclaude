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
from sandclaude.models import (
    ApprovalGate,
    ApprovalStatus,
    PolicyPreset,
    Task,
    TaskPriority,
    TaskStatus,
    TokenInfo,
    User,
)

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
            # v0.2.0 columns
            "policy_preset": "TEXT",
            "requires_approval": "INTEGER NOT NULL DEFAULT 0",
            "declared_secrets": "TEXT",
            "cost_budget_usd": "REAL",
            # v0.2.5 columns
            "budget_check_json": "TEXT",
        }
        for col_name, col_type in migrations.items():
            if col_name not in cols:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")

        # v0.2.0: Approval gates table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS approval_gates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                decided_by TEXT,
                decided_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, action)
            )
        """)

        # v0.2.0: Token registry
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                scopes TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                created_by TEXT
            )
        """)

        # v0.3.0: Users
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                email TEXT,
                github_username TEXT,
                is_service_account INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                created_by_user_id INTEGER REFERENCES users(id)
            )
        """)

        # v0.3.0 migrations: add user_id columns
        cursor = await db.execute("PRAGMA table_info(tokens)")
        token_cols = {row[1] for row in await cursor.fetchall()}
        if "user_id" not in token_cols:
            await db.execute("ALTER TABLE tokens ADD COLUMN user_id INTEGER REFERENCES users(id)")

        cursor = await db.execute("PRAGMA table_info(tasks)")
        task_cols_v3 = {row[1] for row in await cursor.fetchall()}
        if "created_by_user_id" not in task_cols_v3:
            await db.execute("ALTER TABLE tasks ADD COLUMN created_by_user_id INTEGER")

        cursor = await db.execute("PRAGMA table_info(approval_gates)")
        gate_cols = {row[1] for row in await cursor.fetchall()}
        if "decided_by_user_id" not in gate_cols:
            await db.execute("ALTER TABLE approval_gates ADD COLUMN decided_by_user_id INTEGER")
        if "expires_at" not in gate_cols:
            await db.execute("ALTER TABLE approval_gates ADD COLUMN expires_at TEXT")

        # v0.3.0 observability migrations
        for obs_col, obs_type in [
            ("setup_completed_at", "TEXT"),
            ("agent_started_at", "TEXT"),
            ("error_category", "TEXT"),
            ("parent_task_id", "TEXT"),
            ("labels", "TEXT"),
            ("cancel_reason", "TEXT"),
            # v0.4.0 columns
            ("completion_reason", "TEXT"),
            ("review_required", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if obs_col not in task_cols_v3:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {obs_col} {obs_type}")

        # v0.2.0: Policy presets
        await db.execute("""
            CREATE TABLE IF NOT EXISTS policy_presets (
                name TEXT PRIMARY KEY,
                config TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)

        # v0.2.0: Task secrets audit
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_secrets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                secret_name TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'setup',
                granted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(task_id, secret_name)
            )
        """)

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
    policy_preset: str | None = None,
    declared_secrets: list[str] | None = None,
    cost_budget_usd: float | None = None,
    created_by_user_id: int | None = None,
    parent_task_id: str | None = None,
    labels: list[str] | None = None,
) -> Task:
    now = datetime.now(timezone.utc).isoformat()
    allowed_domains_json = json.dumps(allowed_domains) if allowed_domains else None
    notify_on_json = json.dumps(notify_on) if notify_on else None
    declared_secrets_json = json.dumps(declared_secrets) if declared_secrets else None
    labels_json = json.dumps(labels) if labels else None
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """INSERT INTO tasks
               (id, status, repo, branch, prompt, model, max_turns, priority,
                owner_token_hash, host_cwd, allowed_domains, notify_webhook, notify_on,
                policy_preset, declared_secrets, cost_budget_usd, created_by_user_id,
                parent_task_id, labels, created_at)
               VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                policy_preset,
                declared_secrets_json,
                cost_budget_usd,
                created_by_user_id,
                parent_task_id,
                labels_json,
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


async def list_tasks_for_user(user_id: int) -> list[Task]:
    """List tasks created by a specific user (for session auth)."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE created_by_user_id = ? ORDER BY created_at DESC",
            (user_id,),
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
    requires_approval: int | None = None,
    budget_check_json: str | None = None,
    setup_completed_at: str | None = None,
    agent_started_at: str | None = None,
    error_category: str | None = None,
    cancel_reason: str | None = None,
    completion_reason: str | None = None,
    review_required: int | None = None,
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
    if requires_approval is not None:
        sets.append("requires_approval = ?")
        vals.append(requires_approval)
    if budget_check_json is not None:
        sets.append("budget_check_json = ?")
        vals.append(budget_check_json)
    if setup_completed_at is not None:
        sets.append("setup_completed_at = ?")
        vals.append(setup_completed_at)
    if agent_started_at is not None:
        sets.append("agent_started_at = ?")
        vals.append(agent_started_at)
    if error_category is not None:
        sets.append("error_category = ?")
        vals.append(error_category)
    if cancel_reason is not None:
        sets.append("cancel_reason = ?")
        vals.append(cancel_reason)
    if completion_reason is not None:
        sets.append("completion_reason = ?")
        vals.append(completion_reason)
    if review_required is not None:
        sets.append("review_required = ?")
        vals.append(review_required)

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
    error_category: str | None = None,
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
    if error_category is not None:
        sets.append("error_category = ?")
        vals.append(error_category)

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
    keys = row.keys() if hasattr(row, "keys") else []
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
        # v0.2.0 fields (default for migrated DBs missing these columns)
        policy_preset=row["policy_preset"] if "policy_preset" in keys else None,
        requires_approval=row["requires_approval"] if "requires_approval" in keys else 0,
        declared_secrets=row["declared_secrets"] if "declared_secrets" in keys else None,
        cost_budget_usd=row["cost_budget_usd"] if "cost_budget_usd" in keys else None,
        budget_check_json=row["budget_check_json"] if "budget_check_json" in keys else None,
        created_by_user_id=row["created_by_user_id"] if "created_by_user_id" in keys else None,
        setup_completed_at=row["setup_completed_at"] if "setup_completed_at" in keys else None,
        agent_started_at=row["agent_started_at"] if "agent_started_at" in keys else None,
        error_category=row["error_category"] if "error_category" in keys else None,
        parent_task_id=row["parent_task_id"] if "parent_task_id" in keys else None,
        labels=row["labels"] if "labels" in keys else None,
        cancel_reason=row["cancel_reason"] if "cancel_reason" in keys else None,
        completion_reason=row["completion_reason"] if "completion_reason" in keys else None,
        review_required=row["review_required"] if "review_required" in keys else 0,
    )


# ---------------------------------------------------------------------------
# v0.2.0: Approval gates CRUD
# ---------------------------------------------------------------------------


async def create_approval_gate(task_id: str, action: str) -> ApprovalGate:
    from sandclaude.config import settings as _settings

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expires_at: str | None = None
    if _settings.approval_expiry_s > 0:
        from datetime import timedelta

        expires_at = (now + timedelta(seconds=_settings.approval_expiry_s)).isoformat()

    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            "INSERT INTO approval_gates (task_id, action, status, created_at, expires_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (task_id, action, now_iso, expires_at),
        )
        await db.commit()
        gate_id = cursor.lastrowid
    return ApprovalGate(
        id=gate_id or 0,
        task_id=task_id,
        action=action,
        status=ApprovalStatus.pending,
        created_at=now_iso,
        expires_at=expires_at,
    )


async def get_approval_gates(task_id: str) -> list[ApprovalGate]:
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        # Auto-expire pending gates whose expires_at has passed
        await db.execute(
            "UPDATE approval_gates SET status = 'rejected', reason = 'expired' "
            "WHERE task_id = ? AND status = 'pending' AND expires_at IS NOT NULL "
            "AND expires_at < ?",
            (task_id, now_iso),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM approval_gates WHERE task_id = ? ORDER BY created_at", (task_id,)
        )
        rows = await cursor.fetchall()
    return [
        ApprovalGate(
            id=r["id"],
            task_id=r["task_id"],
            action=r["action"],
            status=ApprovalStatus(r["status"]),
            reason=r["reason"],
            decided_by=r["decided_by"],
            decided_at=r["decided_at"],
            created_at=r["created_at"],
            expires_at=r["expires_at"],
        )
        for r in rows
    ]


async def decide_approval_gate(
    task_id: str,
    action: str,
    *,
    decision: ApprovalStatus,
    decided_by: str,
    reason: str | None = None,
    decided_by_user_id: int | None = None,
) -> bool:
    """Approve or reject a gate. Returns True if a pending gate was found and updated."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_db_path()) as conn:
        cursor = await conn.execute(
            "UPDATE approval_gates SET status = ?, reason = ?, decided_by = ?, "
            "decided_at = ?, decided_by_user_id = ? "
            "WHERE task_id = ? AND action = ? AND status = 'pending'",
            (decision.value, reason, decided_by, now, decided_by_user_id, task_id, action),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def has_pending_gates(task_id: str) -> bool:
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM approval_gates WHERE task_id = ? AND status = 'pending' "
            "AND (expires_at IS NULL OR expires_at >= ?)",
            (task_id, now_iso),
        )
        row = await cursor.fetchone()
        return (row[0] if row else 0) > 0


# ---------------------------------------------------------------------------
# v0.2.0: Token registry CRUD
# ---------------------------------------------------------------------------


async def create_token(
    *,
    name: str,
    token_hash: str,
    scopes: list[str],
    expires_at: str | None = None,
    created_by: str | None = None,
    user_id: int | None = None,
) -> TokenInfo:
    now = datetime.now(timezone.utc).isoformat()
    scopes_json = json.dumps(scopes)
    async with aiosqlite.connect(_db_path()) as conn:
        cursor = await conn.execute(
            "INSERT INTO tokens "
            "(name, token_hash, scopes, created_at, expires_at, created_by, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, token_hash, scopes_json, now, expires_at, created_by, user_id),
        )
        await conn.commit()
        token_id = cursor.lastrowid
    return TokenInfo(
        id=token_id or 0,
        name=name,
        token_hash=token_hash,
        scopes=scopes,
        created_at=now,
        expires_at=expires_at,
        created_by=created_by,
        user_id=user_id,
    )


async def get_token_by_hash(token_hash: str) -> TokenInfo | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tokens WHERE token_hash = ?", (token_hash,))
        row = await cursor.fetchone()
        if not row:
            return None
    return _row_to_token(row)


async def list_tokens() -> list[TokenInfo]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
        rows = await cursor.fetchall()
    return [_row_to_token(r) for r in rows]


async def revoke_token(token_id: int) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            "UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now, token_id),
        )
        await db.commit()
        return cursor.rowcount > 0


def _row_to_token(row: aiosqlite.Row) -> TokenInfo:
    scopes = json.loads(row["scopes"]) if row["scopes"] else []
    keys = row.keys() if hasattr(row, "keys") else []
    return TokenInfo(
        id=row["id"],
        name=row["name"],
        token_hash=row["token_hash"],
        scopes=scopes,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_by=row["created_by"],
        user_id=row["user_id"] if "user_id" in keys else None,
    )


# ---------------------------------------------------------------------------
# v0.2.0: Policy presets CRUD
# ---------------------------------------------------------------------------


async def save_policy_preset(name: str, config: dict) -> PolicyPreset:
    now = datetime.now(timezone.utc).isoformat()
    config_json = json.dumps(config)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO policy_presets (name, config, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET config = excluded.config, "
            "updated_at = excluded.updated_at",
            (name, config_json, now, now),
        )
        await db.commit()
    return PolicyPreset(name=name, config=config, created_at=now, updated_at=now)


async def get_policy_preset(name: str) -> PolicyPreset | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM policy_presets WHERE name = ?", (name,))
        row = await cursor.fetchone()
        if not row:
            return None
    return PolicyPreset(
        name=row["name"],
        config=json.loads(row["config"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def list_policy_presets() -> list[PolicyPreset]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM policy_presets ORDER BY name")
        rows = await cursor.fetchall()
    return [
        PolicyPreset(
            name=r["name"],
            config=json.loads(r["config"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def delete_policy_preset(name: str) -> bool:
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute("DELETE FROM policy_presets WHERE name = ?", (name,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# v0.2.0: Task secrets audit CRUD
# ---------------------------------------------------------------------------


async def record_task_secret(task_id: str, secret_name: str, phase: str, granted: bool) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT OR REPLACE INTO task_secrets (task_id, secret_name, phase, granted) "
            "VALUES (?, ?, ?, ?)",
            (task_id, secret_name, phase, 1 if granted else 0),
        )
        await db.commit()


async def get_task_secrets(task_id: str) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM task_secrets WHERE task_id = ? ORDER BY secret_name", (task_id,)
        )
        rows = await cursor.fetchall()
    return [
        {"secret_name": r["secret_name"], "phase": r["phase"], "granted": bool(r["granted"])}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# v0.3.0: Users CRUD
# ---------------------------------------------------------------------------


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        email=row["email"],
        github_username=row["github_username"],
        is_service_account=row["is_service_account"],
        created_at=row["created_at"],
        created_by_user_id=row["created_by_user_id"],
    )


async def create_user(
    *,
    username: str,
    display_name: str,
    email: str | None = None,
    github_username: str | None = None,
    is_service_account: bool = False,
    created_by_user_id: int | None = None,
) -> User:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_db_path()) as conn:
        cursor = await conn.execute(
            """INSERT INTO users
               (username, display_name, email, github_username,
                is_service_account, created_at, created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                username,
                display_name,
                email,
                github_username,
                int(is_service_account),
                now,
                created_by_user_id,
            ),
        )
        await conn.commit()
        user_id = cursor.lastrowid
    assert user_id is not None
    user = await get_user(user_id)
    if user is None:
        raise RuntimeError(f"Failed to read back user {user_id} after creation")
    return user


async def get_user(user_id: int) -> User | None:
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None


async def get_user_by_username(username: str) -> User | None:
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None


async def get_user_by_github_username(github_username: str) -> User | None:
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM users WHERE github_username = ?", (github_username,)
        )
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None


async def list_users() -> list[User]:
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM users ORDER BY created_at")
        rows = await cursor.fetchall()
        return [_row_to_user(r) for r in rows]


async def delete_user(user_id: int) -> bool:
    async with aiosqlite.connect(_db_path()) as conn:
        cursor = await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def update_user(user_id: int, **kwargs: object) -> None:
    sets: list[str] = []
    vals: list[object] = []
    for col in ("display_name", "email", "github_username", "created_by_user_id"):
        if col in kwargs:
            sets.append(f"{col} = ?")
            vals.append(kwargs[col])
    if not sets:
        return
    vals.append(user_id)
    async with aiosqlite.connect(_db_path()) as conn:
        await conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", vals)
        await conn.commit()


async def link_orphan_tokens_to_user(user_id: int) -> int:
    """Link tokens with NULL user_id to the given user. Returns count."""
    async with aiosqlite.connect(_db_path()) as conn:
        cursor = await conn.execute(
            "UPDATE tokens SET user_id = ? WHERE user_id IS NULL", (user_id,)
        )
        await conn.commit()
        return cursor.rowcount


async def backfill_task_user_ids(admin_user_id: int) -> int:
    """Backfill tasks missing created_by_user_id with the admin user.

    Pre-upgrade tasks have owner_token_hash but no created_by_user_id.
    This assigns them to the admin so session-based ownership works.
    """
    async with aiosqlite.connect(_db_path()) as conn:
        cursor = await conn.execute(
            "UPDATE tasks SET created_by_user_id = ? WHERE created_by_user_id IS NULL",
            (admin_user_id,),
        )
        await conn.commit()
        return cursor.rowcount


# ---------------------------------------------------------------------------
# v0.3.0: Metrics & observability queries
# ---------------------------------------------------------------------------


async def get_task_metrics() -> dict:
    """Aggregate task metrics for the /metrics endpoint."""
    async with aiosqlite.connect(_db_path()) as conn:
        # Status counts
        cursor = await conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        by_status = {row[0]: row[1] for row in await cursor.fetchall()}
        total = sum(by_status.values())

        # Error category counts
        cursor = await conn.execute(
            "SELECT error_category, COUNT(*) FROM tasks "
            "WHERE error_category IS NOT NULL GROUP BY error_category"
        )
        by_error = {row[0]: row[1] for row in await cursor.fetchall()}

        # Cost and token aggregates
        cursor = await conn.execute(
            "SELECT SUM(total_cost_usd), SUM(tokens_input), SUM(tokens_output) FROM tasks"
        )
        row = await cursor.fetchone()
        total_cost = (row[0] or 0.0) if row else 0.0
        total_input = (row[1] or 0) if row else 0
        total_output = (row[2] or 0) if row else 0

        # Timing averages (only for completed tasks with both timestamps)
        cursor = await conn.execute(
            "SELECT AVG("
            "  CAST((julianday(completed_at) - julianday(created_at)) * 86400 AS INTEGER)"
            ") FROM tasks WHERE completed_at IS NOT NULL AND created_at IS NOT NULL"
        )
        row = await cursor.fetchone()
        avg_total = row[0] if row else None

        cursor = await conn.execute(
            "SELECT AVG("
            "  CAST((julianday(setup_completed_at) - julianday(started_at)) * 86400 AS INTEGER)"
            ") FROM tasks WHERE setup_completed_at IS NOT NULL AND started_at IS NOT NULL"
        )
        row = await cursor.fetchone()
        avg_setup = row[0] if row else None

        cursor = await conn.execute(
            "SELECT AVG("
            "  CAST((julianday(completed_at) - julianday(agent_started_at)) * 86400 AS INTEGER)"
            ") FROM tasks WHERE completed_at IS NOT NULL AND agent_started_at IS NOT NULL"
        )
        row = await cursor.fetchone()
        avg_agent = row[0] if row else None

        # Last 24 hours
        cursor = await conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), "
            "SUM(total_cost_usd) "
            "FROM tasks WHERE created_at >= datetime('now', '-1 day')"
        )
        recent = await cursor.fetchone()
        recent_count = recent[0] if recent else 0
        recent_success = recent[1] if recent else 0
        recent_cost = recent[2] if recent else 0.0

        completed = by_status.get("completed", 0)
        success_rate = round(completed / total, 2) if total > 0 else 0.0

    return {
        "tasks": {
            "total": total,
            "by_status": by_status,
            "by_error_category": by_error,
            "success_rate": success_rate,
        },
        "cost": {
            "total_usd": round(total_cost, 4),
            "avg_per_task_usd": round(total_cost / total, 4) if total > 0 else 0.0,
        },
        "tokens": {
            "total_input": total_input,
            "total_output": total_output,
        },
        "timing": {
            "avg_total_duration_s": round(avg_total) if avg_total else None,
            "avg_setup_duration_s": round(avg_setup) if avg_setup else None,
            "avg_agent_duration_s": round(avg_agent) if avg_agent else None,
        },
        "recent_24h": {
            "task_count": recent_count or 0,
            "success_count": recent_success or 0,
            "total_cost_usd": round(recent_cost or 0.0, 4),
        },
    }


async def get_retry_chain(task_id: str) -> list[Task]:
    """Build the full retry chain containing task_id (oldest first).

    Walks parent_task_id upward to find the root, then walks children
    downward to find all descendants.
    """
    # Walk up to the root
    ancestors: list[Task] = []
    current_id: str | None = task_id
    seen: set[str] = set()

    while current_id and current_id not in seen:
        seen.add(current_id)
        task = await get_task(current_id)
        if not task:
            break
        ancestors.append(task)
        current_id = task.parent_task_id

    ancestors.reverse()  # oldest first
    root_id = ancestors[0].id if ancestors else task_id

    # Walk down from root to find all descendants
    chain: list[Task] = [ancestors[0]] if ancestors else []
    queue = [root_id]
    visited: set[str] = {root_id}

    while queue:
        parent_id = queue.pop(0)
        # Find tasks whose parent_task_id == parent_id
        async with aiosqlite.connect(_db_path()) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at",
                (parent_id,),
            )
            rows = await cursor.fetchall()
        for row in rows:
            child = _row_to_task(row)
            if child.id not in visited:
                visited.add(child.id)
                chain.append(child)
                queue.append(child.id)

    return chain
