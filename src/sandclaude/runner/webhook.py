"""
Webhook notifications - POST task results to configured webhooks.
Supports Slack-formatted messages out of the box.

Slack notifications include the diff inline (truncated at 7500 chars for
large diffs), so no external URL or auth is needed to see results.

PRIVACY NOTE: Webhook payloads omit task prompts by default.
Set WEBHOOK_INCLUDE_PROMPT=true to include a truncated prompt excerpt.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from sandclaude.config import settings
from sandclaude.models import Task


def _sanitize_error_for_webhook(error: str | None) -> str | None:
    """Redact internal paths and truncate error messages before sending to webhooks.

    Exceptions can contain host paths, config values, or stack traces that
    should not be sent to third-party webhook endpoints.
    """
    if not error:
        return error
    import re

    sanitized = re.sub(r"(?:/[^\s:\"']+)+/?", "<path>", error)
    sanitized = re.sub(r"(?:[A-Za-z]:\\[^\s:\"']+)+\\?", "<path>", sanitized)
    # Strip control characters
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", sanitized)
    if len(sanitized) > 300:
        sanitized = sanitized[:300] + "..."
    return sanitized


def _redact_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "unknown-host"
        return f"{parsed.scheme}://{host}/..."
    except Exception:
        return "<invalid-url>"


def _resolve_safe_webhook_ip(url: str) -> str | None:
    """S6: Resolve webhook hostname and validate all IPs are public.

    Returns the first safe IP address string to connect to,
    or None if validation fails.

    Note: this function validates DNS answers only. The caller still
    connects by hostname via httpx, so a narrow TOCTOU window remains.
    """
    try:
        import ipaddress
        import socket

        parsed = urlparse(url)
        if parsed.scheme != "https":
            return None
        hostname = parsed.hostname
        if not hostname:
            return None

        # Resolve the hostname and check all IPs
        try:
            addrs = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return None

        first_safe_ip: str | None = None
        for family, _, _, _, sockaddr in addrs:
            ip_str = str(sockaddr[0])
            try:
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
                    or ip.is_unspecified
                ):
                    return None  # Any private IP in the set fails the whole check
            except ValueError:
                return None
            if first_safe_ip is None:
                first_safe_ip = ip_str
        return first_safe_ip
    except Exception:
        return None


async def send_webhook(task: Task) -> None:
    """Send webhook notification if configured and status matches."""
    if not task.notify_webhook or not task.notify_on:
        return

    # Defense-in-depth: enforce HTTPS at send time even if DB row bypassed model validation
    env_name = settings.environment.strip().lower()
    if env_name not in {"test", "dev", "development"}:
        if not task.notify_webhook.startswith("https://"):
            print(
                f"[webhook] BLOCKED: {_redact_url(task.notify_webhook)} "
                f"is not HTTPS (task {task.id})"
            )
            return

    try:
        events: list[str] = json.loads(task.notify_on)
    except (json.JSONDecodeError, TypeError):
        print(f"[webhook] Invalid notify_on JSON for task {task.id}, skipping")
        return
    if task.status.value not in events:
        return

    # S6: Double-resolve DNS validation to mitigate rebinding.
    # We resolve twice with a short delay; if either resolve yields a
    # private IP, we block the request. We do NOT require identical IPs
    # because CDN-backed services (Slack, etc.) rotate IPs via round-robin
    # DNS — two consecutive lookups legitimately return different public IPs.
    # The attack we guard against is rebinding from public → private, not
    # CDN rotation between public IPs.
    # Full elimination of TOCTOU would require a custom transport/dialer
    # that pins the validated IP at the socket level, which httpx does not
    # natively support.
    if env_name not in {"test", "dev", "development"}:
        import asyncio

        ip1 = _resolve_safe_webhook_ip(task.notify_webhook)
        if ip1 is None:
            print(
                f"[webhook] BLOCKED: {_redact_url(task.notify_webhook)} "
                f"resolves to private/local IP or failed validation"
            )
            return
        # Brief delay to catch fast-rebinding DNS
        await asyncio.sleep(0.1)
        ip2 = _resolve_safe_webhook_ip(task.notify_webhook)
        if ip2 is None:
            print(
                f"[webhook] BLOCKED: {_redact_url(task.notify_webhook)} "
                f"second DNS resolution returned private/local IP (possible rebinding)"
            )
            return

    # Load audit data and diff
    task_dir = settings.data_dir / "tasks" / task.id
    audit: dict = {}
    audit_path = task_dir / "audit.json"
    if audit_path.exists():
        audit = json.loads(audit_path.read_text())

    diff: str = ""
    diff_path = task_dir / "diff.patch"
    if diff_path.exists():
        diff = diff_path.read_text()

    payload = _build_payload(task, audit, diff)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(task.notify_webhook, json=payload)
            if resp.is_success:
                print(f"[webhook] Notified {_redact_url(task.notify_webhook)} for task {task.id}")
            else:
                print(
                    f"[webhook] Failed to notify {_redact_url(task.notify_webhook)}: "
                    f"{resp.status_code}"
                )
    except Exception as exc:
        print(f"[webhook] Error sending to {_redact_url(task.notify_webhook)}: {exc}")


def _build_payload(task: Task, audit: dict, diff: str) -> dict:
    duration = _calc_duration(task)
    is_slack = task.notify_webhook and "hooks.slack.com" in task.notify_webhook

    if is_slack:
        return _build_slack_payload(task, audit, duration, diff)

    # Only include prompt if explicitly enabled (default off for privacy)
    prompt_field: str | None = None
    if settings.webhook_include_prompt:
        prompt_field = task.prompt[:200] + ("..." if len(task.prompt) > 200 else "")

    task_payload: dict = {
        "id": task.id,
        "status": task.status.value,
        "repo": task.repo,
        "model": task.model,
        "duration": duration,
        "tokens_input": task.tokens_input,
        "tokens_output": task.tokens_output,
        "total_cost_usd": task.total_cost_usd,
        "error": _sanitize_error_for_webhook(task.error),
    }
    if prompt_field is not None:
        task_payload["prompt"] = prompt_field

    return {
        "event": task.status.value,
        "task": task_payload,
        "audit": {
            "files_read": len(audit.get("files_read", [])),
            "files_written": len(audit.get("files_written", [])),
            "commands_executed": len(audit.get("commands_executed", [])),
        },
    }


def _build_slack_payload(task: Task, audit: dict, duration: str, diff: str) -> dict:
    emoji = ":white_check_mark:" if task.status.value == "completed" else ":x:"
    color = "#36a64f" if task.status.value == "completed" else "#ff0000"

    # Diff summary: list changed files from audit
    files_written = audit.get("files_written", [])
    diff_summary = ", ".join(f"`{f}`" for f in files_written[:5])
    if len(files_written) > 5:
        diff_summary += f" (+{len(files_written) - 5} more)"
    if not diff_summary:
        diff_summary = "(no files changed)"

    # Audit highlights
    net_reqs = audit.get("network_requests", [])
    blocked = [r for r in net_reqs if not r.get("allowed")]
    audit_summary = (
        f"{len(audit.get('files_read', []))} read, "
        f"{len(files_written)} written, "
        f"{len(audit.get('commands_executed', []))} commands, "
        f"{len(blocked)} network blocked"
    )

    fields = [
        {"title": "Model", "value": task.model, "short": True},
        {"title": "Duration", "value": duration, "short": True},
        {
            "title": "Tokens",
            "value": f"{task.tokens_input or '?'} in / {task.tokens_output or '?'} out",
            "short": True,
        },
        {
            "title": "Cost",
            "value": f"${task.total_cost_usd:.4f}" if task.total_cost_usd is not None else "?",
            "short": True,
        },
        {"title": "Files Changed", "value": diff_summary, "short": False},
        {"title": "Audit", "value": audit_summary, "short": False},
    ]

    if task.error:
        fields.append(
            {
                "title": "Error",
                "value": _sanitize_error_for_webhook(task.error) or "",
                "short": False,
            }
        )

    # Include the diff inline (Slack truncates attachments at ~8000 chars)
    if diff.strip():
        diff_text = diff[:7500]
        if len(diff) > 7500:
            diff_text += "\n... (truncated)"
        fields.append({"title": "Diff", "value": f"```{diff_text}```", "short": False})

    return {
        "text": f"{emoji} sandclaude task `{task.id}` {task.status.value}",
        "attachments": [
            {
                "color": color,
                "title": task.prompt[:100]
                if settings.webhook_include_prompt
                else f"Task {task.id}",
                "fields": fields,
                "footer": "sandclaude",
                "ts": int(time.time()),
            }
        ],
    }


def _calc_duration(task: Task) -> str:
    if task.started_at and task.completed_at:
        try:
            start = datetime.fromisoformat(task.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(task.completed_at.replace("Z", "+00:00"))
            return f"{(end - start).total_seconds():.0f}s"
        except Exception:
            pass
    return "unknown"
