"""Tests for v0.2.5 pre-flight budget estimation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sandclaude.estimator import (
    BudgetEstimate,
    apply_safety_rule,
    estimate_static,
    run_budget_check,
)
from sandclaude.models import TaskStatus

# ---------------------------------------------------------------------------
# Static estimator
# ---------------------------------------------------------------------------


class TestStaticEstimator:
    def test_basic_estimate(self):
        est = estimate_static(
            model="claude-sonnet-4-5",
            max_turns=10,
            prompt_length=200,
        )
        assert est.predicted_total_usd > 0
        assert est.mode == "static"
        assert est.confidence in ("low", "medium", "high")

    def test_higher_turns_costs_more(self):
        low = estimate_static(model="claude-sonnet-4-5", max_turns=5, prompt_length=200)
        high = estimate_static(model="claude-sonnet-4-5", max_turns=50, prompt_length=200)
        assert high.predicted_total_usd > low.predicted_total_usd

    def test_opus_costs_more_than_sonnet(self):
        sonnet = estimate_static(model="claude-sonnet-4-5", max_turns=10, prompt_length=200)
        opus = estimate_static(model="claude-opus-4-6", max_turns=10, prompt_length=200)
        assert opus.predicted_total_usd > sonnet.predicted_total_usd

    def test_large_prompt_adds_cost(self):
        short = estimate_static(model="claude-sonnet-4-5", max_turns=10, prompt_length=100)
        long = estimate_static(model="claude-sonnet-4-5", max_turns=10, prompt_length=100000)
        assert long.predicted_total_usd > short.predicted_total_usd

    def test_addon_calls_add_cost(self):
        base = estimate_static(model="claude-sonnet-4-5", max_turns=10, prompt_length=200)
        with_addons = estimate_static(
            model="claude-sonnet-4-5",
            max_turns=10,
            prompt_length=200,
            has_review=True,
            has_ai_pr_title=True,
            has_ai_pr_summary=True,
        )
        assert with_addons.predicted_total_usd > base.predicted_total_usd
        assert "review_mode_enabled" in with_addons.reason_codes

    def test_small_task_high_confidence(self):
        est = estimate_static(model="claude-sonnet-4-5", max_turns=5, prompt_length=200)
        assert est.confidence == "high"

    def test_large_task_low_confidence(self):
        est = estimate_static(model="claude-sonnet-4-5", max_turns=100, prompt_length=200)
        assert est.confidence == "low"
        assert "high_turn_count" in est.reason_codes

    def test_unknown_model_uses_default_pricing(self):
        est = estimate_static(model="unknown-model-v99", max_turns=10, prompt_length=200)
        assert est.predicted_total_usd > 0
        assert "model_unknown_pricing" in est.reason_codes
        assert est.confidence == "low"

    def test_estimate_breakdown_sums_correctly(self):
        est = estimate_static(model="claude-sonnet-4-5", max_turns=10, prompt_length=200)
        expected_total = est.predicted_input_cost_usd + est.predicted_output_cost_usd
        # Allow small float rounding
        assert abs(est.predicted_total_usd - expected_total) < 0.01


# ---------------------------------------------------------------------------
# Safety rule
# ---------------------------------------------------------------------------


class TestSafetyRule:
    def test_no_model_estimate_returns_static(self):
        static = BudgetEstimate(predicted_total_usd=2.0)
        result = apply_safety_rule(static, None)
        assert result.predicted_total_usd == 2.0

    def test_model_higher_uses_model_max(self):
        static = BudgetEstimate(predicted_total_usd=2.0)
        model = BudgetEstimate(
            predicted_total_usd=3.0,
            model_max_usd=4.0,
            model_min_usd=1.5,
            model_predicted_total_usd=3.0,
        )
        result = apply_safety_rule(static, model)
        assert result.predicted_total_usd == 4.0
        assert "safety_rule_model_max" in result.reason_codes

    def test_static_higher_keeps_static(self):
        static = BudgetEstimate(predicted_total_usd=5.0)
        model = BudgetEstimate(
            predicted_total_usd=2.0,
            model_max_usd=3.0,
            model_min_usd=1.0,
            model_predicted_total_usd=2.0,
        )
        result = apply_safety_rule(static, model)
        assert result.predicted_total_usd == 5.0
        assert "safety_rule_static_higher" in result.reason_codes


# ---------------------------------------------------------------------------
# Budget check pipeline
# ---------------------------------------------------------------------------


class TestBudgetCheck:
    @pytest.mark.asyncio
    async def test_no_budget_skips_check(self):
        result = await run_budget_check(
            model="claude-sonnet-4-5",
            max_turns=10,
            prompt="test",
            max_budget_usd=None,
        )
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_within_budget_passes(self):
        result = await run_budget_check(
            model="claude-sonnet-4-5",
            max_turns=5,
            prompt="Fix a typo in README",
            max_budget_usd=10.0,
        )
        assert result["status"] == "passed"
        assert result["predicted_total_usd"] <= 10.0

    @pytest.mark.asyncio
    async def test_exceeds_budget_rejects(self):
        result = await run_budget_check(
            model="claude-opus-4-6",
            max_turns=100,
            prompt="Refactor the entire codebase",
            max_budget_usd=0.01,
            budget_fail_policy="reject",
        )
        assert result["status"] == "rejected"
        assert "exceeds" in result["message"]

    @pytest.mark.asyncio
    async def test_exceeds_budget_warns(self):
        result = await run_budget_check(
            model="claude-opus-4-6",
            max_turns=100,
            prompt="Refactor everything",
            max_budget_usd=0.01,
            budget_fail_policy="warn",
        )
        assert result["status"] == "warning"

    @pytest.mark.asyncio
    async def test_exceeds_budget_requires_approval(self):
        result = await run_budget_check(
            model="claude-opus-4-6",
            max_turns=100,
            prompt="Refactor everything",
            max_budget_usd=0.01,
            budget_fail_policy="require_approval",
        )
        assert result["status"] == "requires_approval"

    @pytest.mark.asyncio
    async def test_estimate_includes_model_info(self):
        result = await run_budget_check(
            model="claude-sonnet-4-5",
            max_turns=10,
            prompt="test",
            max_budget_usd=100.0,
        )
        assert "predicted_total_usd" in result
        assert "confidence" in result
        assert "reason_codes" in result


# ---------------------------------------------------------------------------
# API integration: budget gate at task creation
# ---------------------------------------------------------------------------


class TestBudgetGateAPI:
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
    async def test_task_with_budget_includes_check(self, mock_submit, client):
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Fix a typo",
                "cost_budget_usd": 10.0,
                "max_turns": 5,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "budget_check" in data
        assert data["budget_check"]["status"] == "passed"

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_task_without_budget_no_check(self, mock_submit, client):
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "Fix a typo"},
        )
        assert resp.status_code == 201
        assert "budget_check" not in resp.json()

    async def test_task_exceeding_budget_rejected(self, client):
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Refactor everything in the entire codebase",
                "cost_budget_usd": 0.001,
                "model": "claude-opus-4-6",
                "max_turns": 200,
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["budget_check"]["status"] == "rejected"

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_require_approval_blocks_execution(self, mock_submit, client):
        """require_approval must NOT call submit_task."""
        # Create a preset with require_approval policy and a tiny budget
        await client.put(
            "/policies/strict-budget",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Refactor the entire codebase",
                "policy_preset": "strict-budget",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["budget_check"]["status"] == "requires_approval"
        # Task must NOT have been submitted for execution
        mock_submit.assert_not_called()

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_preset_budget_enforced_without_task_budget(self, mock_submit, client):
        """Preset max_cost_usd must trigger estimation even if task omits cost_budget_usd."""
        await client.put(
            "/policies/preset-budget-only",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "reject",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Refactor everything",
                "policy_preset": "preset-budget-only",
                "model": "claude-opus-4-6",
                "max_turns": 100,
                # NOTE: no cost_budget_usd on the task
            },
        )
        assert resp.status_code == 422
        mock_submit.assert_not_called()

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_task_cannot_widen_budget_above_preset(self, mock_submit, client):
        """Task cost_budget_usd=100 must be capped by preset max_cost_usd=0.001."""
        await client.put(
            "/policies/tight-cap",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "reject",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Refactor everything",
                "policy_preset": "tight-cap",
                "cost_budget_usd": 100.0,  # tries to widen
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        # Preset cap wins — task is rejected at $0.001
        assert resp.status_code == 422
        mock_submit.assert_not_called()

    @patch("sandclaude.api.approvals.submit_task", new_callable=AsyncMock)
    async def test_budget_approval_resumes_execution(self, mock_submit, client):
        """Approving budget_exceeded gate must transition task to queued and submit."""
        await client.put(
            "/policies/approve-budget",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        # Create task — should be pending_approval
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Expensive task",
                "policy_preset": "approve-budget",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "pending_approval"
        assert data["requires_approval"] == 1
        task_id = data["id"]
        mock_submit.assert_not_called()

        # Approve the budget gate
        resp2 = await client.post(f"/tasks/{task_id}/approve/budget_exceeded")
        assert resp2.status_code == 200
        assert resp2.json()["execution"] == "resumed"
        mock_submit.assert_called_once()

        # Task should now be queued
        from sandclaude.db import store as _db

        task = await _db.get_task(task_id)
        assert task.status.value == "queued"

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_budget_response_reflects_pending_approval(self, mock_submit, client):
        """Task creation response must show pending_approval, not stale queued."""
        await client.put(
            "/policies/stale-check",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Test stale response",
                "policy_preset": "stale-check",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        # Must reflect the DB state, not the stale in-memory object
        assert data["status"] == "pending_approval"
        assert data["requires_approval"] == 1

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_budget_rejection_fails_task(self, mock_submit, client):
        """Rejecting budget_exceeded must mark task as failed, not leave it stuck."""
        await client.put(
            "/policies/reject-budget",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Expensive task",
                "policy_preset": "reject-budget",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        task_id = resp.json()["id"]
        mock_submit.assert_not_called()

        # Reject the budget gate
        resp2 = await client.post(f"/tasks/{task_id}/reject/budget_exceeded")
        assert resp2.status_code == 200

        # Task must be failed, not stuck in pending_approval
        from sandclaude.db import store as _db

        task = await _db.get_task(task_id)
        assert task.status == TaskStatus.failed
        assert task.error == "budget_approval_rejected"
        assert task.requires_approval == 0

    async def test_retry_enforces_budget(self, client):
        """Retry must run budget check — cannot bypass preset cap."""
        await client.put(
            "/policies/retry-budget",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "reject",
            },
        )
        from sandclaude.auth import get_token, token_fingerprint
        from sandclaude.db import store as _db

        original = await _db.create_task(
            task_id="task-retry-budget",
            repo=".",
            prompt="original",
            model="claude-opus-4-6",
            max_turns=100,
            policy_preset="retry-budget",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await _db.update_task(original.id, status=TaskStatus.completed)

        # Retry should be rejected by budget
        resp = await client.post(
            f"/tasks/{original.id}/retry",
            json={"prompt": "Also fix tests"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["budget_check"]["status"] == "rejected"


# ---------------------------------------------------------------------------
# budget_check_json persistence and retrieval
# ---------------------------------------------------------------------------


class TestBudgetCheckPersistence:
    """Tests for budget_check_json storage, retrieval, fallback, and live status."""

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

    # -- 1. POST /tasks persists budget_check_json -------------------------

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_post_tasks_persists_budget_check_json(self, mock_submit, client):
        """POST /tasks with a budget must store budget_check_json in the DB."""
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Fix a typo",
                "cost_budget_usd": 10.0,
                "max_turns": 5,
            },
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        from sandclaude.db import store as _db

        task = await _db.get_task(task_id)
        assert task.budget_check_json is not None
        import json

        stored = json.loads(task.budget_check_json)
        assert stored["status"] == "passed"
        assert "predicted_total_usd" in stored
        assert "max_budget_usd" in stored

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_post_tasks_no_budget_no_json(self, mock_submit, client):
        """POST /tasks without a budget must NOT store budget_check_json."""
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "Fix a typo"},
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        from sandclaude.db import store as _db

        task = await _db.get_task(task_id)
        assert task.budget_check_json is None

    # -- 2. POST /tasks/{id}/retry persists budget_check_json ---------------

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_retry_persists_budget_check_json(self, mock_submit, client):
        """Retry with a budget must store budget_check_json on the new task."""
        from sandclaude.auth import get_token, token_fingerprint
        from sandclaude.db import store as _db

        original = await _db.create_task(
            task_id="task-retry-persist",
            repo=".",
            prompt="original",
            model="claude-sonnet-4-5",
            max_turns=5,
            cost_budget_usd=10.0,
            policy_preset=None,
            owner_token_hash=token_fingerprint(get_token()),
        )
        await _db.update_task(original.id, status=TaskStatus.completed)

        resp = await client.post(
            f"/tasks/{original.id}/retry",
            json={"prompt": "Follow up"},
        )
        assert resp.status_code == 201
        new_task_id = resp.json()["id"]

        new_task = await _db.get_task(new_task_id)
        assert new_task.budget_check_json is not None
        import json

        stored = json.loads(new_task.budget_check_json)
        assert stored["status"] == "passed"
        assert "predicted_total_usd" in stored

    # -- 3. GET /tasks/{id} returns budget data from stored JSON ------------

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_get_task_returns_stored_budget_check(self, mock_submit, client):
        """GET /tasks/{id} must return budget_check from stored JSON, not recomputed."""
        await client.put(
            "/policies/persist-test",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Expensive task",
                "policy_preset": "persist-test",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        creation_check = resp.json()["budget_check"]

        # GET should return the same predicted_total_usd as admission time
        get_resp = await client.get(f"/tasks/{task_id}")
        assert get_resp.status_code == 200
        get_check = get_resp.json()["budget_check"]
        assert get_check["predicted_total_usd"] == creation_check["predicted_total_usd"]
        assert get_check["gate_status"] == "pending"

    # -- 4. Approval UI renders budget data from stored JSON ----------------

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_approval_ui_renders_stored_budget(self, mock_submit, client):
        """Approval UI must render budget card from stored budget_check_json."""
        await client.put(
            "/policies/ui-persist",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Expensive task for UI",
                "policy_preset": "ui-persist",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        creation_check = resp.json()["budget_check"]

        # Generate an approval link and fetch the page
        link_resp = await client.post(f"/tasks/{task_id}/approval-link/budget_exceeded")
        assert link_resp.status_code == 200
        approval_url = link_resp.json()["approval_url"]
        # Extract token query param
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(approval_url)
        link_token = parse_qs(parsed.query)["token"][0]

        page_resp = await client.get(
            f"/approve/{task_id}/budget_exceeded",
            params={"token": link_token},
        )
        assert page_resp.status_code == 200
        html = page_resp.text
        # The stored predicted cost should appear in the page
        assert f"${creation_check['predicted_total_usd']:.4f}" in html
        assert "$0.00" in html  # max_budget_usd formatted as $0.00

    # -- 5. Pre-upgrade fallback: budget gate without stored JSON -----------

    async def test_fallback_for_pre_upgrade_task(self, client):
        """Tasks with budget_exceeded gate but no budget_check_json must still
        show budget information via the recomputation fallback."""
        from sandclaude.auth import get_token, token_fingerprint
        from sandclaude.db import store as _db

        # Simulate a pre-upgrade task: create directly in DB with no
        # budget_check_json, then manually add a budget_exceeded gate.
        task = await _db.create_task(
            task_id="task-preupgrade",
            repo=".",
            prompt="pre-upgrade task",
            model="claude-opus-4-6",
            max_turns=100,
            cost_budget_usd=0.001,
            owner_token_hash=token_fingerprint(get_token()),
        )
        await _db.create_approval_gate(task.id, "budget_exceeded")
        await _db.update_task(
            task.id,
            requires_approval=1,
            status=TaskStatus.pending_approval,
        )

        # Confirm budget_check_json is NULL
        raw = await _db.get_task(task.id)
        assert raw.budget_check_json is None

        # GET should still surface budget_check via fallback
        get_resp = await client.get(f"/tasks/{task.id}")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert "budget_check" in data
        assert data["budget_check"]["gate_status"] == "pending"
        assert data["budget_check"]["predicted_total_usd"] > 0
        assert data["budget_check"]["max_budget_usd"] == 0.001

    async def test_approval_ui_fallback_for_pre_upgrade_task(self, client):
        """Approval UI must render budget card for pre-upgrade tasks via fallback."""
        from sandclaude.auth import get_token, token_fingerprint
        from sandclaude.db import store as _db

        task = await _db.create_task(
            task_id="task-preupgrade-ui",
            repo=".",
            prompt="pre-upgrade task ui",
            model="claude-opus-4-6",
            max_turns=100,
            cost_budget_usd=0.001,
            owner_token_hash=token_fingerprint(get_token()),
        )
        await _db.create_approval_gate(task.id, "budget_exceeded")
        await _db.update_task(
            task.id,
            requires_approval=1,
            status=TaskStatus.pending_approval,
        )

        # Generate approval link and fetch page
        link_resp = await client.post(f"/tasks/{task.id}/approval-link/budget_exceeded")
        assert link_resp.status_code == 200
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(link_resp.json()["approval_url"])
        link_token = parse_qs(parsed.query)["token"][0]

        page_resp = await client.get(
            f"/approve/{task.id}/budget_exceeded",
            params={"token": link_token},
        )
        assert page_resp.status_code == 200
        html = page_resp.text
        # Budget card should be rendered with fallback-computed values
        assert "Budget Estimate" in html
        assert "$0.00" in html  # max_budget_usd

    # -- 6. Approval UI shows live gate status, not stale decision ----------

    @patch("sandclaude.api.approvals.submit_task", new_callable=AsyncMock)
    async def test_approval_ui_shows_live_approved_status(self, mock_submit, client):
        """After approving a budget gate, the UI must show 'approved', not 'requires_approval'."""
        await client.put(
            "/policies/live-status",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Expensive task live status",
                "policy_preset": "live-status",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        # Approve the budget gate
        await client.post(f"/tasks/{task_id}/approve/budget_exceeded")

        # Generate a new approval link (task may have post-execution gates now)
        # For this test, we directly fetch the approval page for budget_exceeded
        link_resp = await client.post(f"/tasks/{task_id}/approval-link/budget_exceeded")
        assert link_resp.status_code == 200
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(link_resp.json()["approval_url"])
        link_token = parse_qs(parsed.query)["token"][0]

        page_resp = await client.get(
            f"/approve/{task_id}/budget_exceeded",
            params={"token": link_token},
        )
        assert page_resp.status_code == 200
        html = page_resp.text
        # The budget card must show "approved", NOT "requires_approval"
        assert "approved" in html
        # Should NOT contain the stale admission-time status
        assert "requires_approval" not in html

    @patch("sandclaude.api.approvals.submit_task", new_callable=AsyncMock)
    async def test_get_task_shows_live_gate_status_after_approval(self, mock_submit, client):
        """GET /tasks/{id} budget_check.gate_status must reflect live gate state."""
        await client.put(
            "/policies/gate-status",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Gate status test",
                "policy_preset": "gate-status",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        task_id = resp.json()["id"]

        # Before approval — gate_status should be "pending"
        get1 = await client.get(f"/tasks/{task_id}")
        assert get1.json()["budget_check"]["gate_status"] == "pending"

        # Approve
        await client.post(f"/tasks/{task_id}/approve/budget_exceeded")

        # After approval — gate_status should be "approved"
        get2 = await client.get(f"/tasks/{task_id}")
        assert get2.json()["budget_check"]["gate_status"] == "approved"

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_get_task_shows_rejected_gate_status(self, mock_submit, client):
        """GET /tasks/{id} budget_check.gate_status must show 'rejected' after rejection."""
        await client.put(
            "/policies/reject-gate-status",
            json={
                "max_cost_usd": 0.001,
                "budget_fail_policy": "require_approval",
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Reject gate status test",
                "policy_preset": "reject-gate-status",
                "model": "claude-opus-4-6",
                "max_turns": 100,
            },
        )
        task_id = resp.json()["id"]

        # Reject the budget gate
        await client.post(f"/tasks/{task_id}/reject/budget_exceeded")

        # gate_status should be "rejected"
        get_resp = await client.get(f"/tasks/{task_id}")
        assert get_resp.json()["budget_check"]["gate_status"] == "rejected"
