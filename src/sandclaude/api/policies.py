"""Policy preset routes (v0.2.0)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from sandclaude.api.deps import _require_auth
from sandclaude.auth import require_scope, verify_token_with_scopes
from sandclaude.db import store as db

router = APIRouter()


@router.put("/policies/{name}")
async def save_policy_endpoint(
    name: str, config: dict, token: str = Depends(_require_auth)
) -> dict:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    if not re.match(r"^[a-z0-9_-]{1,64}$", name):
        raise HTTPException(status_code=400, detail="Invalid preset name")
    preset = await db.save_policy_preset(name, config)
    return preset.model_dump()


@router.get("/policies")
async def list_policies_endpoint(token: str = Depends(_require_auth)) -> list[dict]:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    presets = await db.list_policy_presets()
    return [p.model_dump() for p in presets]


@router.get("/policies/{name}")
async def get_policy_endpoint(name: str, token: str = Depends(_require_auth)) -> dict:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    preset = await db.get_policy_preset(name)
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    return preset.model_dump()


@router.delete("/policies/{name}")
async def delete_policy_endpoint(name: str, token: str = Depends(_require_auth)) -> dict:
    auth = await verify_token_with_scopes(token)
    require_scope(auth, "admin:policies")
    ok = await db.delete_policy_preset(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Preset not found")
    return {"deleted": name}
