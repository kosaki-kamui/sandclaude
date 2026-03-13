"""Token management routes (v0.2.0)."""

from __future__ import annotations

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
