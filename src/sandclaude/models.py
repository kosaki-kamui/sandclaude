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
    pending_approval = "pending_approval"
    completed = "completed"
    partial = "partial"  # v0.4.0: agent hit max_turns but produced output
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"  # v0.4.0: container-level timeout


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
    policy_preset: str | None = Field(None, max_length=64)
    declared_secrets: list[str] | None = None  # secret names this task needs
    cost_budget_usd: float | None = Field(None, ge=0.0, le=10000.0)
    labels: list[str] | None = None  # v0.3.0: searchable tags

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
    policy_preset: str | None = None
    requires_approval: int = 0  # 1 if any gate is pending
    declared_secrets: str | None = None  # JSON-encoded list of requested secret names
    cost_budget_usd: float | None = None
    budget_check_json: str | None = None  # JSON-encoded budget_check from admission
    created_by_user_id: int | None = None  # v0.3.0: user who created this task
    # v0.3.0: Observability
    setup_completed_at: str | None = None
    agent_started_at: str | None = None
    error_category: str | None = None
    parent_task_id: str | None = None
    labels: str | None = None  # JSON-encoded list of tags
    cancel_reason: str | None = None
    # v0.4.0: Execution result model
    completion_reason: str | None = None  # "success", "max_turns", "cost_exceeded", etc.
    review_required: int = 0  # 1 if task needs human review before PR

    def safe_dump(self) -> dict:
        """Serialize for API responses, excluding internal fields (S11)."""
        import re

        d = self.model_dump()
        d.pop("container_id", None)
        d.pop("owner_token_hash", None)
        d.pop("host_cwd", None)
        d.pop("budget_check_json", None)  # internal; exposed as budget_check
        # Sanitize error field to avoid leaking internal paths/config to clients
        if d.get("error"):
            err = d["error"]
            err = re.sub(r"(?:/[^\s:\"']+)+/?", "<path>", err)
            err = re.sub(r"(?:[A-Za-z]:\\[^\s:\"']+)+\\?", "<path>", err)
            err = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", err)
            if len(err) > 500:
                err = err[:500] + "..."
            d["error"] = err
        # Compute timeline from phase timestamps
        d["timeline"] = self._compute_timeline()
        return d

    def _compute_timeline(self) -> dict | None:
        """Compute phase durations from timestamps."""
        if not self.created_at:
            return None
        from datetime import datetime

        def _parse(ts: str | None) -> datetime | None:
            if not ts:
                return None
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None

        def _delta_s(start: datetime | None, end: datetime | None) -> float | None:
            if start and end:
                return round((end - start).total_seconds(), 1)
            return None

        created = _parse(self.created_at)
        started = _parse(self.started_at)
        setup_done = _parse(self.setup_completed_at)
        agent_start = _parse(self.agent_started_at)
        completed = _parse(self.completed_at)

        return {
            "queued_duration_s": _delta_s(created, started),
            "setup_duration_s": _delta_s(started, setup_done),
            "agent_duration_s": _delta_s(agent_start, completed),
            "total_duration_s": _delta_s(created, completed),
        }


class AuditLog(BaseModel):
    schema_version: str = "2"
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
    operator_summary: dict[str, Any] = Field(default_factory=dict)


class TranscriptEntry(BaseModel):
    timestamp: str
    type: str
    content: str


# ---------------------------------------------------------------------------
# v0.2.0: Approval gates
# ---------------------------------------------------------------------------


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ApprovalGate(BaseModel):
    id: int = 0
    task_id: str
    action: str  # 'create_pr', 'push', etc.
    status: ApprovalStatus = ApprovalStatus.pending
    reason: str | None = None
    decided_by: str | None = None  # token fingerprint
    decided_at: str | None = None
    created_at: str = ""
    expires_at: str | None = None  # v0.4.0: ISO 8601, None = never expires


class ApprovalDecisionRequest(BaseModel):
    reason: str | None = Field(None, max_length=1000)


# ---------------------------------------------------------------------------
# v0.2.0: Token scopes
# ---------------------------------------------------------------------------

# Valid scope values
VALID_SCOPES = frozenset(
    {
        "tasks:create",
        "tasks:read",
        "tasks:cancel",
        "tasks:delete",
        "tasks:approve",
        "prs:create",
        "admin:tokens",
        "admin:policies",
        "admin:users",
    }
)


class TokenInfo(BaseModel):
    id: int = 0
    name: str
    token_hash: str
    scopes: list[str] = Field(default_factory=list)
    created_at: str = ""
    expires_at: str | None = None
    revoked_at: str | None = None
    created_by: str | None = None
    user_id: int | None = None  # v0.3.0: owning user

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "admin:*" in self.scopes

    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            from datetime import datetime, timezone

            try:
                exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
                return datetime.now(timezone.utc) < exp
            except (ValueError, AttributeError):
                return False
        return True


class TokenCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    scopes: list[str]
    expires_in_days: int | None = Field(None, ge=1, le=365)
    user_id: int | None = None  # v0.3.0: which user owns this token (admin-only)

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("At least one scope is required")
        for s in value:
            if s not in VALID_SCOPES:
                raise ValueError(f"Invalid scope: {s}. Valid: {sorted(VALID_SCOPES)}")
        return value


class TokenCreateResponse(BaseModel):
    """Returned only once at creation — includes the raw token."""

    id: int
    name: str
    token: str  # raw token, shown only once
    scopes: list[str]
    created_at: str
    expires_at: str | None = None


# ---------------------------------------------------------------------------
# v0.2.0: Policy presets
# ---------------------------------------------------------------------------


class PolicyPreset(BaseModel):
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str | None = None


class PolicyPresetConfig(BaseModel):
    """Schema for the JSON config blob inside a preset."""

    allowed_commands: list[str] | None = None
    allowed_write_paths: list[str] | None = None
    allowed_domains: list[str] | None = None
    allowed_secrets: list[str] | None = None
    allow_pr_creation: bool = True
    requires_approval_for: list[str] | None = None  # actions requiring approval
    max_turns: int | None = None
    max_cost_usd: float | None = None
    timeout_s: int | None = None
    model: str | None = None
    # v0.2.0 Phase 4: Repo and branch policy
    allowed_repos: list[str] | None = None  # repo URL patterns or "." for local
    blocked_branches: list[str] | None = None  # branches that cannot be targeted
    pr_only: bool = False  # if True, direct push is blocked (only PRs allowed)
    # v0.2.5: Budget admission control
    budget_fail_policy: str | None = None  # "reject", "warn", "require_approval"
    # v0.3.0: Rule-based approval conditions
    approval_rules: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# v0.2.0: Network deny explainability
# ---------------------------------------------------------------------------


class NetworkDeny(BaseModel):
    """Structured record of a denied network request."""

    domain: str
    ip: str | None = None
    port: int | None = None
    reason: str  # 'not_allowlisted', 'private_ip', 'policy_reject', 'stale_ip'
    explanation: str  # human-readable message


# ---------------------------------------------------------------------------
# v0.3.0: Users / identity
# ---------------------------------------------------------------------------


class User(BaseModel):
    id: int = 0
    username: str
    display_name: str
    email: str | None = None
    github_username: str | None = None
    is_service_account: int = 0  # SQLite uses int for bool
    created_at: str = ""
    created_by_user_id: int | None = None


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9._-]+$")
    display_name: str = Field(..., min_length=1, max_length=128)
    email: str | None = Field(None, max_length=256)
    github_username: str | None = Field(None, max_length=64)
    is_service_account: bool = False
