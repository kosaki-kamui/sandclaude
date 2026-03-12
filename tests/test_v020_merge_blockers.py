"""Tests for v0.2.0 merge blocker checklist items.

Covers:
- Restrictive policy merge semantics (P0-1)
- Approval scope enforcement (P0-2)
- Signed approval link tokens (P0-3)
- Approval lifecycle transitions (P0-4)
"""

from __future__ import annotations

import time

import pytest

from sandclaude.auth import (
    AuthResult,
    create_approval_link_token,
    init_token,
    require_scope,
    verify_approval_link_token,
)
from sandclaude.db import store as db
from sandclaude.models import ApprovalStatus, TaskStatus
from sandclaude.policy import merge_policy

# ---------------------------------------------------------------------------
# P0-1: Restrictive policy merge — task overrides cannot widen access
# ---------------------------------------------------------------------------


class TestRestrictiveMerge:
    """Task overrides can only narrow access, never widen it."""

    def test_allowlist_intersection_narrows(self):
        preset = {"allowed_commands": ["npm", "pip", "cargo"]}
        overrides = {"allowed_commands": ["pip"]}
        merged = merge_policy(preset, overrides)
        assert set(merged["allowed_commands"]) == {"pip"}

    def test_allowlist_disjoint_produces_empty(self):
        preset = {"allowed_commands": ["npm", "pip"]}
        overrides = {"allowed_commands": ["cargo", "go"]}
        merged = merge_policy(preset, overrides)
        assert merged["allowed_commands"] == []

    def test_allowlist_no_preset_restriction_accepts_task(self):
        preset = {}
        overrides = {"allowed_commands": ["npm"]}
        merged = merge_policy(preset, overrides)
        assert merged["allowed_commands"] == ["npm"]

    def test_denylist_union_adds_restrictions(self):
        preset = {"blocked_branches": ["main"]}
        overrides = {"blocked_branches": ["staging"]}
        merged = merge_policy(preset, overrides)
        assert set(merged["blocked_branches"]) == {"main", "staging"}

    def test_denylist_task_cannot_remove_preset_denies(self):
        preset = {"requires_approval_for": ["create_pr", "push"]}
        overrides = {"requires_approval_for": ["create_pr"]}
        merged = merge_policy(preset, overrides)
        # Union: preset's "push" survives
        assert "push" in merged["requires_approval_for"]
        assert "create_pr" in merged["requires_approval_for"]

    def test_numeric_minimum_wins(self):
        preset = {"max_cost_usd": 5.0}
        overrides = {"max_cost_usd": 10.0}
        merged = merge_policy(preset, overrides)
        # Task cannot raise ceiling
        assert merged["max_cost_usd"] == 5.0

    def test_numeric_task_can_lower(self):
        preset = {"max_cost_usd": 5.0}
        overrides = {"max_cost_usd": 1.0}
        merged = merge_policy(preset, overrides)
        assert merged["max_cost_usd"] == 1.0

    def test_restriction_bool_true_wins(self):
        preset = {"pr_only": False}
        overrides = {"pr_only": True}
        merged = merge_policy(preset, overrides)
        assert merged["pr_only"] is True

    def test_restriction_bool_preset_true_preserved(self):
        preset = {"pr_only": True}
        overrides = {"pr_only": False}
        merged = merge_policy(preset, overrides)
        # Task cannot turn off a restriction
        assert merged["pr_only"] is True

    def test_permissive_bool_false_wins(self):
        preset = {"allow_pr_creation": True}
        overrides = {"allow_pr_creation": False}
        merged = merge_policy(preset, overrides)
        assert merged["allow_pr_creation"] is False

    def test_permissive_bool_preset_false_preserved(self):
        preset = {"allow_pr_creation": False}
        overrides = {"allow_pr_creation": True}
        merged = merge_policy(preset, overrides)
        # Task cannot re-enable something the preset disabled
        assert merged["allow_pr_creation"] is False

    def test_domains_intersection(self):
        preset = {"allowed_domains": ["pypi.org", "registry.npmjs.org"]}
        overrides = {"allowed_domains": ["pypi.org", "evil.com"]}
        merged = merge_policy(preset, overrides)
        assert set(merged["allowed_domains"]) == {"pypi.org"}
        assert "evil.com" not in merged["allowed_domains"]

    def test_secrets_intersection(self):
        preset = {"allowed_secrets": ["NPM_TOKEN", "DB_URL"]}
        overrides = {"allowed_secrets": ["NPM_TOKEN", "AWS_KEY"]}
        merged = merge_policy(preset, overrides)
        assert merged["allowed_secrets"] == ["NPM_TOKEN"]
        assert "AWS_KEY" not in merged["allowed_secrets"]

    def test_string_fields_still_override(self):
        """Non-security string fields like model should still be overridable."""
        preset = {"model": "claude-sonnet-4-5"}
        overrides = {"model": "claude-opus-4-6"}
        merged = merge_policy(preset, overrides)
        assert merged["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# P0-2: Approval scope enforcement
# ---------------------------------------------------------------------------


class TestApprovalScopeEnforcement:
    def test_legacy_admin_token_can_approve(self):
        auth = AuthResult(
            token="x",
            fingerprint="fp",
            is_legacy=True,
            scopes=[],
        )
        # Legacy tokens have all scopes
        assert auth.has_scope("tasks:approve") is True

    def test_scoped_token_with_approve_can_approve(self):
        auth = AuthResult(
            token="x",
            fingerprint="fp",
            is_legacy=False,
            scopes=["tasks:create", "tasks:read", "tasks:approve"],
        )
        assert auth.has_scope("tasks:approve") is True

    def test_scoped_token_without_approve_cannot_approve(self):
        from fastapi import HTTPException

        auth = AuthResult(
            token="x",
            fingerprint="fp",
            is_legacy=False,
            scopes=["tasks:create", "tasks:read"],
        )
        assert auth.has_scope("tasks:approve") is False
        with pytest.raises(HTTPException) as exc_info:
            require_scope(auth, "tasks:approve")
        assert exc_info.value.status_code == 403

    def test_revoked_token_cannot_be_used(self):
        """A revoked token should be rejected before scope checks."""
        from sandclaude.models import TokenInfo

        token = TokenInfo(
            name="revoked",
            token_hash="x",
            scopes=["tasks:approve"],
            revoked_at="2025-01-01T00:00:00+00:00",
        )
        assert token.is_active() is False

    def test_expired_token_cannot_be_used(self):
        from sandclaude.models import TokenInfo

        token = TokenInfo(
            name="expired",
            token_hash="x",
            scopes=["tasks:approve"],
            expires_at="2020-01-01T00:00:00+00:00",
        )
        assert token.is_active() is False


# ---------------------------------------------------------------------------
# P0-3: Signed approval link tokens
# ---------------------------------------------------------------------------


class TestSignedApprovalLinks:
    def test_create_and_verify_approval_token(self):
        init_token()
        token = create_approval_link_token("task-123", "create_pr")
        assert verify_approval_link_token(token, "task-123", "create_pr") is True

    def test_wrong_task_id_rejected(self):
        init_token()
        token = create_approval_link_token("task-123", "create_pr")
        assert verify_approval_link_token(token, "task-999", "create_pr") is False

    def test_wrong_action_rejected(self):
        init_token()
        token = create_approval_link_token("task-123", "create_pr")
        assert verify_approval_link_token(token, "task-123", "push") is False

    def test_expired_token_rejected(self):
        init_token()
        # Create a token that expires in 0 seconds
        token = create_approval_link_token("task-123", "create_pr", ttl_s=0)
        time.sleep(0.1)
        assert verify_approval_link_token(token, "task-123", "create_pr") is False

    def test_tampered_signature_rejected(self):
        init_token()
        token = create_approval_link_token("task-123", "create_pr")
        # Tamper with the signature
        parts = token.split(":")
        parts[-1] = "0" * len(parts[-1])
        tampered = ":".join(parts)
        assert verify_approval_link_token(tampered, "task-123", "create_pr") is False

    def test_malformed_token_rejected(self):
        init_token()
        assert verify_approval_link_token("garbage", "task-123", "create_pr") is False
        assert verify_approval_link_token("", "task-123", "create_pr") is False
        assert verify_approval_link_token("a:b", "task-123", "create_pr") is False


# ---------------------------------------------------------------------------
# P0-4: Approval lifecycle transitions
# ---------------------------------------------------------------------------


class TestApprovalLifecycle:
    @pytest.mark.asyncio
    async def test_task_without_preset_has_no_gates(self):
        """Tasks without a policy preset should not get approval gates."""
        await db.init_db()
        task = await db.create_task(
            task_id="task-lifecycle1",
            repo=".",
            prompt="test",
        )
        gates = await db.get_approval_gates(task.id)
        assert gates == []
        assert task.requires_approval == 0

    @pytest.mark.asyncio
    async def test_pending_approval_is_valid_status(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-lifecycle2",
            repo=".",
            prompt="test",
        )
        await db.update_task(task.id, status=TaskStatus.pending_approval)
        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.pending_approval

    @pytest.mark.asyncio
    async def test_approval_resolves_requires_flag(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-lifecycle3",
            repo=".",
            prompt="test",
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.update_task(task.id, requires_approval=1)

        # Approve the gate
        await db.decide_approval_gate(
            task.id,
            "create_pr",
            decision=ApprovalStatus.approved,
            decided_by="fp_test",
        )

        # Check no more pending gates
        assert await db.has_pending_gates(task.id) is False

    @pytest.mark.asyncio
    async def test_gate_decision_is_auditable(self):
        """Approval decisions must record who decided and why."""
        await db.init_db()
        task = await db.create_task(
            task_id="task-lifecycle4",
            repo=".",
            prompt="test",
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.decide_approval_gate(
            task.id,
            "create_pr",
            decision=ApprovalStatus.approved,
            decided_by="fp_alice",
            reason="Reviewed diff, LGTM",
        )

        gates = await db.get_approval_gates(task.id)
        assert gates[0].decided_by == "fp_alice"
        assert gates[0].reason == "Reviewed diff, LGTM"
        assert gates[0].decided_at is not None

    @pytest.mark.asyncio
    async def test_backward_compat_old_tasks_no_approval(self):
        """Tasks created without v0.2.0 fields should work normally."""
        await db.init_db()
        task = await db.create_task(
            task_id="task-compat1",
            repo=".",
            prompt="old-style task",
        )
        assert task.policy_preset is None
        assert task.requires_approval == 0
        assert task.declared_secrets is None
        assert task.cost_budget_usd is None

        # Should be completable
        await db.update_task(task.id, status=TaskStatus.completed)
        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.completed
