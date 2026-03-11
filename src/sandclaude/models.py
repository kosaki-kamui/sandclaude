"""
Shared data models (Pydantic).
"""

from __future__ import annotations

import ipaddress
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def _is_valid_domain(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    labels = value.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not all(c.isalnum() or c == "-" for c in label):
            return False
    return True


def _is_private_or_local_host(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        return False


class TaskStatus(str, Enum):
    queued = "queued"
    setup = "setup"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class TaskPriority(str, Enum):
    high = "high"
    normal = "normal"
    low = "low"


class NotifyConfig(BaseModel):
    webhook: str
    on: list[str] = Field(default_factory=lambda: ["completed", "failed"])

    @field_validator("webhook")
    @classmethod
    def validate_webhook(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("notify.webhook must use https")
        if not parsed.hostname:
            raise ValueError("notify.webhook must include a valid host")
        if _is_private_or_local_host(parsed.hostname):
            raise ValueError("notify.webhook cannot target localhost or private/internal addresses")
        return value


MAX_PROMPT_LENGTH = 100_000  # S13: limit prompt size


class TaskCreateRequest(BaseModel):
    repo: str
    prompt: str = Field(..., max_length=MAX_PROMPT_LENGTH)
    branch: str | None = Field(None, max_length=256)
    model: str | None = Field("claude-sonnet-4-5", max_length=64)
    max_turns: int | None = Field(50, ge=1, le=500)
    priority: TaskPriority | None = TaskPriority.normal
    host_cwd: str | None = None  # injected by MCP plugin when repo="."
    allowed_domains: list[str] | None = None  # extra domains allowed in agent phase
    notify: NotifyConfig | None = None

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if len(value) > 4096:
            raise ValueError("repo too long")
        # Reject path traversal patterns
        if ".." in value:
            raise ValueError("repo must not contain '..'")
        return value

    @field_validator("host_cwd")
    @classmethod
    def validate_host_cwd(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if ".." in value:
            raise ValueError("host_cwd must not contain '..'")
        if not value.startswith("/"):
            raise ValueError("host_cwd must be an absolute path")
        if len(value) > 4096:
            raise ValueError("host_cwd too long")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def validate_allowed_domains(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if len(value) > 20:
            raise ValueError("too many allowed_domains (max 20)")
        normalized: list[str] = []
        for domain in value:
            d = domain.strip().lower()
            if not _is_valid_domain(d):
                raise ValueError(f"invalid domain in allowed_domains: {domain}")
            normalized.append(d)
        return normalized


class CreatePRRequest(BaseModel):
    title: str | None = Field(None, max_length=256)


class Task(BaseModel):
    id: str
    status: TaskStatus
    repo: str
    branch: str | None = None
    prompt: str
    model: str = "claude-sonnet-4-5"
    max_turns: int = 50
    priority: TaskPriority = TaskPriority.normal
    owner_token_hash: str | None = None
    container_id: str | None = None
    host_cwd: str | None = None
    allowed_domains: str | None = None  # JSON-encoded list: '["registry.npmjs.org"]'
    notify_webhook: str | None = None
    notify_on: str | None = None  # JSON-encoded list: '["completed","failed"]'
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    total_cost_usd: float | None = None
    error: str | None = None

    def safe_dump(self) -> dict:
        """Serialize for API responses, excluding internal fields (S11)."""
        import re

        d = self.model_dump()
        d.pop("container_id", None)
        d.pop("owner_token_hash", None)
        d.pop("host_cwd", None)
        # Sanitize error field to avoid leaking internal paths/config to clients
        if d.get("error"):
            err = d["error"]
            err = re.sub(r"(?:/[^\s:\"']+)+/?", "<path>", err)
            err = re.sub(r"(?:[A-Za-z]:\\[^\s:\"']+)+\\?", "<path>", err)
            err = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", err)
            if len(err) > 500:
                err = err[:500] + "..."
            d["error"] = err
        return d


class AuditLog(BaseModel):
    task_id: str
    started_at: str
    completed_at: str
    files_read: list[str] = Field(default_factory=list)
    files_written: list[str] = Field(default_factory=list)
    commands_executed: list[str] = Field(default_factory=list)
    network_requests: list[dict[str, Any]] = Field(default_factory=list)
    tokens: dict[str, int] = Field(default_factory=lambda: {"input": 0, "output": 0})
    estimated_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class TranscriptEntry(BaseModel):
    timestamp: str
    type: str
    content: str
