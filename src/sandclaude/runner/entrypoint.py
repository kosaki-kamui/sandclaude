"""
Runner Entrypoint - runs inside the Docker container.

Lifecycle:
1. SETUP PHASE: Clone repo (or copy from bind mount). No dep install - Claude handles it.
2. Wait for network switch signal from API server
3. AGENT PHASE: Run Claude Agent SDK with network access to allowed domains.
   Claude determines what dependencies to install and installs them itself.
4. OUTPUT: Write diff, audit log, transcript to /output
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Allow nested Claude Code sessions
os.environ.pop("CLAUDECODE", None)

WORKSPACE = Path("/workspace")
OUTPUT_DIR = Path("/output")


async def main() -> None:
    task_id = _require_env("TASK_ID")
    prompt = _require_env("TASK_PROMPT")
    model = os.environ.get("TASK_MODEL", "claude-sonnet-4-5")
    max_turns = int(os.environ.get("TASK_MAX_TURNS", "50"))
    repo_url = os.environ.get("REPO_URL")
    repo_branch = os.environ.get("REPO_BRANCH")
    is_local = os.environ.get("LOCAL_REPO") == "true"
    allowed_domains = os.environ.get("ALLOWED_DOMAINS", "api.anthropic.com")

    print(f"[runner] Task {task_id} starting")
    print(f"[runner] Model: {model}, Max turns: {max_turns}")
    print(f"[runner] Allowed domains: {allowed_domains}")

    # ── SETUP PHASE ────────────────────────────────────────────
    # Setup only clones/copies the repo. Dependency installation is handled by
    # Claude during the agent phase, using the allowed_domains network access.
    print("[runner] === SETUP PHASE ===")
    try:
        if is_local:
            print("[runner] Copying local repo from /workspace-source...")
            if WORKSPACE.exists():
                shutil.rmtree(WORKSPACE)
            # S12: Don't follow symlinks to prevent reading outside the repo
            shutil.copytree(
                "/workspace-source",
                str(WORKSPACE),
                symlinks=True,
                dirs_exist_ok=True,
            )

            # Ensure it's a git repo so diffs work
            if not (WORKSPACE / ".git").exists():
                print("[runner] Initializing git repository for local workspace...")
                subprocess.run(
                    ["git", "init", "-b", "main"],
                    cwd=str(WORKSPACE),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "sandclaude"],
                    cwd=str(WORKSPACE),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "bot@sandclaude.local"],
                    cwd=str(WORKSPACE),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "add", "."], cwd=str(WORKSPACE), check=True, capture_output=True
                )
                subprocess.run(
                    ["git", "commit", "-m", "Initial setup commit"],
                    cwd=str(WORKSPACE),
                    check=True,
                    capture_output=True,
                )
        elif repo_url:
            if repo_url.startswith("http://"):
                print("[runner] ERROR: Plaintext http:// Git URLs are not allowed")
                sys.exit(1)
            print(f"[runner] Cloning {repo_url}...")
            git_token = os.environ.get("GIT_TOKEN")
            if git_token and repo_url.startswith("https://"):
                # Configure a one-shot credential helper that provides the token
                # for this clone only. The helper script is deleted after clone.
                _setup_git_credential_helper(git_token)
                print("[runner] Using GIT_TOKEN for private repo authentication")
            branch_args = ["--branch", repo_branch] if repo_branch else []
            subprocess.run(
                ["git", "clone", "--depth", "1", *branch_args, repo_url, str(WORKSPACE)],
                check=True,
            )
            # Scrub credential helper and token immediately after clone
            _cleanup_git_credentials()
        else:
            print("[runner] ERROR: No REPO_URL or LOCAL_REPO specified")
            sys.exit(1)
    except Exception as exc:
        print(f"[runner] Setup phase failed: {exc}")
        _write_error(task_id, exc)
        sys.exit(1)

    print("[runner] Setup phase complete (repo ready, deps deferred to agent)")

    # Signal setup done, wait for network switch
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / ".setup-complete").write_text("")
    print("[runner] Waiting for network switch...")

    deadline = asyncio.get_running_loop().time() + 30
    while not (OUTPUT_DIR / ".network-switched").exists():
        if asyncio.get_running_loop().time() > deadline:
            print("[runner] ERROR: Network switch timed out after 30s")
            _write_error(task_id, TimeoutError("Network switch timed out"))
            sys.exit(1)
        await asyncio.sleep(0.2)

    print("[runner] Network switched to agent-net")

    # ── AGENT PHASE ────────────────────────────────────────────
    # Claude has network access to api.anthropic.com + allowed_domains.
    # It will determine what dependencies are needed and install them.
    print("[runner] === AGENT PHASE ===")

    # Build an augmented prompt that tells Claude about its environment
    allowed_list = [d.strip() for d in allowed_domains.split(",") if d.strip()]
    env_context = _build_env_context(allowed_list)
    augmented_prompt = f"{env_context}\n\n{prompt}"

    try:
        from sandclaude.runner.executor import execute_task

        # S1: Clear non-essential sensitive env vars before dropping privileges.
        # ANTHROPIC_API_KEY must remain available — the SDK reads it at query()
        # call time, not at import time. We scrub it after execution completes.
        _scrub_env_var("GIT_TOKEN")

        _drop_privileges_for_agent()
        result = await execute_task(
            task_id=task_id,
            prompt=augmented_prompt,
            cwd=str(WORKSPACE),
            model=model,
            max_turns=max_turns,
            allowed_domains=allowed_list,
            on_message=lambda msg: print(f"[agent] {getattr(msg, 'type', '?')}"),
        )

        # S1: Now that execution is done, scrub the API key
        _scrub_env_var("ANTHROPIC_API_KEY")

        # ── OUTPUT ─────────────────────────────────────────────
        print("[runner] === OUTPUT ===")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        (OUTPUT_DIR / "result.json").write_text(
            json.dumps(
                {
                    "success": result.success,
                    "num_turns": result.num_turns,
                    "total_cost_usd": result.total_cost_usd,
                    "tokens_input": result.tokens_input,
                    "tokens_output": result.tokens_output,
                    "duration_ms": result.duration_ms,
                    "duration_api_ms": result.duration_api_ms,
                    "stop_reason": result.stop_reason,
                    "completion_reason": result.completion_reason,
                    "error": result.error,
                },
                indent=2,
            )
        )

        _write_artifact(OUTPUT_DIR / "diff.patch", result.diff)
        _write_artifact(OUTPUT_DIR / "audit.json", result.audit.model_dump_json(indent=2))
        _write_artifact(
            OUTPUT_DIR / "transcript.json",
            json.dumps([e.model_dump() for e in result.transcript], indent=2),
        )

        print(f"[runner] Task {task_id} completed. Success: {result.success}")
        print(f"[runner] Turns: {result.num_turns}, Cost: ${result.total_cost_usd:.4f}")
        sys.exit(0 if result.success else 1)

    except Exception as exc:
        print(f"[runner] Agent phase failed: {exc}")
        _write_error(task_id, exc)
        sys.exit(1)


def _build_env_context(allowed_domains: list[str]) -> str:
    """Build a system context string telling Claude about its sandbox environment."""
    non_api = [d for d in allowed_domains if d != "api.anthropic.com"]
    if non_api:
        domains_str = ", ".join(non_api)
        network_note = (
            f"You have network access to the following domains for installing "
            f"dependencies: {domains_str}. "
            f"All other outbound network access is blocked. "
            f"If the project needs dependencies, inspect the project files and "
            f"install them yourself (e.g., npm install, pip install -r requirements.txt)."
        )
    else:
        network_note = (
            "You have NO network access except to the Anthropic API. "
            "You cannot install dependencies from the internet. "
            "Work only with what is already available in the workspace."
        )

    return (
        f"[Environment: You are running inside an isolated Docker container. "
        f"Working directory: /workspace. {network_note}]"
    )


def _drop_privileges_for_agent(uid: int = 1000, gid: int = 1000) -> None:
    """Run agent phase as non-root so Claude bypass-permissions mode is allowed."""
    if os.geteuid() != 0:
        return

    try:
        subprocess.run(
            ["chown", "-R", f"{uid}:{gid}", str(WORKSPACE), str(OUTPUT_DIR)],
            check=False,
        )
    except Exception:
        pass

    home = Path("/home/agent")
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run(["chown", "-R", f"{uid}:{gid}", str(home)], check=False)
    os.environ["HOME"] = str(home)
    os.environ["USER"] = "agent"

    os.setgid(gid)
    os.setuid(uid)

    if os.geteuid() == 0:
        print("[runner] FATAL: privilege drop failed - still running as root")
        sys.exit(1)


_MAX_ARTIFACT_WRITE_BYTES = 50_000_000  # 50MB cap per artifact file


def _write_artifact(path: Path, content: str) -> None:
    """Write an artifact file with a size cap to prevent disk exhaustion."""
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_ARTIFACT_WRITE_BYTES:
        print(
            f"[runner] WARNING: Truncating {path.name} "
            f"({len(encoded)} bytes > {_MAX_ARTIFACT_WRITE_BYTES} cap)"
        )
        # Truncate at byte boundary (may break JSON, but prevents disk fill)
        encoded = encoded[:_MAX_ARTIFACT_WRITE_BYTES]
    path.write_bytes(encoded)


def _scrub_env_var(name: str) -> None:
    """Remove a sensitive env var from Python's os.environ dict.

    Note: /proc/self/environ is set at process start and is immutable —
    this does NOT clear the value from there. The real protection is that
    after privilege drop, the agent runs as uid 1000 and cannot read
    root's /proc/1/environ. This scrub prevents the value from being
    accessible via Python's os.environ or subprocess inheritance.
    """
    os.environ.pop(name, None)


_CREDENTIAL_HELPER_PATH = Path("/tmp/.git-credential-helper")


def _setup_git_credential_helper(token: str) -> None:
    """Write a temporary git credential helper script that provides the token.

    The helper responds to git's credential protocol on stdin, providing
    the token as a password for any HTTPS host. This is safer than embedding
    the token in the URL (which would persist in .git/config).
    """
    # Write a helper script that outputs credentials in git's expected format
    helper_script = f'#!/bin/sh\necho "username=x-access-token"\necho "password={token}"\necho ""\n'
    _CREDENTIAL_HELPER_PATH.write_text(helper_script)
    _CREDENTIAL_HELPER_PATH.chmod(0o700)
    # Configure git to use this helper globally (within this container)
    subprocess.run(
        ["git", "config", "--global", "credential.helper", str(_CREDENTIAL_HELPER_PATH)],
        check=True,
        capture_output=True,
    )


def _cleanup_git_credentials() -> None:
    """Remove credential helper and scrub GIT_TOKEN after clone completes."""
    # Remove the helper script
    if _CREDENTIAL_HELPER_PATH.exists():
        _CREDENTIAL_HELPER_PATH.unlink()
    # Unset the credential helper from git config
    subprocess.run(
        ["git", "config", "--global", "--unset", "credential.helper"],
        check=False,
        capture_output=True,
    )
    # Also remove from workspace-local config if it leaked
    if (WORKSPACE / ".git").exists():
        subprocess.run(
            ["git", "config", "--unset", "credential.helper"],
            cwd=str(WORKSPACE),
            check=False,
            capture_output=True,
        )
    # Scrub token from environment
    os.environ.pop("GIT_TOKEN", None)
    print("[runner] Git credentials cleaned up")


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"[runner] ERROR: Missing required env var: {name}")
        sys.exit(1)
    return val


def _write_error(task_id: str, exc: BaseException) -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "result.json").write_text(
            json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                indent=2,
            )
        )
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
