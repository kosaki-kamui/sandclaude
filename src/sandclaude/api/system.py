"""System routes: health check, pool stats, metrics, and deployment doctor."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends

from sandclaude.api.deps import _require_auth
from sandclaude.auth import AuthResult, require_scope
from sandclaude.config import settings
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


@router.get("/admin/doctor")
async def doctor_endpoint(auth: AuthResult = Depends(_require_auth)) -> dict:
    """Run deployment health checks and report issues.

    Returns a list of checks with pass/warn/fail status and actionable messages.
    """
    require_scope(auth, "admin:policies")
    checks: list[dict] = []

    # 1. Docker reachability
    checks.append(await _check_docker())

    # 2. Runner image exists
    checks.append(await _check_runner_image())

    # 3. gh CLI availability
    checks.append(_check_gh_cli())

    # 4. Template files present
    checks.append(_check_templates())

    # 5. Anthropic API key
    checks.append(_check_api_key())

    # 6. Data directory writable
    checks.append(_check_data_dir())

    # 7. GitHub OAuth config
    checks.append(_check_github_oauth())

    # 8. Network isolation config
    checks.append(_check_network_isolation())

    passed = sum(1 for c in checks if c["status"] == "pass")
    warned = sum(1 for c in checks if c["status"] == "warn")
    failed = sum(1 for c in checks if c["status"] == "fail")

    return {
        "summary": {"passed": passed, "warned": warned, "failed": failed},
        "checks": checks,
    }


async def _check_docker() -> dict:
    """Check Docker daemon is reachable."""
    try:
        import docker

        client = docker.from_env()
        await asyncio.to_thread(client.ping)
        return {"name": "docker", "status": "pass", "message": "Docker daemon reachable"}
    except Exception as exc:
        return {
            "name": "docker",
            "status": "fail",
            "message": f"Cannot reach Docker: {exc}",
        }


async def _check_runner_image() -> dict:
    """Check sandclaude-runner image exists."""
    try:
        import docker

        client = docker.from_env()
        await asyncio.to_thread(client.images.get, "sandclaude-runner")
        return {
            "name": "runner_image",
            "status": "pass",
            "message": "sandclaude-runner image found",
        }
    except Exception:
        return {
            "name": "runner_image",
            "status": "fail",
            "message": (
                "sandclaude-runner image not found. "
                "Run: docker build -t sandclaude-runner -f Dockerfile.runner ."
            ),
        }


def _check_gh_cli() -> dict:
    """Check gh CLI is installed (needed for PR creation)."""
    if shutil.which("gh"):
        return {"name": "gh_cli", "status": "pass", "message": "gh CLI found"}
    return {
        "name": "gh_cli",
        "status": "warn",
        "message": "gh CLI not found. PR creation will fail. Install: https://cli.github.com",
    }


def _check_templates() -> dict:
    """Check HTML templates are included in the package."""
    template_dir = Path(__file__).parent.parent / "templates"
    approve_html = template_dir / "approve.html"
    if approve_html.exists():
        return {
            "name": "templates",
            "status": "pass",
            "message": "Approval UI template found",
        }
    return {
        "name": "templates",
        "status": "fail",
        "message": "templates/approve.html missing. Reinstall sandclaude or check package-data.",
    }


def _check_api_key() -> dict:
    """Check Anthropic API key is configured."""
    if settings.anthropic_api_key:
        # Mask the key for display
        key = settings.anthropic_api_key
        masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"
        return {
            "name": "api_key",
            "status": "pass",
            "message": f"ANTHROPIC_API_KEY set ({masked})",
        }
    return {
        "name": "api_key",
        "status": "fail",
        "message": "ANTHROPIC_API_KEY is not set",
    }


def _check_data_dir() -> dict:
    """Check data directory exists and is writable."""
    data_dir = settings.data_dir
    if not data_dir.exists():
        return {
            "name": "data_dir",
            "status": "fail",
            "message": f"Data directory does not exist: {data_dir}",
        }
    token_path = data_dir / ".token"
    if not token_path.exists():
        return {
            "name": "data_dir",
            "status": "fail",
            "message": f"Auth token not found at {token_path}",
        }
    return {
        "name": "data_dir",
        "status": "pass",
        "message": f"Data directory OK: {data_dir.resolve()}",
    }


def _check_github_oauth() -> dict:
    """Check GitHub OAuth configuration."""
    if settings.github_client_id and settings.github_client_secret:
        return {
            "name": "github_oauth",
            "status": "pass",
            "message": "GitHub OAuth configured",
        }
    if settings.github_client_id and not settings.github_client_secret:
        return {
            "name": "github_oauth",
            "status": "fail",
            "message": "GITHUB_CLIENT_ID set but GITHUB_CLIENT_SECRET missing",
        }
    return {
        "name": "github_oauth",
        "status": "warn",
        "message": "GitHub OAuth not configured. Approval UI will use token-paste only.",
    }


def _check_network_isolation() -> dict:
    """Check network isolation configuration."""
    if settings.skip_network_isolation:
        env = settings.environment.strip().lower()
        if env in {"dev", "development", "test"}:
            return {
                "name": "network_isolation",
                "status": "warn",
                "message": f"Network isolation disabled (SKIP_NETWORK_ISOLATION=true, env={env})",
            }
        return {
            "name": "network_isolation",
            "status": "fail",
            "message": "SKIP_NETWORK_ISOLATION=true in production is not allowed",
        }
    return {
        "name": "network_isolation",
        "status": "pass",
        "message": "Network isolation enabled",
    }
