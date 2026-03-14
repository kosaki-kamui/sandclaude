"""Shared dependencies for the sandclaude API routes.

Constants, helpers, auth guards, and rate limiting — no routes live here.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict, deque
from typing import Any, MutableMapping

from fastapi import Cookie, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.types import ASGIApp, Receive, Scope, Send

from sandclaude.auth import AuthResult, token_fingerprint, verify_token_with_scopes

# ── Constants ──────────────────────────────────────────────────

TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CREATE_RATE_LIMIT_WINDOW_S = 60
CREATE_RATE_LIMIT_MAX_REQUESTS = 20
# F4: Use token fingerprint as key (bounded by number of valid tokens).
# NOTE: Rate limit state is process-local. Running with multiple uvicorn workers
# multiplies the effective limit by the worker count. The default Docker CMD uses
# a single worker. If you need multi-worker, move rate limiting to Redis/DB.
_create_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_RATE_BUCKET_MAX_SIZE = 1000  # Cap total tracked fingerprints


_MAX_ARTIFACT_BYTES = 20_000_000  # 20MB hard cap for artifact file reads


# ── File helpers ───────────────────────────────────────────────


async def _read_file_async(path, max_bytes: int = _MAX_ARTIFACT_BYTES) -> str:
    """F1: Non-blocking file read with size guard."""
    size = await asyncio.to_thread(path.stat)
    if size.st_size > max_bytes:
        raise HTTPException(status_code=413, detail="Artifact too large to serve")
    return await asyncio.to_thread(path.read_text)


async def _read_json_async(path, max_bytes: int = _MAX_ARTIFACT_BYTES) -> dict | list:
    """F1: Non-blocking JSON file read with size guard."""
    size = await asyncio.to_thread(path.stat)
    if size.st_size > max_bytes:
        raise HTTPException(status_code=413, detail="Artifact too large to serve")
    text = await asyncio.to_thread(path.read_text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail="Artifact file is corrupted or partially written",
        )


# ── Security ───────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


def _sanitize_error(exc: Exception) -> str:
    """S9: Sanitize error messages to avoid leaking internal paths/config."""
    msg = str(exc)
    # Strip absolute file paths (Unix and Windows-style)
    msg = re.sub(r"(?:/[^\s:\"']+)+/?", "<path>", msg)
    msg = re.sub(r"(?:[A-Za-z]:\\[^\s:\"']+)+\\?", "<path>", msg)
    # Truncate
    if len(msg) > 500:
        msg = msg[:500] + "..."
    return msg


# ── Auth dependency ────────────────────────────────────────────


async def _require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    sandclaude_session: str | None = Cookie(None),
) -> AuthResult:
    # v0.2.0: Accept bearer tokens (legacy + registry)
    if credentials is not None:
        result = await verify_token_with_scopes(credentials.credentials)
        # v0.4.0: Flag legacy tokens for deprecation header
        if result.is_legacy:
            request.state.legacy_token = True
        return result
    # v0.3.0: Fall back to session cookie (GitHub OAuth)
    if sandclaude_session:
        from sandclaude.auth import verify_session_cookie

        session_result = verify_session_cookie(sandclaude_session)
        if session_result:
            return session_result
    raise HTTPException(status_code=401, detail="Missing Authorization header")


class LegacyTokenDeprecationMiddleware:
    """v0.4.0: Add Deprecation header when legacy tokens are used.

    Legacy tokens (the primary .token file or AUTH_TOKENS env var) grant
    full admin access without scopes. Operators should migrate to scoped
    registry tokens created via POST /tokens.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        async def send_with_deprecation(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                if getattr(request.state, "legacy_token", False):
                    headers = list(message.get("headers", []))
                    headers.append(
                        (
                            b"x-sandclaude-deprecation",
                            b"Legacy token in use. Create a scoped token "
                            b"via POST /tokens for better security.",
                        )
                    )
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_deprecation)


def _validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.match(task_id) or "/" in task_id or "\\" in task_id or ".." in task_id:
        raise HTTPException(status_code=400, detail="Invalid task_id format")


def _check_create_rate_limit(auth: AuthResult) -> None:
    # F4: Use fingerprint as key so bucket count is bounded by valid tokens
    fp = auth.fingerprint
    now = time.monotonic()

    # Evict oldest entries if too many fingerprints tracked (instead of clearing all)
    if len(_create_rate_buckets) > _RATE_BUCKET_MAX_SIZE:
        cutoff = now - CREATE_RATE_LIMIT_WINDOW_S
        stale = [k for k, v in _create_rate_buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del _create_rate_buckets[k]

    bucket = _create_rate_buckets[fp]
    cutoff = now - CREATE_RATE_LIMIT_WINDOW_S
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= CREATE_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for task creation")
    bucket.append(now)


def _require_task_owner(
    task_owner_hash: str | None,
    auth: AuthResult,
    task_created_by_user_id: int | None = None,
) -> None:
    """Verify the caller owns this task.

    Returns 404 (not 403) on ownership mismatch to prevent cross-tenant
    task ID enumeration — an attacker cannot distinguish "task exists but
    not yours" from "task doesn't exist".

    Matches by token fingerprint OR by user_id (for session auth where
    fingerprint is empty but user identity is known).

    Legacy/migrated tasks with NULL owner_token_hash are NOT open-access —
    they are restricted to the primary server token only.
    """
    # v0.3.0: Match by user_id when both sides have one (session auth)
    if (
        auth.user_id is not None
        and task_created_by_user_id is not None
        and auth.user_id == task_created_by_user_id
    ):
        return

    caller_fp = auth.fingerprint
    if not task_owner_hash:
        # Legacy row: only the primary server token (first candidate) may access.
        from sandclaude.auth import get_token

        primary_fp = token_fingerprint(get_token())
        if caller_fp != primary_fp:
            raise HTTPException(status_code=404, detail="Task not found")
        return
    if task_owner_hash != caller_fp:
        raise HTTPException(status_code=404, detail="Task not found")
