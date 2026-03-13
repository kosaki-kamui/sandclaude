"""System routes: health check and pool stats."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sandclaude.api.deps import _require_auth
from sandclaude.runner.pool import get_pool_stats

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.2.5"}


@router.get("/pool", dependencies=[Depends(_require_auth)])
async def pool_stats() -> dict:
    return await get_pool_stats()
