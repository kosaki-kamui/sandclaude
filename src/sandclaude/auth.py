"""
SECURITY-CRITICAL: Bearer token authentication.

v0.1.0: Single primary token + optional AUTH_TOKENS (static bearer tokens).
v0.2.0: Token registry with named tokens, scopes, expiry, and revocation.
        Legacy tokens (primary + AUTH_TOKENS) continue working as admin-scoped.

- Token is secrets.token_urlsafe(32) (43 chars, 256 bits of entropy)
- Stored with mode 0o600 (owner-only read/write)
- Validated with secrets.compare_digest (constant-time comparison)
- Token file is in data/.token which is .gitignored
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import stat
from dataclasses import dataclass

from fastapi import HTTPException

from sandclaude.config import settings

logger = logging.getLogger(__name__)

_cached_token: str | None = None


@dataclass
class AuthResult:
    """Result of token verification — who is this caller and what can they do."""

    token: str
    fingerprint: str
    is_legacy: bool  # True for primary/.env tokens (admin-scoped)
    scopes: list[str]  # empty for legacy (= all scopes)
    token_name: str | None = None  # name from registry, None for legacy
    # v0.3.0: User identity (populated from token → user lookup)
    user_id: int | None = None
    username: str | None = None
    display_name: str | None = None

    def has_scope(self, scope: str) -> bool:
        """Check if this auth result grants the given scope.

        Legacy tokens (primary + AUTH_TOKENS) have all scopes.
        Registry tokens must have the specific scope.
        """
        if self.is_legacy:
            return True
        return scope in self.scopes


def init_token() -> str:
    """Generate or load the Bearer token. Returns the token string."""
    global _cached_token
    token_path = settings.data_dir / ".token"
    token_path.parent.mkdir(parents=True, exist_ok=True)

    if token_path.exists():
        # S14: Verify file permissions on load
        if os.name != "nt":
            mode = token_path.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
                logger.warning(
                    "Token file %s has insecure permissions (mode %s). Fixing to 0o600.",
                    token_path,
                    oct(mode),
                )
                token_path.chmod(0o600)
        loaded = token_path.read_text().strip()
        if not loaded or len(loaded) < 16:
            logger.warning(
                "Token file %s is empty or too short. Regenerating token.",
                token_path,
            )
            loaded = secrets.token_urlsafe(32)
            token_path.write_text(loaded)
            token_path.chmod(0o600)
        _cached_token = loaded
    else:
        _cached_token = secrets.token_urlsafe(32)
        token_path.write_text(_cached_token)
        token_path.chmod(0o600)

    return _cached_token


def verify_token(provided: str) -> None:
    """Raise 401 if the provided token is invalid.

    S16: Always compare against ALL candidates to avoid timing side-channel
    that leaks which token position matched.

    This is the v0.1.0 compatibility shim — checks legacy tokens only.
    For scope-aware auth, use verify_token_with_scopes().
    """
    if _cached_token is None:
        raise HTTPException(status_code=500, detail="Auth not initialized")

    candidates: list[str] = [_cached_token]
    if settings.auth_tokens:
        for token in settings.auth_tokens.split(","):
            t = token.strip()
            if t:
                candidates.append(t)

    # Compare against every candidate (no short-circuit) to prevent timing leaks
    valid = False
    for candidate in candidates:
        if secrets.compare_digest(provided, candidate):
            valid = True
        # Do NOT break - always check all candidates

    if not valid:
        raise HTTPException(status_code=401, detail="Invalid token")


async def verify_token_with_scopes(provided: str) -> AuthResult:
    """Verify a token and return its identity + scopes.

    Check order:
    1. Legacy tokens (primary + AUTH_TOKENS) — admin-scoped, backward compatible
    2. Registry tokens — scoped, with expiry and revocation checks

    Always compares against ALL candidates (constant-time).
    """
    if _cached_token is None:
        raise HTTPException(status_code=500, detail="Auth not initialized")

    # Phase 1: Check legacy tokens (constant-time, check all)
    legacy_candidates: list[str] = [_cached_token]
    if settings.auth_tokens:
        for token in settings.auth_tokens.split(","):
            t = token.strip()
            if t:
                legacy_candidates.append(t)

    legacy_match = False
    for candidate in legacy_candidates:
        if secrets.compare_digest(provided, candidate):
            legacy_match = True
        # Do NOT break

    if legacy_match:
        # v0.3.0: Resolve legacy tokens to the bootstrap admin user
        admin = None
        try:
            from sandclaude.db import store as db

            admin = await db.get_user_by_username("admin")
        except Exception:
            pass  # DB not initialized yet (e.g., during tests)
        return AuthResult(
            token=provided,
            fingerprint=token_fingerprint(provided),
            is_legacy=True,
            scopes=[],
            user_id=admin.id if admin else None,
            username=admin.username if admin else None,
            display_name=admin.display_name if admin else None,
        )

    # Phase 2: Check registry tokens
    from sandclaude.db import store as db

    fp = token_fingerprint(provided)
    token_info = await db.get_token_by_hash(fp)

    if token_info is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not token_info.is_active():
        detail = "Token revoked" if token_info.revoked_at else "Token expired"
        raise HTTPException(status_code=401, detail=detail)

    # v0.3.0: Resolve token to its owning user
    user_id = token_info.user_id
    username = None
    display_name = None
    if user_id is not None:
        user = await db.get_user(user_id)
        if user:
            username = user.username
            display_name = user.display_name

    return AuthResult(
        token=provided,
        fingerprint=fp,
        is_legacy=False,
        scopes=token_info.scopes,
        token_name=token_info.name,
        user_id=user_id,
        username=username,
        display_name=display_name,
    )


def require_scope(auth: AuthResult, scope: str) -> None:
    """Raise 403 if the auth result does not grant the required scope."""
    if not auth.has_scope(scope):
        raise HTTPException(
            status_code=403,
            detail=f"Token lacks required scope: {scope}",
        )


def generate_token() -> str:
    """Generate a new random token string."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# v0.2.0: Signed approval links (short-lived, single-purpose)
# ---------------------------------------------------------------------------

_APPROVAL_LINK_TTL_S = 3600  # 1 hour


def create_approval_link_token(
    task_id: str, action: str, *, ttl_s: int = _APPROVAL_LINK_TTL_S
) -> str:
    """Create a signed, time-limited token for an approval link.

    The token encodes: task_id, action, expiry. It is signed with HMAC
    using the server's primary token as the key. This token does NOT
    grant general API access — it can only be used to render the
    approval page and make approve/reject calls for this specific
    task+action pair.
    """
    import hmac
    import time

    expiry = int(time.time()) + ttl_s
    payload = f"{task_id}:{action}:{expiry}"
    key = get_token().encode()
    sig = hmac.new(key, payload.encode(), "sha256").hexdigest()[:32]
    return f"{payload}:{sig}"


def verify_approval_link_token(token: str, expected_task_id: str, expected_action: str) -> bool:
    """Verify a signed approval link token.

    Returns True if the token is valid, not expired, and matches the
    expected task_id and action.
    """
    import hmac
    import time

    parts = token.split(":")
    if len(parts) != 4:
        return False

    task_id, action, expiry_str, sig = parts
    if task_id != expected_task_id or action != expected_action:
        return False

    try:
        expiry = int(expiry_str)
    except ValueError:
        return False

    if time.time() > expiry:
        return False

    payload = f"{task_id}:{action}:{expiry_str}"
    key = get_token().encode()
    expected_sig = hmac.new(key, payload.encode(), "sha256").hexdigest()[:32]

    return hmac.compare_digest(sig, expected_sig)


def get_token() -> str:
    """Return the current token. Raises if not initialized."""
    if _cached_token is None:
        raise RuntimeError("Auth not initialized - call init_token() first")
    return _cached_token


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible fingerprint used for task ownership checks."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# v0.3.0: Session cookies for GitHub OAuth
# ---------------------------------------------------------------------------


def create_session_cookie(user_id: int, username: str, max_age_s: int = 28800) -> str:
    """Create a signed session cookie value. Default 8h TTL."""
    import json
    import time

    payload = {
        "user_id": user_id,
        "username": username,
        "exp": int(time.time()) + max_age_s,
    }
    data = json.dumps(payload, separators=(",", ":"))
    import hmac

    key = get_token().encode("utf-8")
    sig = hmac.new(key, data.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def verify_session_cookie(cookie: str) -> AuthResult | None:
    """Verify a signed session cookie. Returns AuthResult or None."""
    import json
    import time

    try:
        parts = cookie.rsplit(".", 1)
        if len(parts) != 2:
            return None
        data, sig = parts

        import hmac

        key = get_token().encode("utf-8")
        expected = hmac.new(key, data.encode("utf-8"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None

        payload = json.loads(data)
        if payload.get("exp", 0) < int(time.time()):
            return None

        return AuthResult(
            token="",  # no raw token for session auth
            fingerprint="",
            is_legacy=False,
            scopes=["tasks:approve", "tasks:read"],  # limited scope for sessions
            token_name=None,
            user_id=payload.get("user_id"),
            username=payload.get("username"),
            display_name=payload.get("username"),  # display_name = username for session
        )
    except Exception:
        return None
