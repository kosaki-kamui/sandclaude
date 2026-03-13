"""System routes: health check, pool stats, and metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sandclaude.api.deps import _require_auth
from sandclaude.auth import AuthResult
from sandclaude.db import store as db
from sandclaude.runner.pool import get_pool_stats

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.3.0"}


@router.get("/pool", dependencies=[Depends(_require_auth)])
async def pool_stats() -> dict:
    return await get_pool_stats()


@router.get("/metrics")
async def metrics_endpoint(auth: AuthResult = Depends(_require_auth)) -> dict:
    """Aggregated task metrics: status counts, cost, tokens, timing, error categories."""
    return await db.get_task_metrics()
