"""Token management routes (v0.2.0, extended v0.3.0 with user binding, v0.4.0 rotation)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from sandclaude.api.deps import _require_auth
from sandclaude.auth import (
    AuthResult,
    generate_token,
    require_scope,
    token_fingerprint,
)
from sandclaude.db import store as db
from sandclaude.models import TokenCreateRequest, TokenCreateResponse

router = APIRouter()


@router.post("/tokens", status_code=201)
async def create_token_endpoint(
    request: TokenCreateRequest, auth: AuthResult = Depends(_require_auth)
) -> dict:
    # Only admin-scoped tokens can create new tokens
    require_scope(auth, "admin:tokens")

    # v0.3.0: Determine which user owns the new token
    target_user_id = request.user_id or auth.user_id
    if request.user_id and request.user_id != auth.user_id:
        # Creating a token for a different user — verify user exists
        target_user = await db.get_user(request.user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user not found")

    # v0.3.0: Scope ceiling — token scopes cannot exceed the creator's scopes
    if not auth.is_legacy:
        for scope in request.scopes:
            if scope not in auth.scopes:
                raise HTTPException(
                    status_code=403,
                    detail=f"Cannot grant scope '{scope}' — you do not have it",
                )

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
        user_id=target_user_id,
    )

    return TokenCreateResponse(
        id=token_info.id,
        name=token_info.name,
        token=raw_token,
        scopes=token_info.scopes,
        created_at=token_info.created_at,
        expires_at=token_info.expires_at,
    ).model_dump()


@router.get("/tokens")
async def list_tokens_endpoint(auth: AuthResult = Depends(_require_auth)) -> list[dict]:
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
            "user_id": t.user_id,
        }
        for t in tokens
    ]


@router.post("/tokens/{token_id}/revoke")
async def revoke_token_endpoint(token_id: int, auth: AuthResult = Depends(_require_auth)) -> dict:
    require_scope(auth, "admin:tokens")
    ok = await db.revoke_token(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    return {"revoked": token_id}


@router.post("/tokens/{token_id}/rotate")
async def rotate_token_endpoint(token_id: int, auth: AuthResult = Depends(_require_auth)) -> dict:
    """Rotate a token: revoke the old one and create a new one with the same config.

    Returns the new raw token (shown only once). The old token is immediately
    revoked and can no longer be used.
    """
    require_scope(auth, "admin:tokens")

    # Look up the old token
    tokens = await db.list_tokens()
    old = next((t for t in tokens if t.id == token_id), None)
    if not old:
        raise HTTPException(status_code=404, detail="Token not found")
    if not old.is_active():
        raise HTTPException(status_code=409, detail="Cannot rotate a revoked or expired token")

    # Revoke the old token
    await db.revoke_token(token_id)

    # Create a new token with the same scopes, user, and name
    raw_token = generate_token()
    fp = token_fingerprint(raw_token)

    # Preserve remaining expiry if the old token had one
    new_expires_at: str | None = None
    if old.expires_at:
        old_exp = datetime.fromisoformat(old.expires_at.replace("Z", "+00:00"))
        remaining = old_exp - datetime.now(timezone.utc)
        if remaining.total_seconds() > 0:
            new_expires_at = (datetime.now(timezone.utc) + remaining).isoformat()

    new_token = await db.create_token(
        name=f"{old.name} (rotated)",
        token_hash=fp,
        scopes=old.scopes,
        expires_at=new_expires_at,
        created_by=auth.fingerprint,
        user_id=old.user_id,
    )

    return {
        "rotated_from": token_id,
        "new_token": TokenCreateResponse(
            id=new_token.id,
            name=new_token.name,
            token=raw_token,
            scopes=new_token.scopes,
            created_at=new_token.created_at,
            expires_at=new_token.expires_at,
        ).model_dump(),
    }
