"""
sandclaude API Server (FastAPI).

Endpoints:
- POST /tasks              - Submit a new task
- GET  /tasks              - List tasks visible to the caller token
- GET  /tasks/{task_id}    - Get task details + results
- POST /tasks/{task_id}/cancel    - Cancel a running task
- GET  /tasks/{task_id}/diff        - Get the generated diff
- GET  /tasks/{task_id}/audit      - Get the audit log
- GET  /tasks/{task_id}/result     - Get the result summary
- GET  /tasks/{task_id}/transcript - Get the full transcript
- POST /tasks/{task_id}/create-pr  - Create a GitHub PR
- GET  /pool               - Runner pool stats
- GET  /health             - Health check (no auth)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os as _os
import re
import secrets as _secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from sandclaude.auth import (
    generate_token,
    init_token,
    require_scope,
    token_fingerprint,
    verify_token,
    verify_token_with_scopes,
)
from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.github import create_pr
from sandclaude.models import (
    ApprovalDecisionRequest,
    ApprovalStatus,
    CreatePRRequest,
    TaskCreateRequest,
    TaskPriority,
    TaskStatus,
    TokenCreateRequest,
    TokenCreateResponse,
)
from sandclaude.runner.container import cancel_container, recover_orphans
from sandclaude.runner.pool import get_pool_stats, submit_task

logger = logging.getLogger(__name__)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CREATE_RATE_LIMIT_WINDOW_S = 60
CREATE_RATE_LIMIT_MAX_REQUESTS = 20
# F4: Use token fingerprint as key (bounded by number of valid tokens).
# NOTE: Rate limit state is process-local. Running with multiple uvicorn workers
# multiplies the effective limit by the worker count. The default Docker CMD uses
# a single worker. If you need multi-worker, move rate limiting to Redis/DB.
_create_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_RATE_BUCKET_MAX_SIZE = 1000  # Cap total tracked fingerprints


_MAX_ARTIFACT_BYTES = 20_000_000  # 20MB hard cap for artifact file reads


async def _read_file_async(path, max_bytes: int = _MAX_ARTIFACT_BYTES) -> str:
    """F1: Non-blocking file read with size guard."""
    size = await asyncio.to_thread(path.stat)
    if size.st_size > max_bytes:
        raise HTTPException(status_code=413, detail="Artifact too large to serve")
    return await asyncio.to_thread(path.read_text)


async def _read_json_async(path, max_bytes: int = _MAX_ARTIFACT_BYTES) -> dict | list:
    """F1: Non-blocking JSON file read with size guard."""
    size = await asyncio.to_thread(path.stat)
    if size.st_size > max_bytes:
        raise HTTPException(status_code=413, detail="Artifact too large to serve")
    text = await asyncio.to_thread(path.read_text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail="Artifact file is corrupted or partially written",
        )


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: init DB, auth, recover orphans."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required. Set it in .env or environment.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await db.init_db()
    init_token()
    # v0.2.0: Seed built-in policy presets
    from sandclaude.presets import seed_builtin_presets

    seeded = await seed_builtin_presets()
    if seeded:
        logger.info("Seeded %d built-in policy preset(s)", seeded)
    logger.info("Data directory: %s", settings.data_dir.resolve())
    # Block multi-worker startup — pool and rate limit state is process-local.
    # Check both WEB_CONCURRENCY env var and uvicorn --workers CLI arg.
    web_concurrency = _os.environ.get("WEB_CONCURRENCY", "1")
    if web_concurrency != "1":
        raise RuntimeError(
            f"WEB_CONCURRENCY={web_concurrency} is not supported. "
            "sandclaude requires a single worker because pool scheduling, "
            "rate limiting, and background task state are process-local. "
            "Remove WEB_CONCURRENCY or set it to 1."
        )
    import sys as _sys

    argv = _sys.argv
    for i, arg in enumerate(argv):
        # Handle both --workers N and --workers=N forms
        workers_val: str | None = None
        if arg == "--workers" and i + 1 < len(argv):
            workers_val = argv[i + 1]
        elif arg.startswith("--workers="):
            workers_val = arg.split("=", 1)[1]
        if workers_val is not None and workers_val != "1":
            raise RuntimeError(
                f"--workers {workers_val} is not supported. "
                "sandclaude requires a single worker. Remove --workers or set it to 1."
            )
    try:
        await recover_orphans()
    except Exception as exc:
        logger.warning("Orphan recovery failed: %s", exc)
    if settings.task_retention_days > 0:
        count = await db.cleanup_old_tasks(settings.task_retention_days)
        if count:
            days = settings.task_retention_days
            logger.info("Cleaned up %d tasks older than %dd", count, days)
    yield


app = FastAPI(title="sandclaude", version="0.2.0", lifespan=lifespan)
security = HTTPBearer(auto_error=False)


def _sanitize_error(exc: Exception) -> str:
    """S9: Sanitize error messages to avoid leaking internal paths/config."""
    msg = str(exc)
    # Strip absolute file paths (Unix and Windows-style)
    msg = re.sub(r"(?:/[^\s:\"']+)+/?", "<path>", msg)
    msg = re.sub(r"(?:[A-Za-z]:\\[^\s:\"']+)+\\?", "<path>", msg)
    # Truncate
    if len(msg) > 500:
        msg = msg[:500] + "..."
    return msg


# ── Auth dependency ────────────────────────────────────────────


async def _require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    # v0.2.0: Accept both legacy tokens and registry tokens
    await verify_token_with_scopes(credentials.credentials)
    return credentials.credentials


def _validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.match(task_id) or "/" in task_id or "\\" in task_id or ".." in task_id:
        raise HTTPException(status_code=400, detail="Invalid task_id format")


def _check_create_rate_limit(token: str) -> None:
    # F4: Use fingerprint as key so bucket count is bounded by valid tokens
    fp = token_fingerprint(token)
    now = time.monotonic()

    # Evict oldest entries if too many fingerprints tracked (instead of clearing all)
    if len(_create_rate_buckets) > _RATE_BUCKET_MAX_SIZE:
        cutoff = now - CREATE_RATE_LIMIT_WINDOW_S
        stale = [k for k, v in _create_rate_buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _create_rate_buckets[k]

    bucket = _create_rate_buckets[fp]
    cutoff = now - CREATE_RATE_LIMIT_WINDOW_S
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= CREATE_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for task creation")
    bucket.append(now)


def _require_task_owner(task_owner_hash: str | None, token: str) -> None:
    """Verify the caller owns this task.

    Returns 404 (not 403) on ownership mismatch to prevent cross-tenant
    task ID enumeration — an attacker cannot distinguish "task exists but
    not yours" from "task doesn't exist".

    Legacy/migrated tasks with NULL owner_token_hash are NOT open-access —
    they are restricted to the primary server token only.
    """
    caller_fp = token_fingerprint(token)
    if not task_owner_hash:
        # Legacy row: only the primary server token (first candidate) may access.
        from sandclaude.auth import get_token

        primary_fp = token_fingerprint(get_token())
        if caller_fp != primary_fp:
            raise HTTPException(status_code=404, detail="Task not found")
        return
    if task_owner_hash != caller_fp:
        raise HTTPException(status_code=404, detail="Task not found")


# ── Health ─────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.2.0"}


# ── Pool stats ─────────────────────────────────────────────────


@app.get("/pool", dependencies=[Depends(_require_auth)])
async def pool_stats() -> dict:
    return await get_pool_stats()


# ── POST /tasks ────────────────────────────────────────────────


@app.post("/tasks", status_code=201)
async def create_task_endpoint(
    request: TaskCreateRequest, token: str = Depends(_require_auth)
) -> dict:
    _check_create_rate_limit(token)

    # Validate repo: must be ".", an absolute path, or a secure remote URL
    repo = request.repo
    is_remote = repo.startswith(("https://", "git@"))
    is_dot = repo == "."
    is_absolute = repo.startswith("/")
    if repo.startswith("http://"):
        raise HTTPException(
            status_code=400,
            detail="Plaintext http:// Git URLs are not allowed. Use https:// or git@ instead.",
        )
    if not (is_remote or is_dot or is_absolute):
        raise HTTPException(
            status_code=400,
            detail="repo must be '.', an absolute path (e.g., /home/user/project), or a git URL.",
        )

    # v0.2.0: Pre-creation policy checks (repo/branch validation)
    if request.policy_preset:
        from sandclaude.policy import check_branch_allowed, check_repo_allowed

        pre_preset = await db.get_policy_preset(request.policy_preset)
        if pre_preset:
            from sandclaude.models import PolicyPresetConfig

            pre_policy = PolicyPresetConfig(**pre_preset.config)
            repo_err = check_repo_allowed(pre_policy, request.repo)
            if repo_err:
                raise HTTPException(status_code=403, detail=repo_err)
            branch_err = check_branch_allowed(pre_policy, request.branch)
            if branch_err:
                raise HTTPException(status_code=403, detail=branch_err)

    task_id = f"task-{_secrets.token_hex(8)}"
    task = await db.create_task(
        task_id=task_id,
        repo=request.repo,
        branch=request.branch,
        prompt=request.prompt,
        model=request.model or "claude-sonnet-4-5",
        max_turns=request.max_turns or 50,
        priority=request.priority or TaskPriority.normal,
        owner_token_hash=token_fingerprint(token),
        host_cwd=request.host_cwd,
        allowed_domains=request.allowed_domains,
        notify_webhook=request.notify.webhook if request.notify else None,
        notify_on=request.notify.on if request.notify else None,
        policy_preset=request.policy_preset,
        declared_secrets=request.declared_secrets,
        cost_budget_usd=request.cost_budget_usd,
    )

    # Resolve policy and create approval gates if needed
    from sandclaude.policy import create_required_gates, resolve_effective_policy

    policy = await resolve_effective_policy(task)
    await create_required_gates(task_id, policy)

    await submit_task(task)
    return task.safe_dump()  # S11: exclude internal fields


# ── GET /tasks ─────────────────────────────────────────────────


@app.get("/tasks")
async def list_tasks_endpoint(token: str = Depends(_require_auth)) -> list[dict]:
    from sandclaude.auth import get_token

    caller_fp = token_fingerprint(token)
    # Primary token also sees legacy tasks with NULL owner_token_hash
    is_primary = caller_fp == token_fingerprint(get_token())
    tasks = await db.list_tasks_for_owner(caller_fp, include_unowned=is_primary)
    return [t.safe_dump() for t in tasks]  # S11: exclude internal fields


# ── GET /tasks/{task_id} ───────────────────────────────────────


@app.get("/tasks/{task_id}")
async def get_task_endpoint(task_id: str, token: str = Depends(_require_auth)) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    result = task.safe_dump()  # S11: exclude internal fields
    task_dir = settings.data_dir / "tasks" / task.id

    # F1: async file reads
    audit_path = task_dir / "audit.json"
    if audit_path.exists():
        result["audit"] = await _read_json_async(audit_path)

    diff_path = task_dir / "diff.patch"
    if diff_path.exists():
        result["diff"] = await _read_file_async(diff_path)

    return result


# ── GET /tasks/{task_id}/diff ──────────────────────────────────


@app.get("/tasks/{task_id}/diff")
async def get_diff_endpoint(task_id: str, token: str = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    diff_path = settings.data_dir / "tasks" / task.id / "diff.patch"
    if not diff_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Diff not available (task may still be running)",
        )

    content = await _read_file_async(diff_path)  # F1
    return PlainTextResponse(content)


# ── GET /tasks/{task_id}/audit ─────────────────────────────────


@app.get("/tasks/{task_id}/audit")
async def get_audit_endpoint(task_id: str, token: str = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    audit_path = settings.data_dir / "tasks" / task.id / "audit.json"
    if not audit_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Audit log not available (task may still be running)",
        )

    data = await _read_json_async(audit_path)  # F1
    return JSONResponse(data)


# ── GET /tasks/{task_id}/result ────────────────────────────────


@app.get("/tasks/{task_id}/result")
async def get_result_endpoint(task_id: str, token: str = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    result_path = settings.data_dir / "tasks" / task.id / "result.json"
    if not result_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Result not available (task may still be running)",
        )

    data = await _read_json_async(result_path)  # F1
    return JSONResponse(data)


# ── GET /tasks/{task_id}/transcript ────────────────────────────


@app.get("/tasks/{task_id}/transcript")
async def get_transcript_endpoint(task_id: str, token: str = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    transcript_path = settings.data_dir / "tasks" / task.id / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Transcript not available (task may still be running)",
        )

    data = await _read_json_async(transcript_path)  # F1
    return JSONResponse(data)


# ── DELETE /tasks/{task_id} ────────────────────────────────────


@app.delete("/tasks/{task_id}")
async def delete_task_endpoint(task_id: str, token: str = Depends(_require_auth)) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    if task.status in (TaskStatus.setup, TaskStatus.running, TaskStatus.pending_approval):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an active task. Cancel it first.",
        )

    await db.delete_task(task_id)
    return {"deleted": task_id}


# ── POST /tasks/{task_id}/cancel ───────────────────────────────


@app.post("/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str, token: str = Depends(_require_auth)) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    if task.status not in (TaskStatus.queued, TaskStatus.setup, TaskStatus.running):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel task in status: {task.status.value}",
        )

    ok = await cancel_container(task)
    if ok:
        return {"status": "cancelled", "task_id": task.id}
    # Conditional update returned False — task status changed concurrently
    fresh = await db.get_task(task_id)
    current_status = fresh.status.value if fresh else "deleted"
    raise HTTPException(
        status_code=409,
        detail=f"Task is no longer cancellable (current status: {current_status})",
    )


# ── POST /tasks/{task_id}/create-pr ────────────────────────────


@app.post("/tasks/{task_id}/create-pr")
async def create_pr_endpoint(
    task_id: str, body: CreatePRRequest | None = None, token: str = Depends(_require_auth)
) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    if task.status != TaskStatus.completed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot create PR for task in status: {task.status.value}",
        )

    # v0.2.0: Check approval gate for create_pr action
    gates = await db.get_approval_gates(task_id)
    pr_gate = next((g for g in gates if g.action == "create_pr"), None)
    if pr_gate and pr_gate.status == ApprovalStatus.pending:
        raise HTTPException(
            status_code=409,
            detail="PR creation requires approval. Use POST /tasks/{task_id}/approve/create_pr",
        )
    if pr_gate and pr_gate.status == ApprovalStatus.rejected:
        raise HTTPException(
            status_code=403,
            detail="PR creation was rejected for this task.",
        )

    try:
        result = await create_pr(task, title=body.title if body else None)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_sanitize_error(exc))  # S9


@app.post("/tasks/{task_id}/approve-and-create-pr")
async def approve_and_create_pr_endpoint(
    task_id: str,
    body: ApprovalDecisionRequest | None = None,
    token: str = Depends(_require_auth),
) -> dict:
    """Approve the create_pr gate and create the PR in one step.

    Requires tasks:approve scope. If the gate is already approved,
    skips the approval step and creates the PR directly. If rejected,
    returns 403. If no create_pr gate exists, creates the PR directly.
    """
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "tasks:approve")

    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    if task.status != TaskStatus.completed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot create PR for task in status: {task.status.value}",
        )

    # Handle the approval gate
    gates = await db.get_approval_gates(task_id)
    pr_gate = next((g for g in gates if g.action == "create_pr"), None)

    if pr_gate and pr_gate.status == ApprovalStatus.rejected:
        raise HTTPException(
            status_code=403,
            detail="PR creation was rejected for this task.",
        )

    if pr_gate and pr_gate.status == ApprovalStatus.pending:
        await db.decide_approval_gate(
            task_id,
            "create_pr",
            decision=ApprovalStatus.approved,
            decided_by=auth.fingerprint,
            reason=body.reason if body else None,
        )
        if not await db.has_pending_gates(task_id):
            await db.update_task(task_id, requires_approval=0)

    # Create the PR
    try:
        pr_result = await create_pr(task, title=None)
        return {
            "status": "approved",
            "task_id": task_id,
            "pr": pr_result,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_sanitize_error(exc))


# ── v0.2.0: Approval gates ────────────────────────────────────


@app.get("/tasks/{task_id}/approvals")
async def list_approvals_endpoint(task_id: str, token: str = Depends(_require_auth)) -> list[dict]:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)
    gates = await db.get_approval_gates(task_id)
    return [g.model_dump() for g in gates]


@app.post("/tasks/{task_id}/approve/{action}")
async def approve_action_endpoint(
    task_id: str,
    action: str,
    body: ApprovalDecisionRequest | None = None,
    token: str = Depends(_require_auth),
) -> dict:
    # Scope check: approval requires explicit tasks:approve permission
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "tasks:approve")

    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    ok = await db.decide_approval_gate(
        task_id,
        action,
        decision=ApprovalStatus.approved,
        decided_by=auth.fingerprint,
        reason=body.reason if body else None,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No pending approval gate found")

    # Update task if no more pending gates
    if not await db.has_pending_gates(task_id):
        await db.update_task(task_id, requires_approval=0)

    return {"status": "approved", "task_id": task_id, "action": action}


@app.post("/tasks/{task_id}/reject/{action}")
async def reject_action_endpoint(
    task_id: str,
    action: str,
    body: ApprovalDecisionRequest | None = None,
    token: str = Depends(_require_auth),
) -> dict:
    # Scope check: rejection also requires explicit tasks:approve permission
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "tasks:approve")

    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    ok = await db.decide_approval_gate(
        task_id,
        action,
        decision=ApprovalStatus.rejected,
        decided_by=auth.fingerprint,
        reason=body.reason if body else None,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No pending approval gate found")

    return {"status": "rejected", "task_id": task_id, "action": action}


@app.post("/tasks/{task_id}/approval-link/{action}")
async def generate_approval_link_endpoint(
    task_id: str,
    action: str,
    token: str = Depends(_require_auth),
) -> dict:
    """Generate a signed, short-lived approval link for a task action.

    The link can be shared in Slack notifications or webhooks.
    It grants read-only access to the approval page — the user must
    enter their own API token to actually approve or reject.
    """
    from sandclaude.auth import create_approval_link_token

    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    link_token = create_approval_link_token(task_id, action)
    base_url = settings.api_url.rstrip("/")
    url = f"{base_url}/approve/{task_id}/{action}?token={link_token}"

    return {"approval_url": url, "expires_in_seconds": 3600}


# ── v0.2.0: Approval UI (server-rendered) ────────────────────


@app.get("/approve/{task_id}/{action}")
async def approval_ui_page(task_id: str, action: str, token: str = "") -> HTMLResponse:
    """Server-rendered approval page. Linked from Slack/webhook notifications.

    Uses a signed, short-lived approval token (NOT a raw API token).
    The approval link token:
    - Is HMAC-signed with the server's primary key
    - Expires after 1 hour
    - Is scoped to a specific task_id + action pair
    - Cannot be used for general API access
    """
    from sandclaude.auth import verify_approval_link_token

    if not token:
        return HTMLResponse("<h1>401 — Missing or expired approval link</h1>", status_code=401)
    if not verify_approval_link_token(token, task_id, action):
        return HTMLResponse("<h1>401 — Invalid or expired approval link</h1>", status_code=401)

    task = await db.get_task(task_id)
    if not task:
        return HTMLResponse("<h1>404 — Task not found</h1>", status_code=404)

    gates = await db.get_approval_gates(task_id)
    has_pending = any(g.status.value == "pending" for g in gates)

    # Build context
    diff_preview = ""
    risk_level = "low"
    risk_reasons: list[str] = []
    files_changed: list[str] = []

    task_dir = settings.data_dir / "tasks" / task.id
    diff_path = task_dir / "diff.patch"
    if diff_path.exists():
        try:
            raw_diff = diff_path.read_text()[:10000]
            diff_preview = _colorize_diff(raw_diff)
            from sandclaude.risk import generate_risk_summary

            audit_path = task_dir / "audit.json"
            audit: dict = {}
            if audit_path.exists():
                audit = json.loads(audit_path.read_text())
            summary = generate_risk_summary(raw_diff, audit)
            risk_level = summary.risk_level
            risk_reasons = summary.risk_reasons
            files_changed = summary.files_changed
        except Exception as exc:
            logger.warning("Error loading diff/audit for approval UI %s: %s", task_id, exc)
            diff_preview = "(error reading diff or audit data)"

    duration = "?"
    if task.started_at and task.completed_at:
        try:
            from datetime import datetime

            s = datetime.fromisoformat(task.started_at.replace("Z", "+00:00"))
            e = datetime.fromisoformat(task.completed_at.replace("Z", "+00:00"))
            secs = (e - s).total_seconds()
            duration = f"{secs / 60:.1f}m" if secs >= 60 else f"{secs:.0f}s"
        except Exception:
            pass

    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    template_dir = Path(__file__).parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("approve.html")

    # PR branch context
    pr_source_branch = f"sandclaude/{task.id}"
    pr_target_branch = task.branch or "(default branch)"

    html = template.render(
        task_id=task.id,
        action=action,
        prompt=task.prompt[:500],
        model=task.model,
        repo=task.repo,
        branch=task.branch or "(default)",
        pr_source_branch=pr_source_branch,
        pr_target_branch=pr_target_branch,
        duration=duration,
        cost=f"{task.total_cost_usd:.4f}" if task.total_cost_usd else "?",
        risk_level=risk_level,
        risk_reasons=risk_reasons,
        files_changed=files_changed,
        diff_preview=diff_preview,
        gates=[{"status": g.status.value, "action": g.action, "reason": g.reason} for g in gates],
        has_pending=has_pending,
        has_create_pr_gate=any(g.action == "create_pr" for g in gates),
    )
    return HTMLResponse(html)


def _colorize_diff(diff: str) -> str:
    """Simple HTML colorization for diff preview (escaped)."""
    import html

    lines = []
    for line in diff.split("\n")[:200]:
        escaped = html.escape(line)
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(f'<span class="diff-add">{escaped}</span>')
        elif line.startswith("-") and not line.startswith("---"):
            lines.append(f'<span class="diff-del">{escaped}</span>')
        elif line.startswith("@@") or line.startswith("diff "):
            lines.append(f'<span class="diff-header">{escaped}</span>')
        else:
            lines.append(escaped)
    return "\n".join(lines)


# ── v0.2.0: Token management ─────────────────────────────────


@app.post("/tokens", status_code=201)
async def create_token_endpoint(
    request: TokenCreateRequest, token: str = Depends(_require_auth)
) -> dict:
    # Only admin-scoped tokens can create new tokens
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:tokens")

    from datetime import datetime, timedelta, timezone

    raw_token = generate_token()
    fp = token_fingerprint(raw_token)

    expires_at = None
    if request.expires_in_days:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=request.expires_in_days)
        ).isoformat()

    token_info = await db.create_token(
        name=request.name,
        token_hash=fp,
        scopes=request.scopes,
        expires_at=expires_at,
        created_by=auth.fingerprint,
    )

    return TokenCreateResponse(
        id=token_info.id,
        name=token_info.name,
        token=raw_token,
        scopes=token_info.scopes,
        created_at=token_info.created_at,
        expires_at=token_info.expires_at,
    ).model_dump()


@app.get("/tokens")
async def list_tokens_endpoint(token: str = Depends(_require_auth)) -> list[dict]:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:tokens")
    tokens = await db.list_tokens()
    # Never return the token_hash in list responses
    return [
        {
            "id": t.id,
            "name": t.name,
            "scopes": t.scopes,
            "created_at": t.created_at,
            "expires_at": t.expires_at,
            "revoked_at": t.revoked_at,
            "is_active": t.is_active(),
        }
        for t in tokens
    ]


@app.post("/tokens/{token_id}/revoke")
async def revoke_token_endpoint(token_id: int, token: str = Depends(_require_auth)) -> dict:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:tokens")
    ok = await db.revoke_token(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    return {"revoked": token_id}


# ── v0.2.0: Policy presets ───────────────────────────────────


@app.put("/policies/{name}")
async def save_policy_endpoint(
    name: str, config: dict, token: str = Depends(_require_auth)
) -> dict:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    if not re.match(r"^[a-z0-9_-]{1,64}$", name):
        raise HTTPException(status_code=400, detail="Invalid preset name")
    preset = await db.save_policy_preset(name, config)
    return preset.model_dump()


@app.get("/policies")
async def list_policies_endpoint(token: str = Depends(_require_auth)) -> list[dict]:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    presets = await db.list_policy_presets()
    return [p.model_dump() for p in presets]


@app.get("/policies/{name}")
async def get_policy_endpoint(name: str, token: str = Depends(_require_auth)) -> dict:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    preset = await db.get_policy_preset(name)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    return preset.model_dump()


@app.delete("/policies/{name}")
async def delete_policy_endpoint(name: str, token: str = Depends(_require_auth)) -> dict:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    ok = await db.delete_policy_preset(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Preset not found")
    return {"deleted": name}


# ── v0.2.0: Risk summary ─────────────────────────────────────


@app.get("/tasks/{task_id}/risk")
async def get_risk_summary_endpoint(task_id: str, token: str = Depends(_require_auth)) -> dict:
    """Get a structured risk assessment for a completed task."""
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    if task.status not in (TaskStatus.completed, TaskStatus.pending_approval):
        raise HTTPException(
            status_code=400,
            detail=f"Risk summary requires a completed task (current: {task.status.value})",
        )

    task_dir = settings.data_dir / "tasks" / task.id
    diff_path = task_dir / "diff.patch"
    audit_path = task_dir / "audit.json"

    if not diff_path.exists():
        raise HTTPException(status_code=404, detail="Diff not available")

    diff = await _read_file_async(diff_path)
    audit_data: dict = {}
    if audit_path.exists():
        raw = await _read_json_async(audit_path)
        if isinstance(raw, dict):
            audit_data = raw

    from sandclaude.risk import generate_risk_summary

    summary = generate_risk_summary(
        diff,
        audit_data,
        tokens_input=task.tokens_input or 0,
        tokens_output=task.tokens_output or 0,
        cost_usd=task.total_cost_usd or 0.0,
    )

    from dataclasses import asdict

    return asdict(summary)


# ── v0.2.0: Review mode ─────────────────────────────────────


@app.post("/tasks/{task_id}/review")
async def review_task_endpoint(task_id: str, token: str = Depends(_require_auth)) -> dict:
    """Use Claude to review a completed task's diff and produce a review report.

    Returns risks, missing tests, suspicious changes, and files
    that deserve extra reviewer attention.
    """
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    if task.status not in (TaskStatus.completed, TaskStatus.pending_approval):
        raise HTTPException(
            status_code=400,
            detail=f"Review requires a completed task (current: {task.status.value})",
        )

    task_dir = settings.data_dir / "tasks" / task.id
    diff_path = task_dir / "diff.patch"
    if not diff_path.exists():
        raise HTTPException(status_code=404, detail="Diff not available")

    diff = await _read_file_async(diff_path)

    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="Review mode requires ANTHROPIC_API_KEY",
        )

    try:
        review = await _generate_ai_review(task.prompt, diff)
        return {"task_id": task_id, "review": review}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_sanitize_error(exc))


async def _generate_ai_review(prompt: str, diff: str) -> dict:
    """Use Claude Haiku to review a diff and produce structured feedback."""
    import httpx

    diff_excerpt = diff[:8000]
    if len(diff) > 8000:
        diff_excerpt += "\n... (truncated)"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "You are a senior code reviewer. Review this diff "
                            "and produce a structured assessment. Respond in "
                            "JSON with these fields:\n"
                            '- "risks": list of major risk strings\n'
                            '- "missing_tests": list of areas that lack test '
                            "coverage\n"
                            '- "suspicious_changes": list of changes that look '
                            "unusual or potentially problematic\n"
                            '- "security_concerns": list of security issues\n'
                            '- "attention_files": list of files deserving extra'
                            " review\n"
                            '- "summary": 2-3 sentence overall assessment\n\n'
                            "Be concise. Only flag genuine concerns, not style "
                            "nitpicks.\n\n"
                            f"Task prompt: {prompt[:500]}\n\n"
                            f"Diff:\n{diff_excerpt}"
                        ),
                    }
                ],
            },
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Review API call failed: {resp.status_code}")

    data = resp.json()
    content = data.get("content", [])
    if content and content[0].get("type") == "text":
        text = content[0]["text"].strip()
        # Try to parse JSON from the response
        import json as _json

        # Handle markdown code fences
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            return {"summary": text, "raw": True}

    raise RuntimeError("Empty review response")


# ── v0.2.0: Task secrets audit ───────────────────────────────


@app.get("/tasks/{task_id}/secrets")
async def get_task_secrets_endpoint(
    task_id: str, token: str = Depends(_require_auth)
) -> list[dict]:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)
    return await db.get_task_secrets(task_id)


# ── v0.2.0: Retry / follow-up ─────────────────────────────────


class RetryRequest(BaseModel):
    prompt: str = Field(..., max_length=100_000)
    max_turns: int | None = Field(None, ge=1, le=500)


@app.post("/tasks/{task_id}/retry", status_code=201)
async def retry_task_endpoint(
    task_id: str,
    body: RetryRequest,
    token: str = Depends(_require_auth),
) -> dict:
    """Create a follow-up task that references the original.

    The new task runs against the same repo/branch with a new prompt,
    typically used for addressing review feedback or fixing test failures.
    """
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    if task.status not in (TaskStatus.completed, TaskStatus.failed):
        raise HTTPException(
            status_code=400,
            detail=f"Can only retry completed or failed tasks (current: {task.status.value})",
        )

    # Build follow-up prompt with context from original task
    follow_up_prompt = (
        f"This is a follow-up to a previous task.\n\n"
        f"Original prompt: {task.prompt[:500]}\n\n"
        f"Follow-up instructions: {body.prompt}"
    )

    new_task_id = f"task-{_secrets.token_hex(8)}"
    new_task = await db.create_task(
        task_id=new_task_id,
        repo=task.repo,
        branch=task.branch,
        prompt=follow_up_prompt,
        model=task.model,
        max_turns=body.max_turns or task.max_turns,
        priority=task.priority,
        owner_token_hash=token_fingerprint(token),
        host_cwd=task.host_cwd,
        policy_preset=task.policy_preset,
        cost_budget_usd=task.cost_budget_usd,
    )

    from sandclaude.policy import create_required_gates, resolve_effective_policy

    policy = await resolve_effective_policy(new_task)
    await create_required_gates(new_task_id, policy)

    await submit_task(new_task)
    return new_task.safe_dump()


# ── v0.2.0: Task bundle export ───────────────────────────────


@app.get("/tasks/{task_id}/bundle")
async def export_bundle_endpoint(task_id: str, token: str = Depends(_require_auth)) -> dict:
    """Export a reproducible task bundle with all artifacts.

    Returns a JSON bundle containing prompt, repo, diff, audit, cost,
    policies applied, and outcome metadata — useful for debugging,
    compliance, and incident review.
    """
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, token)

    bundle: dict = {
        "version": "0.2.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "task": task.safe_dump(),
    }

    task_dir = settings.data_dir / "tasks" / task.id

    # Include diff
    diff_path = task_dir / "diff.patch"
    if diff_path.exists():
        try:
            bundle["diff"] = await _read_file_async(diff_path)
        except Exception:
            bundle["diff"] = None

    # Include audit
    audit_path = task_dir / "audit.json"
    if audit_path.exists():
        try:
            bundle["audit"] = await _read_json_async(audit_path)
        except Exception:
            bundle["audit"] = None

    # Include result
    result_path = task_dir / "result.json"
    if result_path.exists():
        try:
            bundle["result"] = await _read_json_async(result_path)
        except Exception:
            bundle["result"] = None

    # Include approval gates
    gates = await db.get_approval_gates(task_id)
    bundle["approval_gates"] = [g.model_dump() for g in gates]

    # Include secrets audit
    secrets = await db.get_task_secrets(task_id)
    bundle["secrets_audit"] = secrets

    # Include applied policy
    if task.policy_preset:
        preset = await db.get_policy_preset(task.policy_preset)
        bundle["applied_policy"] = preset.model_dump() if preset else None

    # Include risk summary if diff exists
    if bundle.get("diff"):
        from dataclasses import asdict

        from sandclaude.risk import generate_risk_summary

        summary = generate_risk_summary(
            bundle["diff"],
            bundle.get("audit") or {},
            tokens_input=task.tokens_input or 0,
            tokens_output=task.tokens_output or 0,
            cost_usd=task.total_cost_usd or 0.0,
        )
        bundle["risk_summary"] = asdict(summary)

    return bundle


# ── WebSocket /tasks/{task_id}/stream ──────────────────────────


@app.websocket("/tasks/{task_id}/stream")
async def stream_task(ws: WebSocket, task_id: str) -> None:
    _validate_task_id(task_id)

    # S10: Auth via Authorization header only (query param removed for security —
    # tokens in URLs leak via logs, browser history, and proxy logs).
    token: str | None = None
    auth_header = ws.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        await ws.close(code=4001, reason="Missing bearer token")
        return
    try:
        verify_token(token)
    except HTTPException:
        await ws.close(code=4003, reason="Invalid token")
        return

    await ws.accept()

    task = await db.get_task(task_id)
    if not task:
        await ws.send_json({"error": "Task not found"})
        await ws.close()
        return
    try:
        _require_task_owner(task.owner_token_hash, token)
    except HTTPException:
        # Return same response as "not found" to prevent task ID enumeration
        await ws.send_json({"error": "Task not found"})
        await ws.close()
        return

    log_dir = settings.data_dir / "tasks" / task_id
    last_size = 0
    last_entry_count = 0  # F3: track entries sent for delta streaming
    # Cap stream duration to task timeout + 60s buffer to prevent resource leaks
    stream_deadline = asyncio.get_running_loop().time() + settings.task_timeout_s + 60

    try:
        while asyncio.get_running_loop().time() < stream_deadline:
            try:
                transcript_path = log_dir / "transcript.json"
                if transcript_path.exists():
                    size = transcript_path.stat().st_size
                    if size > last_size and size <= 10_000_000:
                        # F3: Only send new entries (delta).
                        # Cap at 10MB to avoid blocking event loop on huge transcripts.
                        entries = await _read_json_async(transcript_path)
                        if isinstance(entries, list) and len(entries) > last_entry_count:
                            new_entries = entries[last_entry_count:]
                            await ws.send_json({"type": "transcript", "entries": new_entries})
                            last_entry_count = len(entries)
                        last_size = size
                    elif size > 10_000_000 and last_size <= 10_000_000:
                        # Transcript exceeded size cap — notify client once
                        await ws.send_json(
                            {
                                "type": "warning",
                                "message": "Transcript too large for live streaming",
                            }
                        )
                        last_size = size
            except (json.JSONDecodeError, OSError) as exc:
                # Transient file read/parse error — log and continue polling
                logger.warning("Transcript read error for %s: %s", task_id, exc)

            current = await db.get_task(task_id)
            if current and current.status in (
                TaskStatus.completed,
                TaskStatus.failed,
                TaskStatus.cancelled,
                TaskStatus.pending_approval,
            ):
                await ws.send_json(
                    {
                        "type": "done",
                        "status": current.status.value,
                        "task_id": task_id,
                    }
                )
                break

            await asyncio.sleep(1)
        else:
            # Stream deadline exceeded
            await ws.send_json({"type": "error", "message": "Stream timed out"})
    except Exception as exc:
        # WebSocket disconnect or other unrecoverable error
        try:
            await ws.send_json({"type": "error", "message": "Stream interrupted"})
        except Exception:
            pass
        logger.warning("Stream error for %s: %s: %s", task_id, type(exc).__name__, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
