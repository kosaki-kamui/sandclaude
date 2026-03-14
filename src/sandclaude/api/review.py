"""Review, risk summary, and secrets endpoints."""

from __future__ import annotations

import json as _json
import re

from fastapi import APIRouter, Depends, HTTPException

from sandclaude.api.deps import (
    _read_file_async,
    _read_json_async,
    _require_auth,
    _require_task_owner,
    _sanitize_error,
    _validate_task_id,
)
from sandclaude.auth import AuthResult
from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.models import TaskStatus

router = APIRouter()


# ── GET /tasks/{task_id}/risk ────────────────────────────────


@router.get("/tasks/{task_id}/risk")
async def get_risk_summary_endpoint(
    task_id: str, auth: AuthResult = Depends(_require_auth)
) -> dict:
    """Get a structured risk assessment for a completed task."""
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth, task.created_by_user_id)

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


# ── POST /tasks/{task_id}/review ─────────────────────────────


@router.post("/tasks/{task_id}/review")
async def review_task_endpoint(task_id: str, auth: AuthResult = Depends(_require_auth)) -> dict:
    """Use Claude to review a completed task's diff and produce a review report.

    Returns risks, missing tests, suspicious changes, and files
    that deserve extra reviewer attention.
    """
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth, task.created_by_user_id)

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
        # Handle markdown code fences
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            return {"summary": text, "raw": True}

    raise RuntimeError("Empty review response")


# ── GET /tasks/{task_id}/secrets ─────────────────────────────


@router.get("/tasks/{task_id}/secrets")
async def get_task_secrets_endpoint(
    task_id: str, auth: AuthResult = Depends(_require_auth)
) -> list[dict]:
    _validate_task_id(task_id)
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _require_task_owner(task.owner_token_hash, auth, task.created_by_user_id)
    return await db.get_task_secrets(task_id)
