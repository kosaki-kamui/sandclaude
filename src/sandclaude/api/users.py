"""User management endpoints (v0.3.0)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from sandclaude.api.deps import _require_auth
from sandclaude.auth import AuthResult, require_scope
from sandclaude.db import store as db
from sandclaude.models import UserCreateRequest

router = APIRouter()


@router.post("/users", status_code=201)
async def create_user_endpoint(
    request: UserCreateRequest, auth: AuthResult = Depends(_require_auth)
) -> dict:
    require_scope(auth, "admin:users")

    existing = await db.get_user_by_username(request.username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already taken")

    user = await db.create_user(
        username=request.username,
        display_name=request.display_name,
        email=request.email,
        github_username=request.github_username,
        is_service_account=request.is_service_account,
        created_by_user_id=auth.user_id,
    )
    return user.model_dump()


@router.get("/users")
async def list_users_endpoint(
    auth: AuthResult = Depends(_require_auth),
) -> list[dict]:
    require_scope(auth, "admin:users")
    users = await db.list_users()
    return [u.model_dump() for u in users]


@router.get("/users/{user_id}")
async def get_user_endpoint(user_id: int, auth: AuthResult = Depends(_require_auth)) -> dict:
    require_scope(auth, "admin:users")
    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user.model_dump()


@router.delete("/users/{user_id}")
async def delete_user_endpoint(user_id: int, auth: AuthResult = Depends(_require_auth)) -> dict:
    require_scope(auth, "admin:users")
    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Revoke all tokens belonging to this user
    tokens = await db.list_tokens()
    for t in tokens:
        keys = t.keys() if hasattr(t, "keys") else []
        user_id_val = t["user_id"] if "user_id" in keys else None
        if user_id_val == user_id:
            await db.revoke_token(t["id"])

    deleted = await db.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"deleted": user_id}
