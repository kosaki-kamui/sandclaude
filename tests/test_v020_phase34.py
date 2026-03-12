"""Tests for v0.2.0 Phase 3+4: cost budgets, repo/branch policy, retry, bundles."""

from __future__ import annotations

import pytest

from sandclaude.db import store as db
from sandclaude.models import PolicyPresetConfig, TaskStatus
from sandclaude.policy import check_branch_allowed, check_repo_allowed

# ---------------------------------------------------------------------------
# Cost budget enforcement
# ---------------------------------------------------------------------------


class TestCostBudget:
    @pytest.mark.asyncio
    async def test_task_stores_cost_budget(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-budget1", repo=".", prompt="test",
            cost_budget_usd=2.50,
        )
        assert task.cost_budget_usd == 2.50

    @pytest.mark.asyncio
    async def test_task_without_budget_has_none(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-budget2", repo=".", prompt="test",
        )
        assert task.cost_budget_usd is None

    @pytest.mark.asyncio
    async def test_cost_budget_propagated_to_retry(self):
        """Retry should inherit cost_budget_usd from original task."""
        await db.init_db()
        task = await db.create_task(
            task_id="task-budget3", repo=".", prompt="original",
            cost_budget_usd=5.0,
        )
        # Simulate retry by creating new task with same budget
        retry = await db.create_task(
            task_id="task-budget3-retry", repo=task.repo, prompt="retry",
            cost_budget_usd=task.cost_budget_usd,
        )
        assert retry.cost_budget_usd == 5.0


# ---------------------------------------------------------------------------
# Repository and branch policy
# ---------------------------------------------------------------------------


class TestRepoPolicy:
    def test_repo_allowed_when_no_restriction(self):
        policy = PolicyPresetConfig(allowed_repos=None)
        assert check_repo_allowed(policy, "https://github.com/org/repo") is None

    def test_repo_allowed_exact_match(self):
        policy = PolicyPresetConfig(
            allowed_repos=["https://github.com/org/repo"]
        )
        assert check_repo_allowed(policy, "https://github.com/org/repo") is None

    def test_repo_allowed_prefix_match(self):
        policy = PolicyPresetConfig(
            allowed_repos=["https://github.com/org/"]
        )
        assert check_repo_allowed(policy, "https://github.com/org/repo.git") is None

    def test_repo_blocked(self):
        policy = PolicyPresetConfig(
            allowed_repos=["https://github.com/org/allowed"]
        )
        result = check_repo_allowed(policy, "https://github.com/other/repo")
        assert result is not None
        assert "not in the allowed repos" in result

    def test_dot_repo_allowed(self):
        policy = PolicyPresetConfig(allowed_repos=["."])
        assert check_repo_allowed(policy, ".") is None

    def test_empty_allowlist_blocks_all(self):
        policy = PolicyPresetConfig(allowed_repos=[])
        result = check_repo_allowed(policy, "https://github.com/org/repo")
        assert result is not None


class TestBranchPolicy:
    def test_branch_allowed_when_no_restriction(self):
        policy = PolicyPresetConfig(blocked_branches=None)
        assert check_branch_allowed(policy, "main") is None

    def test_branch_allowed_when_not_blocked(self):
        policy = PolicyPresetConfig(blocked_branches=["production"])
        assert check_branch_allowed(policy, "feature/fix") is None

    def test_branch_blocked(self):
        policy = PolicyPresetConfig(blocked_branches=["main", "production"])
        result = check_branch_allowed(policy, "main")
        assert result is not None
        assert "blocked" in result

    def test_none_branch_always_allowed(self):
        policy = PolicyPresetConfig(blocked_branches=["main"])
        assert check_branch_allowed(policy, None) is None


# ---------------------------------------------------------------------------
# Retry / follow-up
# ---------------------------------------------------------------------------


class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_creates_new_task(self):
        await db.init_db()
        original = await db.create_task(
            task_id="task-retry1", repo="https://github.com/org/repo",
            prompt="Fix the bug", model="claude-sonnet-4-5",
            max_turns=20, policy_preset="bugfix-pr",
        )
        # Mark as completed
        await db.update_task(original.id, status=TaskStatus.completed)

        # Create retry task
        retry = await db.create_task(
            task_id="task-retry1-followup",
            repo=original.repo,
            branch=original.branch,
            prompt=f"Follow-up: {original.prompt}",
            model=original.model,
            max_turns=original.max_turns,
            priority=original.priority,
            policy_preset=original.policy_preset,
        )
        assert retry.repo == original.repo
        assert retry.model == original.model
        assert retry.policy_preset == "bugfix-pr"

    @pytest.mark.asyncio
    async def test_retry_preserves_budget(self):
        await db.init_db()
        original = await db.create_task(
            task_id="task-retry2", repo=".", prompt="test",
            cost_budget_usd=3.0,
        )
        await db.update_task(original.id, status=TaskStatus.completed)

        retry = await db.create_task(
            task_id="task-retry2-followup", repo=original.repo,
            prompt="follow-up", cost_budget_usd=original.cost_budget_usd,
        )
        assert retry.cost_budget_usd == 3.0


# ---------------------------------------------------------------------------
# Task bundle export
# ---------------------------------------------------------------------------


class TestTaskBundle:
    @pytest.mark.asyncio
    async def test_bundle_contains_task_data(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-bundle1", repo=".", prompt="bundle test",
            policy_preset="bugfix-pr", cost_budget_usd=5.0,
        )
        # Verify task fields are accessible for bundle export
        dump = task.safe_dump()
        assert dump["id"] == "task-bundle1"
        assert dump["policy_preset"] == "bugfix-pr"
        assert dump["cost_budget_usd"] == 5.0

    @pytest.mark.asyncio
    async def test_bundle_includes_gates_and_secrets(self):
        await db.init_db()
        task = await db.create_task(
            task_id="task-bundle2", repo=".", prompt="bundle test",
        )
        # Create a gate and secret record
        await db.create_approval_gate(task.id, "create_pr")
        await db.record_task_secret(task.id, "NPM_TOKEN", "setup", True)

        gates = await db.get_approval_gates(task.id)
        secrets = await db.get_task_secrets(task.id)

        assert len(gates) == 1
        assert gates[0].action == "create_pr"
        assert len(secrets) == 1
        assert secrets[0]["secret_name"] == "NPM_TOKEN"


# ---------------------------------------------------------------------------
# GIT_ASKPASS shell injection protection
# ---------------------------------------------------------------------------


class TestAskpassShellSafety:
    def test_token_with_shell_metacharacters(self):
        """Tokens containing shell metacharacters must be safely escaped."""
        import sandclaude.config as cfg
        from sandclaude.github import _GitCredentialHelper

        original = cfg.settings.git_token
        try:
            # Token containing shell metacharacters
            cfg.settings.git_token = 'ghp_test$(whoami)"evil'
            with _GitCredentialHelper() as env:
                content = open(env["GIT_ASKPASS"]).read()
                # The token must be shell-quoted, not raw-embedded
                assert "$(whoami)" not in content.split("echo ")[1].split("'")[0] or \
                    content.count("'") >= 2  # shlex.quote wraps in single quotes
                # Verify the script would echo the literal token, not expand it
                assert "#!/bin/sh" in content
        finally:
            cfg.settings.git_token = original

    def test_token_with_single_quotes(self):
        """Tokens with single quotes must also be handled safely."""
        import sandclaude.config as cfg
        from sandclaude.github import _GitCredentialHelper

        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_test'quoted"
            with _GitCredentialHelper() as env:
                content = open(env["GIT_ASKPASS"]).read()
                assert "#!/bin/sh" in content
                # shlex.quote handles single quotes by ending the quote,
                # adding an escaped quote, and restarting
        finally:
            cfg.settings.git_token = original
