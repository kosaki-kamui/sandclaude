"""PR creation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from sandclaude.api.deps import (
    _require_auth,
    _require_task_owner,
    _sanitize_error,
    _validate_task_id,
)
from sandclaude.auth import AuthResult, require_scope
from sandclaude.db import store as db
from sandclaude.github import create_pr
from sandclaude.models import (
    ApprovalDecisionRequest,
    ApprovalStatus,
    CreatePRRequest,
    TaskStatus,
)

router = APIRouter()


# ── POST /tasks/{task_id}/create-pr ──────────────────────────


@router.post("/tasks/{task_id}/create-pr")
async def create_pr_endpoint(
    task_id: str, body: CreatePRRequest | None = None, auth: AuthResult = Depends(_require_auth)
) -> dict:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth, task.created_by_user_id)

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


# ── POST /tasks/{task_id}/approve-and-create-pr ──────────────


@router.post("/tasks/{task_id}/approve-and-create-pr")
async def approve_and_create_pr_endpoint(
    task_id: str,
    body: ApprovalDecisionRequest | None = None,
    auth: AuthResult = Depends(_require_auth),
) -> dict:
    """Approve the create_pr gate and create the PR in one step.

    Requires tasks:approve scope. If the gate is already approved,
    skips the approval step and creates the PR directly. If rejected,
    returns 403. If no create_pr gate exists, creates the PR directly.
    """
    require_scope(auth, "tasks:approve")

    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth, task.created_by_user_id)

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
            decided_by_user_id=auth.user_id,
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
