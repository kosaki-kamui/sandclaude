"""
Task Executor - runs Claude Agent SDK in headless mode.

Primary: Claude Agent SDK (claude_agent_sdk.query)
Fallback: Claude CLI subprocess (claude --print -p "...")

Captures all messages, builds diff, audit log, and transcript.
Runs inside a Docker container.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from sandclaude.models import AuditLog, TranscriptEntry

# Try importing the Agent SDK; fall back to CLI if unavailable
_SDK_AVAILABLE = False
try:
    from claude_agent_sdk import ClaudeAgentOptions, query

    _SDK_AVAILABLE = True
except ImportError:
    ClaudeAgentOptions = None  # type: ignore[assignment, misc]
    query = None  # type: ignore[assignment]


class ExecutorResult:
    def __init__(
        self,
        *,
        success: bool,
        diff: str,
        audit: AuditLog,
        transcript: list[TranscriptEntry],
        num_turns: int = 0,
        total_cost_usd: float = 0.0,
        tokens_input: int = 0,
        tokens_output: int = 0,
        duration_ms: int = 0,
        duration_api_ms: int = 0,
        stop_reason: str | None = None,
        error: str | None = None,
    ):
        self.success = success
        self.diff = diff
        self.audit = audit
        self.transcript = transcript
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd
        self.tokens_input = tokens_input
        self.tokens_output = tokens_output
        self.duration_ms = duration_ms
        self.duration_api_ms = duration_api_ms
        self.stop_reason = stop_reason
        self.error = error


async def execute_task(
    *,
    task_id: str,
    prompt: str,
    cwd: str,
    model: str = "claude-sonnet-4-5",
    max_turns: int = 50,
    allowed_domains: list[str] | None = None,
    on_message: Callable[[Any], None] | None = None,
) -> ExecutorResult:
    """Run Claude Agent SDK headless and capture results.

    Falls back to CLI subprocess if the SDK is not installed.
    allowed_domains: domains that are allowed in iptables (for audit classification).
    """
    if _SDK_AVAILABLE:
        return await _execute_via_sdk(
            task_id=task_id,
            prompt=prompt,
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            allowed_domains=allowed_domains or ["api.anthropic.com"],
            on_message=on_message,
        )
    return await _execute_via_cli(
        task_id=task_id,
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        allowed_domains=allowed_domains or ["api.anthropic.com"],
    )


async def _execute_via_sdk(
    *,
    task_id: str,
    prompt: str,
    cwd: str,
    model: str,
    max_turns: int,
    allowed_domains: list[str],
    on_message: Callable[[Any], None] | None = None,
) -> ExecutorResult:
    """Primary path: Claude Agent SDK."""
    started_at = _now()
    transcript: list[TranscriptEntry] = []
    files_read: set[str] = set()
    files_written: set[str] = set()
    commands_executed: list[str] = []
    network_requests: list[dict[str, Any]] = []
    result_message: Any = None
    last_message: Any = None  # F7: initialize to avoid NameError

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=cwd,
                model=model,
                max_turns=max_turns,
                permission_mode="bypassPermissions",
                allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
            ),
        ):
            if on_message:
                on_message(message)

            transcript.append(
                TranscriptEntry(
                    timestamp=_now(),
                    type=getattr(message, "type", str(type(message).__name__)),
                    content=_capture_message(message),
                )
            )

            # Extract audit info from tool calls
            if hasattr(message, "message") and hasattr(message.message, "content"):
                for block in message.message.content:
                    if getattr(block, "type", None) == "tool_use":
                        _extract_audit(
                            block,
                            files_read,
                            files_written,
                            commands_executed,
                            network_requests,
                            allowed_domains,
                        )

            msg_type = getattr(message, "type", None)
            if msg_type == "result":
                result_message = message
            # Also capture the last message as a fallback in case
            # the SDK no longer emits a distinct "result" type.
            last_message = message

    except Exception as exc:
        completed_at = _now()
        return ExecutorResult(
            success=False,
            diff="",
            audit=_build_audit(
                task_id,
                started_at,
                completed_at,
                files_read,
                files_written,
                commands_executed,
                network_requests,
                0,
                0,
                0.0,
            ),
            transcript=transcript,
            error=str(exc),
        )

    completed_at = _now()

    # Capture diff
    diff, diff_warnings = _capture_diff(cwd)

    # If no explicit result message was emitted, use last_message for metadata
    # extraction only — but do NOT infer success from a non-empty diff alone.
    # Partial output from a terminated/failed run must not be classified as success.
    if result_message is None and last_message is not None:
        print(
            "[executor] No SDK result message received — "
            "using last message for metadata only (not marking as success)"
        )
        result_message = last_message

    # Record Anthropic API usage - always relevant since the agent called the API
    if transcript:
        network_requests.insert(0, {"destination": "api.anthropic.com", "allowed": True})

    usage = getattr(result_message, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
    tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
    # Determine success: explicit "success" subtype, OR the agent hit max_turns
    # but still produced a non-empty diff (work completed, just ran out of turns
    # before the SDK could emit a clean "success" result).
    subtype = getattr(result_message, "subtype", "")
    is_success = subtype == "success" or (subtype == "error_max_turns" and bool(diff.strip()))

    # If audit data is empty but we have a diff, extract file info from it
    if not files_written and diff.strip():
        files_written = _extract_files_from_diff(diff)

    return ExecutorResult(
        success=is_success,
        diff=diff,
        audit=_build_audit(
            task_id,
            started_at,
            completed_at,
            files_read,
            files_written,
            commands_executed,
            network_requests,
            tokens_in,
            tokens_out,
            getattr(result_message, "total_cost_usd", 0.0) or 0.0,
            warnings=diff_warnings,
        ),
        transcript=transcript,
        num_turns=getattr(result_message, "num_turns", 0),
        total_cost_usd=getattr(result_message, "total_cost_usd", 0.0) or 0.0,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        duration_ms=getattr(result_message, "duration_ms", 0),
        duration_api_ms=getattr(result_message, "duration_api_ms", 0),
        stop_reason=getattr(result_message, "stop_reason", None),
        error=None if is_success else _get_errors(result_message),
    )


async def _execute_via_cli(
    *,
    task_id: str,
    prompt: str,
    cwd: str,
    model: str,
    max_turns: int,
    allowed_domains: list[str],
) -> ExecutorResult:
    """Fallback: invoke `claude` CLI as subprocess."""
    started_at = _now()
    print(f"[executor] SDK not available, using CLI fallback for task {task_id}")

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "claude",
                "--print",
                "-p",
                prompt,
                "--model",
                model,
                "--max-turns",
                str(max_turns),
                "--dangerously-skip-permissions",
                # S5: Restrict tools in CLI fallback to match SDK path
                "--allowedTools",
                "Read,Edit,Write,Bash,Glob,Grep",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("TASK_TIMEOUT_S", "1800")),
        )
    except subprocess.TimeoutExpired:
        completed_at = _now()
        timeout_s = os.environ.get("TASK_TIMEOUT_S", "1800")
        return ExecutorResult(
            success=False,
            diff="",
            audit=_build_audit(task_id, started_at, completed_at, set(), set(), [], [], 0, 0, 0.0),
            transcript=[],
            error=f"CLI execution timed out after {timeout_s}s",
        )
    except Exception as exc:
        completed_at = _now()
        return ExecutorResult(
            success=False,
            diff="",
            audit=_build_audit(task_id, started_at, completed_at, set(), set(), [], [], 0, 0, 0.0),
            transcript=[],
            error=str(exc),
        )

    completed_at = _now()
    diff, diff_warnings = _capture_diff(cwd)
    is_success = result.returncode == 0

    transcript = [
        TranscriptEntry(timestamp=started_at, type="cli_output", content=result.stdout),
    ]
    if result.stderr:
        transcript.append(
            TranscriptEntry(timestamp=completed_at, type="cli_stderr", content=result.stderr),
        )

    network_requests: list[dict[str, Any]] = []
    if is_success:
        network_requests.append({"destination": "api.anthropic.com", "allowed": True})

    return ExecutorResult(
        success=is_success,
        diff=diff,
        audit=_build_audit(
            task_id,
            started_at,
            completed_at,
            set(),
            set(),
            [],
            network_requests,
            0,
            0,
            0.0,
            warnings=diff_warnings,
        ),
        transcript=transcript,
        error=None
        if is_success
        else (result.stderr or f"CLI exited with code {result.returncode}"),
    )


# ── Helpers ────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_GIT_TIMEOUT_S = 60  # Timeout for individual git subprocess calls


def _capture_diff(cwd: str) -> tuple[str, list[str]]:
    """Capture git diff including staged and untracked files.

    Returns (diff_text, warnings). Warnings about partial failures are
    included in the audit log so users know if their diff is incomplete.
    """
    warnings: list[str] = []
    diff = ""
    try:
        # Unstaged changes
        result = subprocess.run(
            ["git", "diff"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        if result.returncode != 0:
            warnings.append(f"git diff failed: {result.stderr.strip()}")
        diff = result.stdout

        # Staged changes (git add'd but not committed)
        staged = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        if staged.returncode != 0:
            warnings.append(f"git diff --cached failed: {staged.stderr.strip()}")
        elif staged.stdout:
            diff += "\n" + staged.stdout

        # Also capture untracked files
        untracked_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        untracked = untracked_result.stdout.strip()

        if untracked:
            for f in untracked.split("\n"):
                try:
                    r = subprocess.run(
                        ["git", "diff", "--no-index", "/dev/null", f],
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        timeout=_GIT_TIMEOUT_S,
                    )
                    diff += "\n" + r.stdout
                except subprocess.TimeoutExpired:
                    warnings.append(f"Timed out diffing untracked file {f}")
                except Exception as exc:
                    warnings.append(f"Failed to diff untracked file {f}: {exc}")
    except subprocess.TimeoutExpired:
        warnings.append("Git diff capture timed out")
    except Exception as exc:
        warnings.append(f"Diff capture failed: {exc}")

    if warnings:
        for w in warnings:
            print(f"[executor] WARNING: {w}")
    return diff, warnings


def _extract_audit(
    block: Any,
    files_read: set[str],
    files_written: set[str],
    commands_executed: list[str],
    network_requests: list[dict[str, Any]],
    allowed_domains: list[str],
) -> None:
    name = getattr(block, "name", "")
    inp = getattr(block, "input", {})
    if not isinstance(inp, dict):
        return

    if name == "Read":
        fp = inp.get("file_path", "")
        if fp:
            files_read.add(fp)
    elif name in ("Edit", "Write"):
        fp = inp.get("file_path", "")
        if fp:
            files_written.add(fp)
    elif name == "Bash":
        cmd = inp.get("command", "")
        if cmd:
            commands_executed.append(cmd)
            _extract_network_from_command(cmd, network_requests, allowed_domains)
    elif name in ("WebFetch", "WebSearch"):
        url = inp.get("url", "")
        domain = _extract_domain(url)
        if domain:
            allowed = domain in allowed_domains
            network_requests.append({"destination": domain, "allowed": allowed})


def _extract_network_from_command(
    command: str,
    network_requests: list[dict[str, Any]],
    allowed_domains: list[str],
) -> None:
    pattern = r'(?:curl|wget|fetch|pip3?\s+install|npm\s+install)\s+(?:[^\s]*\s+)*(?:[\'"]?)?(https?://[^\s\'"]+)'
    for match in re.finditer(pattern, command, re.IGNORECASE):
        domain = _extract_domain(match.group(1))
        if domain:
            allowed = domain in allowed_domains
            network_requests.append({"destination": domain, "allowed": allowed})


def _extract_files_from_diff(diff: str) -> set[str]:
    """Extract file paths from a unified diff when audit extraction missed them."""
    files: set[str] = set()
    for match in re.finditer(r"^diff --git a/.+ b/(.+)$", diff, re.MULTILINE):
        files.add(match.group(1))
    return files


def _extract_domain(url: str) -> str | None:
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def _build_audit(
    task_id: str,
    started_at: str,
    completed_at: str,
    files_read: set[str],
    files_written: set[str],
    commands_executed: list[str],
    network_requests: list[dict[str, Any]],
    tokens_input: int,
    tokens_output: int,
    estimated_cost_usd: float,
    warnings: list[str] | None = None,
) -> AuditLog:
    return AuditLog(
        task_id=task_id,
        started_at=started_at,
        completed_at=completed_at,
        files_read=sorted(files_read),
        files_written=sorted(files_written),
        commands_executed=commands_executed,
        network_requests=network_requests,
        tokens={"input": tokens_input, "output": tokens_output},
        estimated_cost_usd=estimated_cost_usd,
        warnings=warnings or [],
    )


def _capture_message(msg: Any) -> str:
    """Full message capture - no truncation."""
    msg_type = getattr(msg, "type", "")

    if msg_type == "assistant" and hasattr(msg, "message"):
        parts: list[str] = []
        for block in msg.message.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
            elif getattr(block, "type", "") == "tool_use":
                parts.append(json.dumps({"tool": block.name, "input": block.input}))
        return "\n".join(parts)

    if msg_type == "result":
        subtype = getattr(msg, "subtype", "")
        turns = getattr(msg, "num_turns", 0)
        cost = getattr(msg, "total_cost_usd", 0)
        if subtype == "success":
            result_text = getattr(msg, "result", "")
            return f"result={result_text} turns={turns} cost=${cost:.4f}"
        errors = getattr(msg, "errors", [])
        return f"error subtype={subtype} turns={turns} errors={'; '.join(errors or [])}"

    return str(msg)


def _get_errors(result_message: Any) -> str | None:
    if result_message is None:
        return "No result message received"
    errors = getattr(result_message, "errors", None)
    if errors:
        return "; ".join(errors)
    return getattr(result_message, "subtype", None)
