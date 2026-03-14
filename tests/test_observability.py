"""API-level integration tests for v0.3.0 observability features."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import get_token, init_token, token_fingerprint
from sandclaude.db import store as db
from sandclaude.db.store import init_db
from sandclaude.models import TaskStatus


@pytest.fixture(autouse=True)
async def _setup(tmp_path):
    import sandclaude.config as cfg
    import sandclaude.db.store as store

    cfg.settings.data_dir = tmp_path
    cfg.settings.anthropic_api_key = "test-key"
    cfg.settings.environment = "test"
    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()
    init_token()


@pytest.fixture
async def client():
    token = get_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_task(task_id: str, **kwargs) -> None:
    """Create a task directly via db for test setup."""
    defaults = {
        "task_id": task_id,
        "repo": "https://github.com/org/repo",
        "prompt": "test prompt",
        "owner_token_hash": token_fingerprint(get_token()),
    }
    defaults.update(kwargs)
    await db.create_task(**defaults)


# ---------------------------------------------------------------------------
# test_metrics_endpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    async def test_metrics_endpoint(self, client: AsyncClient):
        """GET /metrics returns expected structure with correct aggregation."""
        # Create tasks with different statuses and costs
        await _create_task("task-m1")
        await db.update_task(
            "task-m1",
            status=TaskStatus.completed,
            total_cost_usd=1.50,
            tokens_input=1000,
            tokens_output=500,
        )

        await _create_task("task-m2")
        await db.update_task(
            "task-m2",
            status=TaskStatus.completed,
            total_cost_usd=2.00,
            tokens_input=2000,
            tokens_output=800,
        )

        await _create_task("task-m3")
        await db.update_task(
            "task-m3",
            status=TaskStatus.failed,
            error="something broke",
            error_category="container_error",
        )

        await _create_task("task-m4")
        # stays queued

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()

        # Top-level structure
        assert "tasks" in data
        assert "cost" in data
        assert "tokens" in data
        assert "timing" in data
        assert "recent_24h" in data

        # Task counts
        assert data["tasks"]["total"] == 4
        assert data["tasks"]["by_status"]["completed"] == 2
        assert data["tasks"]["by_status"]["failed"] == 1
        assert data["tasks"]["by_status"]["queued"] == 1

        # Cost aggregation
        assert data["cost"]["total_usd"] == 3.5

        # Token aggregation
        assert data["tokens"]["total_input"] == 3000
        assert data["tokens"]["total_output"] == 1300

        # Recent 24h — all tasks were just created so they should count
        assert data["recent_24h"]["task_count"] == 4


# ---------------------------------------------------------------------------
# test_task_timeline_in_response
# ---------------------------------------------------------------------------


class TestTaskTimelineInResponse:
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_task_timeline_in_response(self, mock_submit, client: AsyncClient):
        """POST /tasks then GET /tasks/{id} should include timeline field."""
        resp = await client.post(
            "/tasks",
            json={"repo": "https://github.com/org/repo", "prompt": "do something"},
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        # Simulate phase transitions by setting timestamps
        now = datetime.now(timezone.utc)
        await db.update_task(
            task_id,
            status=TaskStatus.completed,
            started_at=(now + timedelta(seconds=5)).isoformat(),
            setup_completed_at=(now + timedelta(seconds=15)).isoformat(),
            agent_started_at=(now + timedelta(seconds=16)).isoformat(),
            completed_at=(now + timedelta(seconds=60)).isoformat(),
        )

        resp = await client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()

        assert "timeline" in data
        timeline = data["timeline"]
        assert "queued_duration_s" in timeline
        assert "setup_duration_s" in timeline
        assert "agent_duration_s" in timeline
        assert "total_duration_s" in timeline

        # Verify durations are reasonable numbers (not None)
        assert timeline["setup_duration_s"] is not None
        assert timeline["agent_duration_s"] is not None
        assert timeline["total_duration_s"] is not None


# ---------------------------------------------------------------------------
# test_timeline_endpoint
# ---------------------------------------------------------------------------


class TestTimelineEndpoint:
    async def test_timeline_endpoint(self, client: AsyncClient):
        """GET /tasks/{id}/timeline returns phase breakdown and status."""
        await _create_task("task-tl1")
        now = datetime.now(timezone.utc)
        await db.update_task(
            "task-tl1",
            status=TaskStatus.completed,
            started_at=(now + timedelta(seconds=2)).isoformat(),
            setup_completed_at=(now + timedelta(seconds=10)).isoformat(),
            agent_started_at=(now + timedelta(seconds=11)).isoformat(),
            completed_at=(now + timedelta(seconds=50)).isoformat(),
        )

        resp = await client.get("/tasks/task-tl1/timeline")
        assert resp.status_code == 200
        data = resp.json()

        assert data["task_id"] == "task-tl1"
        assert data["status"] == "completed"
        assert "timeline" in data
        assert data["timeline"]["total_duration_s"] is not None


# ---------------------------------------------------------------------------
# test_retry_creates_parent_link
# ---------------------------------------------------------------------------


class TestRetryCreatesParentLink:
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_retry_creates_parent_link(self, mock_submit, client: AsyncClient):
        """POST /tasks/{id}/retry sets parent_task_id on the new task."""
        await _create_task("task-parent")
        await db.update_task("task-parent", status=TaskStatus.completed)

        resp = await client.post(
            "/tasks/task-parent/retry",
            json={"prompt": "fix the tests"},
        )
        assert resp.status_code == 201
        new_task_id = resp.json()["id"]

        # Verify parent link via DB
        new_task = await db.get_task(new_task_id)
        assert new_task is not None
        assert new_task.parent_task_id == "task-parent"


# ---------------------------------------------------------------------------
# test_retry_chain_in_timeline
# ---------------------------------------------------------------------------


class TestRetryChainInTimeline:
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_retry_chain_in_timeline(self, mock_submit, client: AsyncClient):
        """A chain (A -> B -> C) should show retry_chain with all 3 in GET /tasks/C/timeline."""
        # Task A (original)
        await _create_task("task-chain-a")
        await db.update_task("task-chain-a", status=TaskStatus.completed)

        # Task B (retry of A)
        resp_b = await client.post(
            "/tasks/task-chain-a/retry",
            json={"prompt": "retry attempt 1"},
        )
        assert resp_b.status_code == 201
        task_b_id = resp_b.json()["id"]
        await db.update_task(task_b_id, status=TaskStatus.completed)

        # Task C (retry of B)
        resp_c = await client.post(
            f"/tasks/{task_b_id}/retry",
            json={"prompt": "retry attempt 2"},
        )
        assert resp_c.status_code == 201
        task_c_id = resp_c.json()["id"]

        # GET timeline for C
        resp = await client.get(f"/tasks/{task_c_id}/timeline")
        assert resp.status_code == 200
        data = resp.json()

        assert "retry_chain" in data
        chain = data["retry_chain"]
        assert len(chain) == 3
        chain_ids = [c["id"] for c in chain]
        assert chain_ids[0] == "task-chain-a"
        assert chain_ids[1] == task_b_id
        assert chain_ids[2] == task_c_id


# ---------------------------------------------------------------------------
# test_error_category_on_budget_rejection
# ---------------------------------------------------------------------------


class TestErrorCategoryOnBudgetRejection:
    async def test_error_category_on_budget_rejection(self, client: AsyncClient):
        """POST /tasks/{id}/reject/budget_exceeded sets error_category='approval_rejected'."""
        await _create_task("task-budget-rej")
        await db.create_approval_gate("task-budget-rej", "budget_exceeded")
        await db.update_task(
            "task-budget-rej",
            status=TaskStatus.pending_approval,
            requires_approval=1,
        )

        resp = await client.post(
            "/tasks/task-budget-rej/reject/budget_exceeded",
            json={"reason": "Too expensive"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        # Verify error_category was set on the task
        task = await db.get_task("task-budget-rej")
        assert task is not None
        assert task.error_category == "approval_rejected"
        assert task.status == TaskStatus.failed


# ---------------------------------------------------------------------------
# test_metrics_counts_error_categories
# ---------------------------------------------------------------------------


class TestMetricsCountsErrorCategories:
    async def test_metrics_counts_error_categories(self, client: AsyncClient):
        """GET /metrics includes by_error_category counts."""
        await _create_task("task-ec1")
        await db.update_task("task-ec1", status=TaskStatus.failed, error_category="container_error")

        await _create_task("task-ec2")
        await db.update_task("task-ec2", status=TaskStatus.failed, error_category="container_error")

        await _create_task("task-ec3")
        await db.update_task(
            "task-ec3", status=TaskStatus.failed, error_category="approval_rejected"
        )

        await _create_task("task-ec4")
        await db.update_task("task-ec4", status=TaskStatus.completed)

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()

        by_error = data["tasks"]["by_error_category"]
        assert by_error["container_error"] == 2
        assert by_error["approval_rejected"] == 1
        # Completed task without error_category should not appear
        assert "None" not in by_error


# ---------------------------------------------------------------------------
# test_version_030
# ---------------------------------------------------------------------------


class TestVersion030:
    async def test_version_030(self, client: AsyncClient):
        """GET /health returns version '0.3.0'."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "0.4.0"
        assert data["status"] == "ok"


class TestMetricsAuth:
    async def test_metrics_requires_admin_scope(self, client):
        """GET /metrics must require admin:policies scope."""
        from sandclaude.auth import generate_token, token_fingerprint
        from sandclaude.db import store as _db

        raw = generate_token()
        await _db.create_token(
            name="limited",
            token_hash=token_fingerprint(raw),
            scopes=["tasks:read"],
        )
        resp = await client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403
