"""
sandclaude API Server — FastAPI application assembly.

Routes are organized into domain-specific router modules:
- system.py     — /health, /pool
- tasks.py      — task CRUD, artifacts, retry, bundle, streaming
- approvals.py  — approval gates + browser-based approval UI
- prs.py        — PR creation endpoints
- tokens.py     — scoped token management
- policies.py   — policy preset CRUD
- review.py     — risk summary, AI review, secrets audit
"""

from __future__ import annotations

import logging
import os as _os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sandclaude.api.approvals import router as approvals_router
from sandclaude.api.deps import CREATE_RATE_LIMIT_MAX_REQUESTS, _create_rate_buckets
from sandclaude.api.policies import router as policies_router
from sandclaude.api.prs import router as prs_router
from sandclaude.api.review import router as review_router
from sandclaude.api.system import router as system_router
from sandclaude.api.tasks import router as tasks_router
from sandclaude.api.tokens import router as tokens_router
from sandclaude.api.users import router as users_router
from sandclaude.auth import init_token
from sandclaude.config import settings
from sandclaude.db import store as db
from sandclaude.runner.container import recover_orphans

# Re-export for backward compatibility with tests that import these directly
__all__ = ["app", "CREATE_RATE_LIMIT_MAX_REQUESTS", "_create_rate_buckets"]

logger = logging.getLogger(__name__)


async def _ensure_admin_user():
    """Ensure the bootstrap admin user exists and is linked to the primary token."""

    admin = await db.get_user_by_username("admin")
    if admin:
        # Link any orphan tokens (e.g., created before users table existed)
        linked = await db.link_orphan_tokens_to_user(admin.id)
        if linked:
            logger.info("Linked %d orphan token(s) to admin user", linked)
        return admin

    # Create admin user (self-referential created_by set after)
    admin = await db.create_user(
        username="admin",
        display_name="Admin",
        is_service_account=False,
        created_by_user_id=None,
    )
    await db.update_user(admin.id, created_by_user_id=admin.id)

    # Link any existing registry tokens to the admin
    linked = await db.link_orphan_tokens_to_user(admin.id)
    if linked:
        logger.info("Linked %d existing token(s) to admin user", linked)

    return admin


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: init DB, auth, recover orphans."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required. Set it in .env or environment.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await db.init_db()
    init_token()
    # v0.2.0: Seed built-in policy presets
    from sandclaude.presets import seed_builtin_presets

    seeded = await seed_builtin_presets()
    if seeded:
        logger.info("Seeded %d built-in policy preset(s)", seeded)
    # v0.3.0: Ensure the bootstrap admin user exists
    admin = await _ensure_admin_user()
    logger.info("Admin user: %s (id=%d)", admin.username, admin.id)
    logger.info("Data directory: %s", settings.data_dir.resolve())
    # Block multi-worker startup — pool and rate limit state is process-local.
    # Check both WEB_CONCURRENCY env var and uvicorn --workers CLI arg.
    web_concurrency = _os.environ.get("WEB_CONCURRENCY", "1")
    if web_concurrency != "1":
        raise RuntimeError(
            f"WEB_CONCURRENCY={web_concurrency} is not supported. "
            "sandclaude requires a single worker because pool scheduling, "
            "rate limiting, and background task state are process-local. "
            "Remove WEB_CONCURRENCY or set it to 1."
        )
    import sys as _sys

    argv = _sys.argv
    for i, arg in enumerate(argv):
        # Handle both --workers N and --workers=N forms
        workers_val: str | None = None
        if arg == "--workers" and i + 1 < len(argv):
            workers_val = argv[i + 1]
        elif arg.startswith("--workers="):
            workers_val = arg.split("=", 1)[1]
        if workers_val is not None and workers_val != "1":
            raise RuntimeError(
                f"--workers {workers_val} is not supported. "
                "sandclaude requires a single worker. Remove --workers or set it to 1."
            )
    try:
        await recover_orphans()
    except Exception as exc:
        logger.warning("Orphan recovery failed: %s", exc)
    if settings.task_retention_days > 0:
        count = await db.cleanup_old_tasks(settings.task_retention_days)
        if count:
            days = settings.task_retention_days
            logger.info("Cleaned up %d tasks older than %dd", count, days)
    yield


app = FastAPI(title="sandclaude", version="0.2.5", lifespan=lifespan)

# Register routers (no prefix — all routes stay at their current paths)
app.include_router(system_router)
app.include_router(tasks_router)
app.include_router(prs_router)
app.include_router(approvals_router)
app.include_router(tokens_router)
app.include_router(policies_router)
app.include_router(review_router)
app.include_router(users_router)
