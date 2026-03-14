"""Tests for v0.3.0 task lifecycle ergonomics: labels, search, cancel reason."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import get_token, init_token
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
# Labels
# ---------------------------------------------------------------------------


class TestTaskLabels:
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_create_task_with_labels(self, mock_submit, client):
        """POST /tasks with labels stores them on the task."""
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "Fix bug",
                "labels": ["bugfix", "urgent"],
                "max_turns": 5,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["labels"] == '["bugfix", "urgent"]'

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_filter_tasks_by_label(self, mock_submit, client):
        """GET /tasks?label=X filters to tasks with that label."""
        # Create two tasks with different labels
        await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "A", "labels": ["frontend"], "max_turns": 5},
        )
        await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "B", "labels": ["backend"], "max_turns": 5},
        )
        await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "C", "max_turns": 5},
        )

        # Filter by label
        resp = await client.get("/tasks?label=frontend")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 1
        assert "A" in tasks[0]["prompt"]

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_create_task_without_labels(self, mock_submit, client):
        """POST /tasks without labels sets labels to null."""
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "No labels", "max_turns": 5},
        )
        assert resp.status_code == 201
        assert resp.json()["labels"] is None


# ---------------------------------------------------------------------------
# Search/Filter
# ---------------------------------------------------------------------------


class TestTaskSearch:
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_filter_by_status(self, mock_submit, client):
        """GET /tasks?status=completed filters by status."""
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "A", "max_turns": 5},
        )
        task_id = resp.json()["id"]
        await db.update_task(task_id, status=TaskStatus.completed)

        await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "B", "max_turns": 5},
        )

        resp = await client.get("/tasks?status=completed")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 1
        assert tasks[0]["id"] == task_id

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_filter_by_preset(self, mock_submit, client):
        """GET /tasks?preset=X filters by policy preset."""
        await client.put("/policies/my-preset", json={"max_cost_usd": 1.0})
        await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "A",
                "policy_preset": "my-preset",
                "max_turns": 5,
            },
        )
        await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "B", "max_turns": 5},
        )

        resp = await client.get("/tasks?preset=my-preset")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 1
        assert tasks[0]["policy_preset"] == "my-preset"

    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_filter_by_repo(self, mock_submit, client):
        """GET /tasks?repo=X filters by repo."""
        await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "Local", "max_turns": 5},
        )

        resp = await client.get("/tasks?repo=.")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1
        assert all(t["repo"] == "." for t in resp.json())


# ---------------------------------------------------------------------------
# Cancel reason
# ---------------------------------------------------------------------------


class TestCancelReason:
    @patch("sandclaude.api.tasks.cancel_container", new_callable=AsyncMock)
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_cancel_with_reason(self, mock_submit, mock_cancel, client):
        """POST /tasks/{id}/cancel with reason records it."""
        mock_cancel.return_value = True
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "Cancel me", "max_turns": 5},
        )
        task_id = resp.json()["id"]

        resp = await client.post(
            f"/tasks/{task_id}/cancel",
            json={"reason": "No longer needed"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        # Verify reason stored
        task = await db.get_task(task_id)
        assert task.cancel_reason == "No longer needed"
        assert task.error_category == "cancelled"

    @patch("sandclaude.api.tasks.cancel_container", new_callable=AsyncMock)
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_cancel_without_reason(self, mock_submit, mock_cancel, client):
        """POST /tasks/{id}/cancel without body still works."""
        mock_cancel.return_value = True
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "Cancel me", "max_turns": 5},
        )
        task_id = resp.json()["id"]

        resp = await client.post(f"/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
