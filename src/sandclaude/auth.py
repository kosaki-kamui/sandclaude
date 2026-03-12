"""
SECURITY-CRITICAL: Bearer token authentication.

- Token is secrets.token_urlsafe(32) (43 chars, 256 bits of entropy)
- Stored with mode 0o600 (owner-only read/write)
- Validated with secrets.compare_digest (constant-time comparison)
- Token file is in data/.token which is .gitignored
"""

import hashlib
import logging
import os
import secrets
import stat

from fastapi import HTTPException

from sandclaude.config import settings

logger = logging.getLogger(__name__)

_cached_token: str | None = None


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


def get_token() -> str:
    """Return the current token. Raises if not initialized."""
    if _cached_token is None:
        raise RuntimeError("Auth not initialized - call init_token() first")
    return _cached_token


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible fingerprint used for task ownership checks."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
