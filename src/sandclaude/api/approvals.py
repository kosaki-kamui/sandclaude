"""Approval-gate routes for the sandclaude API.

Extracted from main.py — handles listing, approving, rejecting approval
gates, generating approval links, and the server-rendered approval UI page.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from fastapi.responses import HTMLResponse

from sandclaude.auth import AuthResult, require_scope
from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.models import (
    ApprovalDecisionRequest,
    ApprovalStatus,
    TaskStatus,
)
from sandclaude.runner.pool import submit_task

from .deps import _require_auth, _require_task_owner, _validate_task_id

logger = logging.getLogger(__name__)

router = APIRouter()


# ── v0.2.0: Approval gates ────────────────────────────────────


@router.get("/tasks/{task_id}/approvals")
async def list_approvals_endpoint(
    task_id: str, auth: AuthResult = Depends(_require_auth)
) -> list[dict]:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)
    gates = await db.get_approval_gates(task_id)
    return [g.model_dump() for g in gates]


@router.post("/tasks/{task_id}/approve/{action}")
async def approve_action_endpoint(
    task_id: str,
    action: str,
    body: ApprovalDecisionRequest | None = None,
    auth: AuthResult = Depends(_require_auth),
) -> dict:
    # Scope check: approval requires explicit tasks:approve permission
    require_scope(auth, "tasks:approve")

    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    ok = await db.decide_approval_gate(
        task_id,
        action,
        decision=ApprovalStatus.approved,
        decided_by=auth.fingerprint,
        reason=body.reason if body else None,
        decided_by_user_id=auth.user_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No pending approval gate found")

    # Check if this was a pre-execution budget gate on a pending_approval task
    # If so, resume execution now that the budget is approved
    task = await db.get_task(task_id)  # re-fetch after gate update
    if action == "budget_exceeded" and task and task.status == TaskStatus.pending_approval:
        # Only check budget-related gates, not post-execution gates like create_pr
        budget_gates = await db.get_approval_gates(task_id)
        budget_pending = any(
            g.action == "budget_exceeded" and g.status.value == "pending" for g in budget_gates
        )
        if not budget_pending:
            # Create post-execution gates now (deferred from task creation)
            from sandclaude.policy import (
                create_required_gates,
                resolve_effective_policy,
            )

            eff_policy = await resolve_effective_policy(task)
            await create_required_gates(task_id, eff_policy)

            # Check if deferred gates were created (e.g. create_pr)
            # If so, requires_approval stays 1; if not, clear it
            still_pending = await db.has_pending_gates(task_id)
            await db.update_task(
                task_id,
                status=TaskStatus.queued,
                requires_approval=1 if still_pending else 0,
            )
            task = await db.get_task(task_id)
            if task:
                await submit_task(task)
            return {
                "status": "approved",
                "task_id": task_id,
                "action": action,
                "execution": "resumed",
            }

    # For non-budget gates, just update requires_approval flag
    if not await db.has_pending_gates(task_id):
        await db.update_task(task_id, requires_approval=0)

    return {"status": "approved", "task_id": task_id, "action": action}


@router.post("/tasks/{task_id}/reject/{action}")
async def reject_action_endpoint(
    task_id: str,
    action: str,
    body: ApprovalDecisionRequest | None = None,
    auth: AuthResult = Depends(_require_auth),
) -> dict:
    # Scope check: rejection also requires explicit tasks:approve permission
    require_scope(auth, "tasks:approve")

    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth)

    ok = await db.decide_approval_gate(
        task_id,
        action,
        decision=ApprovalStatus.rejected,
        decided_by=auth.fingerprint,
        reason=body.reason if body else None,
        decided_by_user_id=auth.user_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No pending approval gate found")

    # If this was a pre-execution budget gate rejection, mark the task as failed
    task = await db.get_task(task_id)
    if action == "budget_exceeded" and task and task.status == TaskStatus.pending_approval:
        await db.update_task(
            task_id,
            status=TaskStatus.failed,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error="budget_approval_rejected",
            requires_approval=0,
        )

    return {"status": "rejected", "task_id": task_id, "action": action}


@router.post("/tasks/{task_id}/approval-link/{action}")
async def generate_approval_link_endpoint(
    task_id: str,
    action: str,
    auth: AuthResult = Depends(_require_auth),
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
    _require_task_owner(task.owner_token_hash, auth)

    link_token = create_approval_link_token(task_id, action)
    base_url = settings.api_url.rstrip("/")
    url = f"{base_url}/approve/{task_id}/{action}?token={link_token}"

    return {"approval_url": url, "expires_in_seconds": 3600}


# ── v0.2.0: Approval UI (server-rendered) ────────────────────


@router.get("/approve/{task_id}/{action}")
async def approval_ui_page(
    task_id: str,
    action: str,
    token: str = "",
    sandclaude_session: str | None = Cookie(None),
) -> HTMLResponse:
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

    # Budget check context — prefer stored admission data, fall back to
    # recomputation for pre-upgrade tasks. Use live gate status, not the
    # stale admission-time decision.
    budget_check_ctx: dict | None = None
    budget_gate = next((g for g in gates if g.action == "budget_exceeded"), None)
    live_status = budget_gate.status.value if budget_gate else None

    if task.budget_check_json:
        import json as _json

        stored = _json.loads(task.budget_check_json)
        budget_check_ctx = {
            "predicted_total_usd": f"{stored.get('predicted_total_usd', 0):.4f}",
            "max_budget_usd": f"{stored.get('max_budget_usd', 0):.2f}",
            "confidence": stored.get("confidence", "unknown"),
            "status": live_status or stored.get("status", "unknown"),
            "mode": stored.get("mode", "static"),
        }
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
            budget_check_ctx = {
                "predicted_total_usd": f"{est.predicted_total_usd:.4f}",
                "max_budget_usd": f"{eff_budget:.2f}",
                "confidence": est.confidence,
                "status": live_status or "unknown",
                "mode": est.mode,
            }

    # v0.3.0: Check for GitHub OAuth session
    logged_in_as = None
    github_oauth_available = bool(settings.github_client_id)
    if sandclaude_session:
        from sandclaude.auth import verify_session_cookie

        session = verify_session_cookie(sandclaude_session)
        if session:
            logged_in_as = session.username

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
        budget_check=budget_check_ctx,
        logged_in_as=logged_in_as,
        github_oauth_available=github_oauth_available,
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
