"""Tests for GitHub PR creation module."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from sandclaude.github import _resolve_repo_dir
from sandclaude.models import Task, TaskPriority, TaskStatus


@pytest.fixture
def safe_tmp():
    """Create a temp directory outside /tmp (which is in the sensitive path blocklist)."""
    home = os.path.expanduser("~")
    base = os.path.join(home, ".sandclaude-test")
    os.makedirs(base, exist_ok=True)
    d = tempfile.mkdtemp(prefix="sc-gh-", dir=base)
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _make_task(**overrides) -> Task:
    defaults = dict(
        id="task-gh",
        status=TaskStatus.completed,
        repo="https://github.com/test/repo",
        prompt="Fix the auth bug in login.py",
        model="claude-sonnet-4-5",
        max_turns=10,
        priority=TaskPriority.normal,
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:05Z",
        completed_at="2026-01-01T00:01:05Z",
        tokens_input=5000,
        tokens_output=1500,
        total_cost_usd=0.05,
    )
    defaults.update(overrides)
    return Task(**defaults)


@pytest.mark.asyncio
async def test_remote_repo_detection_http_rejected():
    """Resolver should reject plaintext http:// remote URLs."""
    task = _make_task(repo="http://github.com/test/repo")
    with pytest.raises(RuntimeError, match="http://"):
        await _resolve_repo_dir(task)


@pytest.mark.asyncio
async def test_local_repo_detection(safe_tmp):
    """Resolver should accept absolute local git repos."""
    import sandclaude.config as cfg

    repo_dir = safe_tmp / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)

    original_env = cfg.settings.environment
    try:
        cfg.settings.environment = "test"
        task = _make_task(repo=str(repo_dir))
        cwd, tmp_dir = await _resolve_repo_dir(task)
        assert cwd == str(repo_dir)
        assert tmp_dir is None
    finally:
        cfg.settings.environment = original_env


@pytest.mark.asyncio
async def test_dot_repo_resolution_uses_host_cwd_in_production(safe_tmp):
    """In production, repo='.' should resolve via settings.host_cwd."""
    import sandclaude.config as cfg

    host_repo = safe_tmp / "host-repo"
    host_repo.mkdir()
    subprocess.run(["git", "init"], cwd=host_repo, capture_output=True, check=True)

    original_env = cfg.settings.environment
    original_host_cwd = cfg.settings.host_cwd
    try:
        cfg.settings.environment = "production"
        cfg.settings.host_cwd = str(host_repo)
        task = _make_task(repo=".", host_cwd="/ignored/by/production")
        cwd, tmp_dir = await _resolve_repo_dir(task)
        assert cwd == str(host_repo)
        assert tmp_dir is None
    finally:
        cfg.settings.environment = original_env
        cfg.settings.host_cwd = original_host_cwd


def test_git_repo_validation(tmp_path):
    non_git = tmp_path / "not-git"
    non_git.mkdir()
    result = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=non_git, capture_output=True)
    assert result.returncode != 0

    git_dir = tmp_path / "is-git"
    git_dir.mkdir()
    subprocess.run(["git", "init"], cwd=git_dir, capture_output=True)
    result = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=git_dir, capture_output=True)
    assert result.returncode == 0


def test_diff_file_extraction():
    diff = "\n".join(
        [
            "diff --git a/src/auth.py b/src/auth.py",
            "--- a/src/auth.py",
            "+++ b/src/auth.py",
            "@@ -1,3 +1,5 @@",
            "+import hashlib",
            "diff --git a/src/config.py b/src/config.py",
        ]
    )
    import re

    files = []
    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            m = re.search(r"b/(.+)$", line)
            if m:
                files.append(m.group(1))
    assert files == ["src/auth.py", "src/config.py"]


def test_pr_body_duration():
    from datetime import datetime, timezone

    start = datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 1, 5, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 60
