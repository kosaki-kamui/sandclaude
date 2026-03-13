"""Tests for v0.3.0 approval policy engine v2 — rule-based conditions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sandclaude.policy import evaluate_approval_rules

# ---------------------------------------------------------------------------
# Rule evaluation (pure logic, no DB)
# ---------------------------------------------------------------------------


class TestEvaluateApprovalRules:
    def test_single_predicate_matches(self):
        rules = [
            {"action": "create_pr", "condition": "auto_approve", "when": {"risk_level": "low"}}
        ]
        result = evaluate_approval_rules(rules, "create_pr", risk_level="low")
        assert result == "auto_approve"

    def test_single_predicate_no_match(self):
        rules = [
            {"action": "create_pr", "condition": "auto_approve", "when": {"risk_level": "low"}}
        ]
        result = evaluate_approval_rules(rules, "create_pr", risk_level="high")
        assert result is None

    def test_multiple_predicates_all_must_match(self):
        rules = [
            {
                "action": "create_pr",
                "condition": "auto_approve",
                "when": {"risk_level": "low", "predicted_cost_below": 1.0},
            }
        ]
        # Both match
        result = evaluate_approval_rules(rules, "create_pr", risk_level="low", predicted_cost=0.5)
        assert result == "auto_approve"

        # Only risk matches, cost too high
        result = evaluate_approval_rules(rules, "create_pr", risk_level="low", predicted_cost=2.0)
        assert result is None

    def test_first_match_wins(self):
        rules = [
            {"action": "create_pr", "condition": "auto_approve", "when": {"risk_level": "low"}},
            {
                "action": "create_pr",
                "condition": "require_approval",
                "when": {"risk_level": "low"},
            },
        ]
        result = evaluate_approval_rules(rules, "create_pr", risk_level="low")
        assert result == "auto_approve"

    def test_wildcard_action(self):
        rules = [{"action": "*", "condition": "auto_approve", "when": {"risk_level": "low"}}]
        result = evaluate_approval_rules(rules, "create_pr", risk_level="low")
        assert result == "auto_approve"

    def test_action_mismatch_skips_rule(self):
        rules = [{"action": "push", "condition": "auto_approve", "when": {"risk_level": "low"}}]
        result = evaluate_approval_rules(rules, "create_pr", risk_level="low")
        assert result is None

    def test_risk_level_list_predicate(self):
        rules = [
            {
                "action": "create_pr",
                "condition": "auto_approve",
                "when": {"risk_level": ["low", "medium"]},
            }
        ]
        assert evaluate_approval_rules(rules, "create_pr", risk_level="low") == "auto_approve"
        assert evaluate_approval_rules(rules, "create_pr", risk_level="medium") == "auto_approve"
        assert evaluate_approval_rules(rules, "create_pr", risk_level="high") is None

    def test_has_secrets_predicate(self):
        rules = [
            {
                "action": "create_pr",
                "condition": "require_approval",
                "when": {"has_secrets": True},
            }
        ]
        assert evaluate_approval_rules(rules, "create_pr", has_secrets=True) == "require_approval"
        assert evaluate_approval_rules(rules, "create_pr", has_secrets=False) is None

    def test_repo_matches_predicate(self):
        rules = [
            {
                "action": "create_pr",
                "condition": "auto_approve",
                "when": {"repo_matches": "https://github.com/safe-org/"},
            }
        ]
        assert (
            evaluate_approval_rules(rules, "create_pr", repo="https://github.com/safe-org/my-repo")
            == "auto_approve"
        )
        assert (
            evaluate_approval_rules(rules, "create_pr", repo="https://github.com/other-org/repo")
            is None
        )

    def test_preset_predicate(self):
        rules = [
            {
                "action": "create_pr",
                "condition": "auto_approve",
                "when": {"preset": "docs-only"},
            }
        ]
        assert (
            evaluate_approval_rules(rules, "create_pr", preset_name="docs-only") == "auto_approve"
        )
        assert evaluate_approval_rules(rules, "create_pr", preset_name="bugfix-pr") is None

    def test_no_rules_returns_none(self):
        assert evaluate_approval_rules([], "create_pr") is None

    def test_risk_level_none_fails_match(self):
        """Risk predicate should not match when risk_level is None (not yet computed)."""
        rules = [
            {"action": "create_pr", "condition": "auto_approve", "when": {"risk_level": "low"}}
        ]
        assert evaluate_approval_rules(rules, "create_pr", risk_level=None) is None

    def test_predicted_cost_none_fails_match(self):
        """Cost predicate should not match when cost is None."""
        rules = [
            {
                "action": "create_pr",
                "condition": "auto_approve",
                "when": {"predicted_cost_below": 1.0},
            }
        ]
        assert evaluate_approval_rules(rules, "create_pr", predicted_cost=None) is None


# ---------------------------------------------------------------------------
# Gate creation with rules (API integration)
# ---------------------------------------------------------------------------


class TestGateCreationWithRules:
    @pytest.fixture(autouse=True)
    async def _setup(self, tmp_path):
        import sandclaude.config as cfg
        import sandclaude.db.store as store

        cfg.settings.data_dir = tmp_path
        cfg.settings.anthropic_api_key = "test-key"
        cfg.settings.environment = "test"
        store.DB_PATH = tmp_path / "tasks.db"
        from sandclaude.db.store import init_db

        await init_db()
        from sandclaude.auth import init_token

        init_token()

    @pytest.fixture
    async def client(self):
        from httpx import ASGITransport, AsyncClient

        from sandclaude.api.main import app
        from sandclaude.auth import get_token

        token = get_token()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c.headers["Authorization"] = f"Bearer {token}"
            yield c

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_auto_approve_skips_pending_gate(self, mock_submit, client):
        """When a rule auto-approves, the gate should be created as approved."""
        await client.put(
            "/policies/auto-approve-test",
            json={
                "requires_approval_for": ["create_pr"],
                "approval_rules": [
                    {
                        "action": "create_pr",
                        "condition": "auto_approve",
                        "when": {"predicted_cost_below": 100.0},
                    }
                ],
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Fix a typo",
                "policy_preset": "auto-approve-test",
                "cost_budget_usd": 10.0,
                "max_turns": 5,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        # Task should NOT be pending_approval since gate was auto-approved
        assert data["requires_approval"] == 0

        # Verify the gate exists but is already approved
        from sandclaude.db import store as _db

        gates = await _db.get_approval_gates(data["id"])
        assert len(gates) == 1
        assert gates[0].action == "create_pr"
        assert gates[0].status.value == "approved"
        assert gates[0].decided_by == "system:auto_approve"

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_require_approval_creates_pending_gate(self, mock_submit, client):
        """When a rule requires approval, the gate should be pending."""
        await client.put(
            "/policies/require-secrets",
            json={
                "requires_approval_for": ["create_pr"],
                "approval_rules": [
                    {
                        "action": "create_pr",
                        "condition": "require_approval",
                        "when": {"has_secrets": True},
                    }
                ],
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Deploy with secrets",
                "policy_preset": "require-secrets",
                "declared_secrets": ["NPM_TOKEN"],
                "max_turns": 5,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["requires_approval"] == 1

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_no_rules_falls_back_to_requires_approval_for(self, mock_submit, client):
        """Without approval_rules, requires_approval_for still works."""
        await client.put(
            "/policies/legacy-style",
            json={"requires_approval_for": ["create_pr"]},
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Fix bug",
                "policy_preset": "legacy-style",
                "max_turns": 5,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["requires_approval"] == 1


# ---------------------------------------------------------------------------
# Post-execution rule evaluation
# ---------------------------------------------------------------------------


class TestPostExecutionRules:
    @pytest.fixture(autouse=True)
    async def _setup(self, tmp_path):
        import sandclaude.config as cfg
        import sandclaude.db.store as store

        cfg.settings.data_dir = tmp_path
        cfg.settings.anthropic_api_key = "test-key"
        cfg.settings.environment = "test"
        store.DB_PATH = tmp_path / "tasks.db"
        from sandclaude.db.store import init_db

        await init_db()
        from sandclaude.auth import init_token

        init_token()

    async def test_post_execution_auto_approves_with_risk(self):
        """Pending gates should be auto-approved when risk matches after execution."""
        from sandclaude.auth import get_token, token_fingerprint
        from sandclaude.db import store as _db
        from sandclaude.models import PolicyPresetConfig

        # Create a task with a pending gate
        task = await _db.create_task(
            task_id="task-post-exec",
            repo=".",
            prompt="test",
            policy_preset="test-preset",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await _db.create_approval_gate(task.id, "create_pr")
        await _db.update_task(task.id, requires_approval=1)

        # Create policy with auto-approve rule for low risk
        policy = PolicyPresetConfig(
            requires_approval_for=["create_pr"],
            approval_rules=[
                {"action": "create_pr", "condition": "auto_approve", "when": {"risk_level": "low"}}
            ],
        )

        from sandclaude.policy import evaluate_post_execution_rules

        count = await evaluate_post_execution_rules(task.id, policy, risk_level="low", repo=".")
        assert count == 1

        # Gate should now be approved
        gates = await _db.get_approval_gates(task.id)
        assert gates[0].status.value == "approved"
        assert gates[0].decided_by == "system:auto_approve"

        # requires_approval should be cleared
        updated = await _db.get_task(task.id)
        assert updated.requires_approval == 0

    async def test_post_execution_no_match_keeps_pending(self):
        """Gates stay pending when risk doesn't match auto-approve rules."""
        from sandclaude.auth import get_token, token_fingerprint
        from sandclaude.db import store as _db
        from sandclaude.models import PolicyPresetConfig

        task = await _db.create_task(
            task_id="task-post-exec-high",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await _db.create_approval_gate(task.id, "create_pr")
        await _db.update_task(task.id, requires_approval=1)

        policy = PolicyPresetConfig(
            requires_approval_for=["create_pr"],
            approval_rules=[
                {"action": "create_pr", "condition": "auto_approve", "when": {"risk_level": "low"}}
            ],
        )

        from sandclaude.policy import evaluate_post_execution_rules

        count = await evaluate_post_execution_rules(task.id, policy, risk_level="high", repo=".")
        assert count == 0

        gates = await _db.get_approval_gates(task.id)
        assert gates[0].status.value == "pending"
