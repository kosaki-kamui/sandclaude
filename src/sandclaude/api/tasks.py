"""Task CRUD, cancel, retry, bundle export, and WebSocket streaming routes."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as _secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from sandclaude.api.deps import (
    _check_create_rate_limit,
    _read_file_async,
    _read_json_async,
    _require_auth,
    _require_task_owner,
    _validate_task_id,
)
from sandclaude.auth import AuthResult, token_fingerprint, verify_token_with_scopes
from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.models import (
    TaskCreateRequest,
    TaskPriority,
    TaskStatus,
)
from sandclaude.runner.container import cancel_container
from sandclaude.runner.pool import submit_task

logger = logging.getLogger(__name__)

router = APIRouter()


# ── POST /tasks ────────────────────────────────────────────────


@router.post("/tasks", status_code=201)
async def create_task_endpoint(
    request: TaskCreateRequest, auth: AuthResult = Depends(_require_auth)
) -> dict:
    _check_create_rate_limit(auth)

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
        owner_token_hash=auth.fingerprint,
        created_by_user_id=auth.user_id,
        host_cwd=request.host_cwd,
        allowed_domains=request.allowed_domains,
        notify_webhook=request.notify.webhook if request.notify else None,
        notify_on=request.notify.on if request.notify else None,
        policy_preset=request.policy_preset,
        declared_secrets=request.declared_secrets,
        cost_budget_usd=request.cost_budget_usd,
        labels=request.labels,
    )

    # Resolve effective policy (but do NOT create post-execution gates yet —
    # those are deferred until after the budget check to avoid deadlock
    # between pre-execution budget gates and post-execution action gates)
    from sandclaude.policy import create_required_gates, resolve_effective_policy

    policy = await resolve_effective_policy(task)

    # v0.2.5: Pre-flight budget admission control
    #
    # Effective budget = min(preset.max_cost_usd, task.cost_budget_usd).
    # A task cannot raise the budget above the preset ceiling.
    # If the preset defines max_cost_usd but the task omits cost_budget_usd,
    # the preset ceiling still applies.
    budget_check: dict | None = None
    effective_budget: float | None = policy.max_cost_usd
    if task.cost_budget_usd is not None:
        if effective_budget is not None:
            effective_budget = min(effective_budget, task.cost_budget_usd)
        else:
            effective_budget = task.cost_budget_usd

    if effective_budget is not None:
        from sandclaude.estimator import run_budget_check

        budget_fail_policy = policy.budget_fail_policy or "reject"
        has_api_key = bool(settings.anthropic_api_key)
        budget_check = await run_budget_check(
            model=task.model,
            max_turns=task.max_turns,
            prompt=task.prompt,
            max_budget_usd=effective_budget,
            budget_fail_policy=budget_fail_policy,
            anthropic_api_key=settings.anthropic_api_key,
            has_ai_pr_title=has_api_key,
            has_ai_pr_summary=has_api_key,
        )

        # Persist the admission-time budget_check so GET and approval UI
        # can display the exact same numbers without recomputing.
        import json as _json

        await db.update_task(task_id, budget_check_json=_json.dumps(budget_check))

        if budget_check["status"] == "rejected":
            await db.delete_task(task_id)
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "predicted budget exceeds budget cap",
                    "budget_check": budget_check,
                },
            )

        if budget_check["status"] == "requires_approval":
            await db.create_approval_gate(task_id, "budget_exceeded")
            await db.update_task(
                task_id,
                requires_approval=1,
                status=TaskStatus.pending_approval,
            )
            # Do NOT submit — task blocks until budget gate is approved.
            # Do NOT create post-execution gates yet — they'll be created
            # when the budget gate is approved and execution resumes.
            # Re-fetch task to return current DB state (not stale in-memory).
            fresh_task = await db.get_task(task_id)
            result = fresh_task.safe_dump() if fresh_task else task.safe_dump()
            result["budget_check"] = budget_check
            return result

    # Create post-execution approval gates (e.g. create_pr) now that
    # the budget check has passed. These gates fire after execution,
    # not before, so they don't block task submission.
    has_secrets = bool(request.declared_secrets)
    cost_est = budget_check.get("predicted_total_usd") if budget_check else None
    await create_required_gates(
        task_id,
        policy,
        predicted_cost=cost_est,
        has_secrets=has_secrets,
        repo=request.repo,
        preset_name=request.policy_preset,
    )

    await submit_task(task)
    # Re-fetch to reflect any requires_approval changes from gate creation
    fresh = await db.get_task(task_id)
    result = fresh.safe_dump() if fresh else task.safe_dump()
    if budget_check:
        result["budget_check"] = budget_check
    return result


# ── GET /tasks ─────────────────────────────────────────────────


@router.get("/tasks")
async def list_tasks_endpoint(
    auth: AuthResult = Depends(_require_auth),
    status: str | None = None,
    preset: str | None = None,
    label: str | None = None,
    repo: str | None = None,
) -> list[dict]:
    from sandclaude.auth import get_token

    caller_fp = auth.fingerprint
    # Primary token also sees legacy tasks with NULL owner_token_hash
    is_primary = caller_fp == token_fingerprint(get_token())
    tasks = await db.list_tasks_for_owner(caller_fp, include_unowned=is_primary)

    # v0.3.0: Filter by query params
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    if preset:
        tasks = [t for t in tasks if t.policy_preset == preset]
    if repo:
        tasks = [t for t in tasks if t.repo == repo]
    if label:
        import json as _json

        tasks = [
            t for t in tasks if t.labels and label in (_json.loads(t.labels) if t.labels else [])
        ]

    return [t.safe_dump() for t in tasks]  # S11: exclude internal fields


# ── GET /tasks/{task_id} ───────────────────────────────────────


@router.get("/tasks/{task_id}")
async def get_task_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    result = task.safe_dump()  # S11: exclude internal fields
    task_dir = settings.data_dir / "tasks" / task.id

    # F1: async file reads
    audit_path = task_dir / "audit.json"
    if audit_path.exists():
        result["audit"] = await _read_json_async(audit_path)

    diff_path = task_dir / "diff.patch"
    if diff_path.exists():
        result["diff"] = await _read_file_async(diff_path)

    # Include budget_check: prefer stored admission data, fall back to
    # recomputation for pre-upgrade tasks that have a budget gate but no
    # stored budget_check_json.
    gates = await db.get_approval_gates(task_id)
    budget_gate = next((g for g in gates if g.action == "budget_exceeded"), None)
    if task.budget_check_json:
        import json as _json

        budget_info = _json.loads(task.budget_check_json)
        if budget_gate:
            budget_info["gate_status"] = budget_gate.status.value
        result["budget_check"] = budget_info
    elif budget_gate:
        # Fallback for tasks created before budget_check_json was stored
        from sandclaude.estimator import estimate_static
        from sandclaude.policy import resolve_effective_policy

        eff_policy = await resolve_effective_policy(task)
        eff_budget: float | None = eff_policy.max_cost_usd
        if task.cost_budget_usd is not None:
            if eff_budget is not None:
                eff_budget = min(eff_budget, task.cost_budget_usd)
            else:
                eff_budget = task.cost_budget_usd
        if eff_budget is not None:
            est = estimate_static(
                model=task.model,
                max_turns=task.max_turns,
                prompt_length=len(task.prompt),
            )
            result["budget_check"] = {
                "predicted_total_usd": round(est.predicted_total_usd, 4),
                "max_budget_usd": round(eff_budget, 4),
                "confidence": est.confidence,
                "gate_status": budget_gate.status.value,
                "mode": est.mode,
            }

    return result


# ── GET /tasks/{task_id}/diff ──────────────────────────────────


@router.get("/tasks/{task_id}/diff")
async def get_diff_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    diff_path = settings.data_dir / "tasks" / task.id / "diff.patch"
    if not diff_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Diff not available (task may still be running)",
        )

    content = await _read_file_async(diff_path)  # F1
    return PlainTextResponse(content)


# ── GET /tasks/{task_id}/audit ─────────────────────────────────


@router.get("/tasks/{task_id}/audit")
async def get_audit_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    audit_path = settings.data_dir / "tasks" / task.id / "audit.json"
    if not audit_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Audit log not available (task may still be running)",
        )

    data = await _read_json_async(audit_path)  # F1
    return JSONResponse(data)


# ── GET /tasks/{task_id}/result ────────────────────────────────


@router.get("/tasks/{task_id}/result")
async def get_result_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    result_path = settings.data_dir / "tasks" / task.id / "result.json"
    if not result_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Result not available (task may still be running)",
        )

    data = await _read_json_async(result_path)  # F1
    return JSONResponse(data)


# ── GET /tasks/{task_id}/transcript ────────────────────────────


@router.get("/tasks/{task_id}/transcript")
async def get_transcript_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)):
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    transcript_path = settings.data_dir / "tasks" / task.id / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Transcript not available (task may still be running)",
        )

    data = await _read_json_async(transcript_path)  # F1
    return JSONResponse(data)


# ── DELETE /tasks/{task_id} ────────────────────────────────────


@router.delete("/tasks/{task_id}")
async def delete_task_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    if task.status in (TaskStatus.setup, TaskStatus.running, TaskStatus.pending_approval):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an active task. Cancel it first.",
        )

    await db.delete_task(task_id)
    return {"deleted": task_id}


# ── POST /tasks/{task_id}/cancel ───────────────────────────────


class CancelRequest(BaseModel):
    reason: str | None = Field(None, max_length=500)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task_endpoint(
    task_id: str,
    body: CancelRequest | None = None,
    auth: AuthResult = Depends(_require_auth),
) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    if task.status not in (TaskStatus.queued, TaskStatus.setup, TaskStatus.running):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel task in status: {task.status.value}",
        )

    ok = await cancel_container(task)
    if ok:
        # v0.3.0: Record cancel reason if provided
        if body and body.reason:
            await db.update_task(
                task.id,
                cancel_reason=body.reason,
                error_category="cancelled",
            )
        return {"status": "cancelled", "task_id": task.id}
    # Conditional update returned False — task status changed concurrently
    fresh = await db.get_task(task_id)
    current_status = fresh.status.value if fresh else "deleted"
    raise HTTPException(
        status_code=409,
        detail=f"Task is no longer cancellable (current status: {current_status})",
    )


# ── v0.2.0: Retry / follow-up ─────────────────────────────────


class RetryRequest(BaseModel):
    prompt: str = Field(..., max_length=100_000)
    max_turns: int | None = Field(None, ge=1, le=500)


@router.post("/tasks/{task_id}/retry", status_code=201)
async def retry_task_endpoint(
    task_id: str,
    body: RetryRequest,
    auth: AuthResult = Depends(_require_auth),
) -> dict:
    """Create a follow-up task that references the original.

    The new task runs against the same repo/branch with a new prompt,
    typically used for addressing review feedback or fixing test failures.
    """
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

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
        owner_token_hash=auth.fingerprint,
        created_by_user_id=auth.user_id,
        host_cwd=task.host_cwd,
        policy_preset=task.policy_preset,
        cost_budget_usd=task.cost_budget_usd,
        parent_task_id=task.id,
    )

    from sandclaude.policy import create_required_gates, resolve_effective_policy

    policy = await resolve_effective_policy(new_task)

    # Run the same budget admission check as POST /tasks
    budget_check: dict | None = None
    effective_budget: float | None = policy.max_cost_usd
    if new_task.cost_budget_usd is not None:
        if effective_budget is not None:
            effective_budget = min(effective_budget, new_task.cost_budget_usd)
        else:
            effective_budget = new_task.cost_budget_usd

    if effective_budget is not None:
        from sandclaude.estimator import run_budget_check

        budget_fail_policy = policy.budget_fail_policy or "reject"
        has_api_key = bool(settings.anthropic_api_key)
        budget_check = await run_budget_check(
            model=new_task.model,
            max_turns=new_task.max_turns,
            prompt=new_task.prompt,
            max_budget_usd=effective_budget,
            budget_fail_policy=budget_fail_policy,
            anthropic_api_key=settings.anthropic_api_key,
            has_ai_pr_title=has_api_key,
            has_ai_pr_summary=has_api_key,
        )

        import json as _json

        await db.update_task(new_task_id, budget_check_json=_json.dumps(budget_check))

        if budget_check["status"] == "rejected":
            await db.delete_task(new_task_id)
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "predicted budget exceeds budget cap",
                    "budget_check": budget_check,
                },
            )

        if budget_check["status"] == "requires_approval":
            await db.create_approval_gate(new_task_id, "budget_exceeded")
            await db.update_task(
                new_task_id,
                requires_approval=1,
                status=TaskStatus.pending_approval,
            )
            fresh = await db.get_task(new_task_id)
            result = fresh.safe_dump() if fresh else new_task.safe_dump()
            result["budget_check"] = budget_check
            return result

    retry_cost = budget_check.get("predicted_total_usd") if budget_check else None
    await create_required_gates(
        new_task_id,
        policy,
        predicted_cost=retry_cost,
        has_secrets=bool(task.declared_secrets),
        repo=task.repo,
        preset_name=task.policy_preset,
    )

    await submit_task(new_task)
    result = new_task.safe_dump()
    if budget_check:
        result["budget_check"] = budget_check
    return result


# ── v0.3.0: Task timeline ──────────────────────────────────────


@router.get("/tasks/{task_id}/timeline")
async def get_task_timeline_endpoint(
    task_id: str, auth: AuthResult = Depends(_require_auth)
) -> dict:
    """Get phase breakdown and retry chain for a task."""
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    result: dict = {
        "task_id": task.id,
        "status": task.status.value,
        "error_category": task.error_category,
        "timeline": task._compute_timeline(),
    }

    # Include retry chain if this task has a parent or children
    chain = await db.get_retry_chain(task_id)
    if len(chain) > 1:
        result["retry_chain"] = [
            {"id": t.id, "status": t.status.value, "created_at": t.created_at} for t in chain
        ]

    return result


# ── v0.2.0: Task bundle export ───────────────────────────────


@router.get("/tasks/{task_id}/bundle")
async def export_bundle_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)) -> dict:
    """Export a reproducible task bundle with all artifacts.

    Returns a JSON bundle containing prompt, repo, diff, audit, cost,
    policies applied, and outcome metadata — useful for debugging,
    compliance, and incident review.
    """
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    bundle: dict = {
        "version": "0.2.5",
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


@router.websocket("/tasks/{task_id}/stream")
async def stream_task(ws: WebSocket, task_id: str) -> None:
    _validate_task_id(task_id)

    # S10: Auth via Authorization header only (query param removed for security —
    # tokens in URLs leak via logs, browser history, and proxy logs).
    raw_token: str | None = None
    auth_header = ws.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw_token = auth_header[7:].strip()
    if not raw_token:
        await ws.close(code=4001, reason="Missing bearer token")
        return
    try:
        ws_auth = await verify_token_with_scopes(raw_token)
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
        _require_task_owner(task.owner_token_hash, ws_auth)
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
