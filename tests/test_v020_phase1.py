"""Tests for v0.2.0 Phase 1 features: schema, approval gates, tokens, presets, secrets, policy."""

from __future__ import annotations

import pytest

from sandclaude.db import store as db
from sandclaude.models import (
    VALID_SCOPES,
    ApprovalStatus,
    PolicyPresetConfig,
    TaskStatus,
    TokenInfo,
)
from sandclaude.policy import check_secret_allowed, merge_policy

# ---------------------------------------------------------------------------
# Schema migration: new tables exist and new task columns work
# ---------------------------------------------------------------------------


class TestSchemaMigration:
    @pytest.mark.asyncio
    async def test_new_tables_created(self):
        await db.init_db()
        # Should not raise — tables exist
        gates = await db.get_approval_gates("nonexistent")
        assert gates == []
        tokens = await db.list_tokens()
        assert tokens == []
        presets = await db.list_policy_presets()
        assert presets == []
        secrets = await db.get_task_secrets("nonexistent")
        assert secrets == []

    @pytest.mark.asyncio
    async def test_task_with_new_columns(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-v020-schema",
            repo="https://github.com/test/repo",
            prompt="Test v0.2.0 columns",
            policy_preset="bugfix-pr",
            declared_secrets=["NPM_TOKEN"],
            cost_budget_usd=5.0,
        )
        assert task.policy_preset == "bugfix-pr"
        assert task.declared_secrets == '["NPM_TOKEN"]'
        assert task.cost_budget_usd == 5.0
        assert task.requires_approval == 0

    @pytest.mark.asyncio
    async def test_task_without_new_columns_uses_defaults(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-v020-defaults",
            repo="https://github.com/test/repo",
            prompt="Test defaults",
        )
        assert task.policy_preset is None
        assert task.declared_secrets is None
        assert task.cost_budget_usd is None
        assert task.requires_approval == 0

    @pytest.mark.asyncio
    async def test_pending_approval_status(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-v020-approval",
            repo=".",
            prompt="Test pending_approval",
        )
        await db.update_task(task.id, status=TaskStatus.pending_approval)
        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.pending_approval


# ---------------------------------------------------------------------------
# Approval gates
# ---------------------------------------------------------------------------


class TestApprovalGates:
    @pytest.mark.asyncio
    async def test_create_and_list_gates(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-gate1",
            repo=".",
            prompt="test",
        )
        gate = await db.create_approval_gate(task.id, "create_pr")
        assert gate.action == "create_pr"
        assert gate.status == ApprovalStatus.pending

        gates = await db.get_approval_gates(task.id)
        assert len(gates) == 1
        assert gates[0].action == "create_pr"

    @pytest.mark.asyncio
    async def test_approve_gate(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-gate2",
            repo=".",
            prompt="test",
        )
        await db.create_approval_gate(task.id, "create_pr")

        ok = await db.decide_approval_gate(
            task.id,
            "create_pr",
            decision=ApprovalStatus.approved,
            decided_by="fp_abc123",
            reason="Looks good",
        )
        assert ok is True

        gates = await db.get_approval_gates(task.id)
        assert gates[0].status == ApprovalStatus.approved
        assert gates[0].reason == "Looks good"
        assert gates[0].decided_by == "fp_abc123"

    @pytest.mark.asyncio
    async def test_reject_gate(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-gate3",
            repo=".",
            prompt="test",
        )
        await db.create_approval_gate(task.id, "create_pr")

        ok = await db.decide_approval_gate(
            task.id,
            "create_pr",
            decision=ApprovalStatus.rejected,
            decided_by="fp_abc456",
            reason="Too risky",
        )
        assert ok is True

        gates = await db.get_approval_gates(task.id)
        assert gates[0].status == ApprovalStatus.rejected

    @pytest.mark.asyncio
    async def test_cannot_decide_already_decided(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-gate4",
            repo=".",
            prompt="test",
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.decide_approval_gate(
            task.id,
            "create_pr",
            decision=ApprovalStatus.approved,
            decided_by="fp1",
        )
        # Second decision should fail
        ok = await db.decide_approval_gate(
            task.id,
            "create_pr",
            decision=ApprovalStatus.rejected,
            decided_by="fp2",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_has_pending_gates(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-gate5",
            repo=".",
            prompt="test",
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.create_approval_gate(task.id, "push")

        assert await db.has_pending_gates(task.id) is True

        await db.decide_approval_gate(
            task.id,
            "create_pr",
            decision=ApprovalStatus.approved,
            decided_by="fp1",
        )
        # Still has pending (push)
        assert await db.has_pending_gates(task.id) is True

        await db.decide_approval_gate(
            task.id,
            "push",
            decision=ApprovalStatus.approved,
            decided_by="fp1",
        )
        assert await db.has_pending_gates(task.id) is False

    @pytest.mark.asyncio
    async def test_requires_approval_flag(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-gate6",
            repo=".",
            prompt="test",
        )
        await db.update_task(task.id, requires_approval=1)
        updated = await db.get_task(task.id)
        assert updated.requires_approval == 1

        await db.update_task(task.id, requires_approval=0)
        updated = await db.get_task(task.id)
        assert updated.requires_approval == 0


# ---------------------------------------------------------------------------
# Token registry
# ---------------------------------------------------------------------------


class TestTokenRegistry:
    @pytest.mark.asyncio
    async def test_create_and_lookup_token(self):
        await db.init_db()
        token = await db.create_token(
            name="ci-bot",
            token_hash="abc123hash",
            scopes=["tasks:create", "tasks:read"],
        )
        assert token.name == "ci-bot"
        assert token.scopes == ["tasks:create", "tasks:read"]

        found = await db.get_token_by_hash("abc123hash")
        assert found is not None
        assert found.name == "ci-bot"

    @pytest.mark.asyncio
    async def test_token_not_found(self):
        await db.init_db()
        found = await db.get_token_by_hash("nonexistent")
        assert found is None

    @pytest.mark.asyncio
    async def test_revoke_token(self):
        await db.init_db()
        token = await db.create_token(
            name="temp-token",
            token_hash="revoke_test",
            scopes=["tasks:read"],
        )
        assert token.revoked_at is None

        ok = await db.revoke_token(token.id)
        assert ok is True

        found = await db.get_token_by_hash("revoke_test")
        assert found.revoked_at is not None
        assert found.is_active() is False

    @pytest.mark.asyncio
    async def test_revoke_already_revoked(self):
        await db.init_db()
        token = await db.create_token(
            name="double-revoke",
            token_hash="double_rev",
            scopes=["tasks:read"],
        )
        await db.revoke_token(token.id)
        ok = await db.revoke_token(token.id)
        assert ok is False

    @pytest.mark.asyncio
    async def test_list_tokens(self):
        await db.init_db()
        await db.create_token(
            name="t1",
            token_hash="list_hash1",
            scopes=["tasks:read"],
        )
        await db.create_token(
            name="t2",
            token_hash="list_hash2",
            scopes=["tasks:create"],
        )
        tokens = await db.list_tokens()
        names = {t.name for t in tokens}
        assert "t1" in names
        assert "t2" in names

    def test_token_info_has_scope(self):
        info = TokenInfo(
            name="test",
            token_hash="x",
            scopes=["tasks:create", "tasks:read"],
        )
        assert info.has_scope("tasks:create") is True
        assert info.has_scope("admin:tokens") is False

    def test_token_info_is_active_no_expiry(self):
        info = TokenInfo(name="test", token_hash="x", scopes=[])
        assert info.is_active() is True

    def test_token_info_is_active_expired(self):
        info = TokenInfo(
            name="test",
            token_hash="x",
            scopes=[],
            expires_at="2020-01-01T00:00:00+00:00",
        )
        assert info.is_active() is False

    def test_token_info_is_active_revoked(self):
        info = TokenInfo(
            name="test",
            token_hash="x",
            scopes=[],
            revoked_at="2025-01-01T00:00:00+00:00",
        )
        assert info.is_active() is False


# ---------------------------------------------------------------------------
# Policy presets
# ---------------------------------------------------------------------------


class TestPolicyPresets:
    @pytest.mark.asyncio
    async def test_save_and_get_preset(self):
        await db.init_db()
        config = {"allowed_commands": ["npm", "pip"], "max_cost_usd": 1.0}
        preset = await db.save_policy_preset("bugfix-pr", config)
        assert preset.name == "bugfix-pr"
        assert preset.config == config

        found = await db.get_policy_preset("bugfix-pr")
        assert found is not None
        assert found.config["max_cost_usd"] == 1.0

    @pytest.mark.asyncio
    async def test_update_preset(self):
        await db.init_db()
        await db.save_policy_preset("update-test", {"max_cost_usd": 1.0})
        await db.save_policy_preset("update-test", {"max_cost_usd": 2.0})
        found = await db.get_policy_preset("update-test")
        assert found.config["max_cost_usd"] == 2.0

    @pytest.mark.asyncio
    async def test_delete_preset(self):
        await db.init_db()
        await db.save_policy_preset("delete-me", {})
        ok = await db.delete_policy_preset("delete-me")
        assert ok is True
        assert await db.get_policy_preset("delete-me") is None

    @pytest.mark.asyncio
    async def test_list_presets(self):
        await db.init_db()
        await db.save_policy_preset("preset-a", {"a": 1})
        await db.save_policy_preset("preset-b", {"b": 2})
        presets = await db.list_policy_presets()
        names = {p.name for p in presets}
        assert "preset-a" in names
        assert "preset-b" in names


# ---------------------------------------------------------------------------
# Policy merge logic
# ---------------------------------------------------------------------------


class TestPolicyMerge:
    def test_allowlists_use_intersection(self):
        """Task overrides can only narrow allowlists, not widen them."""
        preset = {"allowed_commands": ["npm", "pip", "cargo"]}
        overrides = {"allowed_commands": ["pip", "cargo"]}
        merged = merge_policy(preset, overrides)
        assert set(merged["allowed_commands"]) == {"pip", "cargo"}

    def test_allowlist_task_cannot_add_items(self):
        """A task requesting a command not in the preset gets nothing extra."""
        preset = {"allowed_commands": ["npm", "pip"]}
        overrides = {"allowed_commands": ["cargo"]}
        merged = merge_policy(preset, overrides)
        assert merged["allowed_commands"] == []

    def test_denylists_use_union(self):
        """Task can add deny entries but not remove preset denies."""
        preset = {"requires_approval_for": ["create_pr"]}
        overrides = {"requires_approval_for": ["push"]}
        merged = merge_policy(preset, overrides)
        assert set(merged["requires_approval_for"]) == {"create_pr", "push"}

    def test_numerics_capped_by_preset(self):
        preset = {"max_cost_usd": 1.0}
        overrides = {"max_cost_usd": 5.0}
        merged = merge_policy(preset, overrides)
        assert merged["max_cost_usd"] == 1.0  # preset caps

    def test_numerics_can_be_lowered(self):
        preset = {"max_cost_usd": 5.0}
        overrides = {"max_cost_usd": 1.0}
        merged = merge_policy(preset, overrides)
        assert merged["max_cost_usd"] == 1.0

    def test_strings_override(self):
        preset = {"model": "claude-sonnet-4-5"}
        overrides = {"model": "claude-opus-4-6"}
        merged = merge_policy(preset, overrides)
        assert merged["model"] == "claude-opus-4-6"

    def test_none_overrides_ignored(self):
        preset = {"max_cost_usd": 1.0, "model": "claude-sonnet-4-5"}
        overrides = {"max_cost_usd": None, "model": None}
        merged = merge_policy(preset, overrides)
        assert merged["max_cost_usd"] == 1.0
        assert merged["model"] == "claude-sonnet-4-5"

    def test_empty_preset(self):
        merged = merge_policy({}, {"model": "claude-opus-4-6"})
        assert merged["model"] == "claude-opus-4-6"

    def test_empty_overrides(self):
        preset = {"max_cost_usd": 1.0}
        merged = merge_policy(preset, {})
        assert merged["max_cost_usd"] == 1.0


# ---------------------------------------------------------------------------
# Secret policy checks
# ---------------------------------------------------------------------------


class TestSecretPolicy:
    def test_secret_allowed_when_in_allowlist(self):
        policy = PolicyPresetConfig(allowed_secrets=["NPM_TOKEN", "DB_URL"])
        assert check_secret_allowed(policy, "NPM_TOKEN") is True

    def test_secret_denied_when_not_in_allowlist(self):
        policy = PolicyPresetConfig(allowed_secrets=["NPM_TOKEN"])
        assert check_secret_allowed(policy, "DB_URL") is False

    def test_all_secrets_allowed_when_no_restriction(self):
        policy = PolicyPresetConfig(allowed_secrets=None)
        assert check_secret_allowed(policy, "ANYTHING") is True

    def test_empty_allowlist_denies_all(self):
        policy = PolicyPresetConfig(allowed_secrets=[])
        assert check_secret_allowed(policy, "NPM_TOKEN") is False


# ---------------------------------------------------------------------------
# Task secrets audit
# ---------------------------------------------------------------------------


class TestTaskSecretsAudit:
    @pytest.mark.asyncio
    async def test_record_and_retrieve_secrets(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-sec1",
            repo=".",
            prompt="test",
        )
        await db.record_task_secret(task.id, "NPM_TOKEN", "setup", True)
        await db.record_task_secret(task.id, "DB_URL", "setup", False)

        secrets = await db.get_task_secrets(task.id)
        assert len(secrets) == 2
        npm = next(s for s in secrets if s["secret_name"] == "NPM_TOKEN")
        assert npm["granted"] is True
        db_url = next(s for s in secrets if s["secret_name"] == "DB_URL")
        assert db_url["granted"] is False


# ---------------------------------------------------------------------------
# Auth: scoped token verification
# ---------------------------------------------------------------------------


class TestAuthScopes:
    def test_valid_scopes_list(self):
        assert "tasks:create" in VALID_SCOPES
        assert "admin:tokens" in VALID_SCOPES
        assert "invalid:scope" not in VALID_SCOPES

    @pytest.mark.asyncio
    async def test_verify_legacy_token(self):
        from sandclaude.auth import init_token, verify_token_with_scopes

        token = init_token()
        result = await verify_token_with_scopes(token)
        assert result.is_legacy is True
        assert result.has_scope("tasks:create") is True
        assert result.has_scope("admin:tokens") is True

    @pytest.mark.asyncio
    async def test_verify_registry_token(self):
        from sandclaude.auth import (
            generate_token,
            init_token,
            token_fingerprint,
            verify_token_with_scopes,
        )

        init_token()
        await db.init_db()

        raw = generate_token()
        fp = token_fingerprint(raw)
        await db.create_token(
            name="test-scoped",
            token_hash=fp,
            scopes=["tasks:create", "tasks:read"],
        )

        result = await verify_token_with_scopes(raw)
        assert result.is_legacy is False
        assert result.has_scope("tasks:create") is True
        assert result.has_scope("admin:tokens") is False
        assert result.token_name == "test-scoped"

    @pytest.mark.asyncio
    async def test_verify_revoked_token_rejected(self):
        from fastapi import HTTPException

        from sandclaude.auth import (
            generate_token,
            init_token,
            token_fingerprint,
            verify_token_with_scopes,
        )

        init_token()
        await db.init_db()

        raw = generate_token()
        fp = token_fingerprint(raw)
        token_info = await db.create_token(
            name="revokable",
            token_hash=fp,
            scopes=["tasks:read"],
        )
        await db.revoke_token(token_info.id)

        with pytest.raises(HTTPException) as exc_info:
            await verify_token_with_scopes(raw)
        assert exc_info.value.status_code == 401
        assert "revoked" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_require_scope_raises(self):
        from fastapi import HTTPException

        from sandclaude.auth import AuthResult, require_scope

        auth = AuthResult(
            token="x",
            fingerprint="fp",
            is_legacy=False,
            scopes=["tasks:read"],
        )
        # Should not raise
        require_scope(auth, "tasks:read")
        # Should raise
        with pytest.raises(HTTPException) as exc_info:
            require_scope(auth, "admin:tokens")
        assert exc_info.value.status_code == 403
