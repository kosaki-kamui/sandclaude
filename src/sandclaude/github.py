"""
GitHub PR creation from completed task diffs.

Uses `gh` CLI for PR creation and GIT_TOKEN (via GH_TOKEN) for authentication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime

from sandclaude.config import settings
from sandclaude.models import Task

logger = logging.getLogger(__name__)


async def create_pr(task: Task, *, title: str | None = None) -> dict:
    """Create a PR from a completed task. Returns {branch, url, title}."""
    data_dir = settings.data_dir
    diff_path = data_dir / "tasks" / task.id / "diff.patch"
    audit_path = data_dir / "tasks" / task.id / "audit.json"

    if not diff_path.exists():
        raise RuntimeError("No diff available for this task")

    diff = diff_path.read_text()
    if not diff.strip():
        raise RuntimeError("Diff is empty - no changes to create a PR from")

    audit: dict = {}
    if audit_path.exists():
        audit = json.loads(audit_path.read_text())

    branch_name = f"sandclaude/{task.id}"

    # Generate AI commit/PR title from the diff if no explicit title provided
    ai_title = None
    if not title:
        ai_title = await _generate_ai_commit_title(task.prompt, diff)
    pr_title = title or ai_title or f"sandclaude: task {task.id}"

    # Generate AI summary for the PR body
    ai_summary = await _generate_ai_pr_summary(task.prompt, diff)

    # v0.2.0: Generate risk summary
    from sandclaude.risk import (
        format_risk_summary_markdown,
        generate_risk_summary,
    )

    risk = generate_risk_summary(
        diff, audit,
        tokens_input=task.tokens_input or 0,
        tokens_output=task.tokens_output or 0,
        cost_usd=task.total_cost_usd or 0.0,
    )
    risk_md = format_risk_summary_markdown(risk)

    pr_body = _build_pr_body(
        task, audit, diff, ai_summary=ai_summary, risk_summary=risk_md,
    )

    # Resolve repo working directory
    cwd, tmp_dir = await _resolve_repo_dir(task)

    # For local repos, use a git worktree so we never mutate the main working
    # tree. This prevents leaving stale branches checked out and avoids
    # interfering with other automation or operators using the repo.
    worktree_dir: str | None = None
    work_cwd = cwd

    try:
        is_local = tmp_dir is None  # local repo, not a temp clone

        if is_local:
            worktree_dir = tempfile.mkdtemp(prefix="sandclaude-wt-")
            await _run_git(["worktree", "add", "-b", branch_name, worktree_dir], cwd=cwd)
            work_cwd = worktree_dir
        else:
            await _run_git(["checkout", "-b", branch_name], cwd=work_cwd)

        # Apply the diff
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "apply", "--allow-empty"],
            cwd=work_cwd,
            input=diff,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git apply failed: {proc.stderr}")

        await _run_git(["add", "-A"], cwd=work_cwd)

        # Ensure git identity is configured for the commit
        await _run_git(["config", "user.name", "sandclaude"], cwd=work_cwd)
        await _run_git(["config", "user.email", "bot@sandclaude.local"], cwd=work_cwd)

        commit_msg = _build_commit_message(task, audit, diff, pr_title)
        await _run_git(["commit", "-m", commit_msg], cwd=work_cwd)

        with _GitCredentialHelper() as env:
            await _run_git(["push", "-u", "origin", branch_name], cwd=work_cwd, env=env)

        # Create PR via gh CLI
        if not shutil.which("gh"):
            raise RuntimeError(
                "GitHub PR creation requires the gh CLI. Install it: https://cli.github.com"
            )

        # gh CLI uses GH_TOKEN env var for authentication
        gh_env = {**os.environ}
        if settings.git_token:
            gh_env["GH_TOKEN"] = settings.git_token
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                "gh",
                "pr",
                "create",
                "--title",
                pr_title,
                "--body",
                pr_body,
                "--head",
                branch_name,
            ],
            cwd=work_cwd,
            capture_output=True,
            timeout=120,
            text=True,
            env=gh_env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {proc.stderr}")

        pr_url = proc.stdout.strip()
        return {"branch": branch_name, "url": pr_url, "title": pr_title}

    except Exception:
        # Cleanup branch on failure (only for non-worktree path)
        if not worktree_dir:
            try:
                await _run_git(["checkout", "-"], cwd=work_cwd)
                await _run_git(["branch", "-D", branch_name], cwd=work_cwd)
            except Exception:
                pass
        raise

    finally:
        # Clean up worktree (removes the worktree dir and prunes the ref)
        if worktree_dir:
            try:
                await _run_git(["worktree", "remove", "--force", worktree_dir], cwd=cwd)
            except Exception:
                shutil.rmtree(worktree_dir, ignore_errors=True)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _resolve_repo_dir(task: Task) -> tuple[str, str | None]:
    """Resolve repo to a local directory. Returns (cwd, tmp_dir_or_None).

    For local paths, mirrors the same production enforcement as container.py:
    in production, client-provided host_cwd is ignored and only settings.host_cwd
    is used. The resolved path is validated before any git operations.
    """
    from sandclaude.runner.container import _validate_local_path

    repo = task.repo

    # Remote repo - clone to temp dir (only secure transports)
    if repo.startswith("http://"):
        raise RuntimeError("Plaintext http:// Git URLs are not allowed. Use https:// or git@.")
    if repo.startswith(("https://", "git@")):
        tmp_dir = tempfile.mkdtemp(prefix="sandclaude-pr-")
        # Configure git credentials for private repo cloning
        with _GitCredentialHelper() as env:
            branch_args = ["--branch", task.branch] if task.branch else []
            await _run_git(["clone", "--depth", "50", *branch_args, repo, tmp_dir], env=env)
        return tmp_dir, tmp_dir

    # Local path — mirror container.py's production enforcement (S2)
    if repo == ".":
        if settings.environment.strip().lower() == "production":
            local_path = settings.host_cwd or os.getcwd()
        else:
            local_path = task.host_cwd or settings.host_cwd or os.getcwd()
    else:
        local_path = os.path.abspath(repo)

    # Validate before running any git commands in this path
    _validate_local_path(local_path)

    # Verify git repo
    proc = await asyncio.to_thread(
        subprocess.run,
        ["git", "rev-parse", "--git-dir"],
        cwd=local_path,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'"{local_path}" is not a git repository')

    return local_path, None


async def _run_git(
    args: list[str],
    cwd: str | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> str:
    run_env = None
    if env:
        run_env = {**os.environ, **env}
    proc = await asyncio.to_thread(
        subprocess.run,
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=run_env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


class _GitCredentialHelper:
    """Context manager that creates a secure GIT_ASKPASS script and cleans it up.

    Uses mkstemp() (not mktemp()) to avoid TOCTOU race conditions, sets strict
    permissions (0o700), and guarantees cleanup via __exit__.
    """

    def __init__(self) -> None:
        self._path: str | None = None

    def __enter__(self) -> dict[str, str] | None:
        token = settings.git_token
        if not token:
            return None
        fd, self._path = tempfile.mkstemp(prefix="sandclaude-askpass-", suffix=".sh")
        try:
            os.fchmod(fd, 0o700)
            # Use shlex.quote to prevent shell injection from token values
            # containing metacharacters ($, `, ", newlines, etc.)
            os.write(fd, f"#!/bin/sh\necho {shlex.quote(token)}\n".encode())
        finally:
            os.close(fd)
        return {
            "GIT_ASKPASS": self._path,
            "GIT_TERMINAL_PROMPT": "0",
        }

    def __exit__(self, *exc: object) -> None:
        if self._path:
            try:
                os.unlink(self._path)
            except OSError:
                pass


async def _generate_ai_commit_title(prompt: str, diff: str) -> str | None:
    """Use Claude to generate a concise commit message title from the diff.

    Returns None on any failure (network, auth, timeout) — callers should
    fall back to the template-based title.
    """
    if not settings.anthropic_api_key:
        return None

    # Truncate diff to avoid excessive token usage (~4K chars is enough context)
    diff_excerpt = diff[:4000]
    if len(diff) > 4000:
        diff_excerpt += "\n... (truncated)"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 100,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Write a single-line git commit message "
                                "(max 72 chars) for this change. "
                                "Use conventional commit format "
                                "(feat:, fix:, refactor:, docs:, etc). "
                                "Be specific about what changed. "
                                "Output ONLY the commit message.\n\n"
                                f"Task prompt: {prompt[:500]}\n\n"
                                f"Diff:\n{diff_excerpt}"
                            ),
                        }
                    ],
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content", [])
            if content and content[0].get("type") == "text":
                title = content[0]["text"].strip().strip('"').strip("'")
                # Enforce 72 char limit and single line
                title = title.split("\n")[0][:72]
                if title:
                    return title
    except Exception as exc:
        logger.info("AI commit title generation failed (falling back to template): %s", exc)

    return None


async def _generate_ai_pr_summary(prompt: str, diff: str) -> str | None:
    """Use Claude to generate a human-readable summary of the changes for the PR body.

    Returns None on any failure — callers fall back to the template-only body.
    """
    if not settings.anthropic_api_key:
        return None

    diff_excerpt = diff[:6000]
    if len(diff) > 6000:
        diff_excerpt += "\n... (truncated)"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Write a concise PR description summarizing what this diff does "
                                "and why. Use markdown. Start with a 1-2 sentence overview, then "
                                "bullet points for key changes. Do NOT include a title line. "
                                "Do NOT repeat the file list or metadata — just explain the "
                                "substance of the changes in plain language a reviewer would "
                                "find useful. Keep it under 200 words.\n\n"
                                f"Task prompt: {prompt[:500]}\n\n"
                                f"Diff:\n{diff_excerpt}"
                            ),
                        }
                    ],
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content", [])
            if content and content[0].get("type") == "text":
                summary = content[0]["text"].strip()
                if summary:
                    return summary
    except Exception as exc:
        logger.info("AI PR summary generation failed (using template only): %s", exc)

    return None


def _extract_changed_files(diff: str) -> list[str]:
    """Extract file paths from a unified diff."""
    files = []
    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            m = re.search(r"b/(.+)$", line)
            if m:
                files.append(m.group(1))
    return files


def _calc_duration_str(task: Task) -> str:
    if task.started_at and task.completed_at:
        try:
            s = datetime.fromisoformat(task.started_at.replace("Z", "+00:00"))
            e = datetime.fromisoformat(task.completed_at.replace("Z", "+00:00"))
            secs = (e - s).total_seconds()
            if secs >= 60:
                return f"{secs / 60:.1f}m"
            return f"{secs:.0f}s"
        except Exception:
            pass
    return "unknown"


def _build_commit_message(task: Task, audit: dict, diff: str, pr_title: str) -> str:
    """Build a detailed commit message with context about what was done and why."""
    files_changed = _extract_changed_files(diff)
    duration = _calc_duration_str(task)

    lines = [pr_title, ""]

    # Always include the prompt — this is the user's own commit, not a webhook
    prompt_excerpt = task.prompt[:500]
    if len(task.prompt) > 500:
        prompt_excerpt += "..."
    lines.append(f"Prompt: {prompt_excerpt}")
    lines.append("")

    # Summary of changes
    if files_changed:
        lines.append(f"Changed {len(files_changed)} file(s):")
        for f in files_changed[:20]:
            lines.append(f"  - {f}")
        if len(files_changed) > 20:
            lines.append(f"  ... and {len(files_changed) - 20} more")
        lines.append("")

    # Commands executed (if any, shows what the agent did)
    commands = audit.get("commands_executed", [])
    if commands:
        lines.append(f"Commands executed: {len(commands)}")
        for cmd in commands[:10]:
            # Truncate long commands
            display = cmd[:120] + ("..." if len(cmd) > 120 else "")
            lines.append(f"  $ {display}")
        if len(commands) > 10:
            lines.append(f"  ... and {len(commands) - 10} more")
        lines.append("")

    # Metadata footer
    cost_str = f"${task.total_cost_usd:.4f}" if task.total_cost_usd is not None else "?"
    lines.append(f"Model: {task.model} | Duration: {duration} | Cost: {cost_str}")
    lines.append(f"Task: {task.id}")
    lines.append("")
    lines.append("Generated by sandclaude")

    return "\n".join(lines)


def _build_pr_body(
    task: Task, audit: dict, diff: str, *,
    ai_summary: str | None = None,
    risk_summary: str | None = None,
) -> str:
    files_changed = _extract_changed_files(diff)
    duration = _calc_duration_str(task)

    net_reqs = audit.get("network_requests", [])
    blocked = [r for r in net_reqs if not r.get("allowed")]
    commands = audit.get("commands_executed", [])

    parts = [
        "## Summary",
        "",
        f"> {task.prompt[:500]}{'...' if len(task.prompt) > 500 else ''}",
        "",
    ]

    if ai_summary:
        parts.extend([ai_summary, ""])

    parts.extend(
        [
            f"**Model:** {task.model}",
            f"**Duration:** {duration}",
            f"**Tokens:** {task.tokens_input or '?'} input / {task.tokens_output or '?'} output",
            (
                f"**Estimated cost:** ${task.total_cost_usd:.4f}"
                if task.total_cost_usd is not None
                else "**Estimated cost:** ?"
            ),
            "",
            "## Changes",
            "",
        ]
    )

    for f in files_changed:
        parts.append(f"- `{f}`")

    # v0.2.0: Risk summary
    if risk_summary:
        parts.extend(["", risk_summary])

    parts.extend(["", "## Audit Trail", ""])

    # File activity
    files_read = audit.get("files_read", [])
    files_written = audit.get("files_written", [])
    parts.append(f"- **Files read:** {len(files_read)}")
    parts.append(f"- **Files written:** {len(files_written)}")

    # Commands (show the actual commands for reviewability)
    if commands:
        parts.append(f"- **Commands executed:** {len(commands)}")
        parts.append("")
        parts.append("<details><summary>Commands run by the agent</summary>")
        parts.append("")
        parts.append("```bash")
        for cmd in commands:
            parts.append(cmd)
        parts.append("```")
        parts.append("</details>")
    else:
        parts.append("- **Commands executed:** 0")

    # Network
    parts.append("")
    if blocked:
        parts.append(f"**{len(blocked)} network request(s) were blocked by iptables.**")
    else:
        parts.append("No blocked network requests.")

    # Warnings from diff capture
    warnings = audit.get("warnings", [])
    if warnings:
        parts.extend(["", "### Warnings", ""])
        for w in warnings:
            parts.append(f"- {w}")

    parts.extend(
        [
            "",
            "---",
            f"*Generated by [sandclaude](https://github.com/kosaki-kamui/sandclaude) "
            f"task `{task.id}`. All changes require human review before merge.*",
        ]
    )

    return "\n".join(parts)
