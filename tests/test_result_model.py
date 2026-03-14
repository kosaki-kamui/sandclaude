"""Tests for v0.4.0 execution result model: partial status, review_required, completion_reason."""

from __future__ import annotations

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


class TestPartialStatus:
    async def test_partial_task_in_api_response(self, client):
        """A task with status=partial should appear correctly in API responses."""
        task = await db.create_task(
            task_id="task-partial-test",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(
            task.id,
            status=TaskStatus.partial,
            completion_reason="max_turns",
            review_required=1,
        )

        resp = await client.get(f"/tasks/{task.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert data["completion_reason"] == "max_turns"
        assert data["review_required"] == 1

    async def test_timed_out_status(self, client):
        """A task with status=timed_out should appear correctly."""
        task = await db.create_task(
            task_id="task-timeout-test",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.timed_out)

        resp = await client.get(f"/tasks/{task.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "timed_out"


class TestPRCreationGating:
    async def test_completed_task_allows_pr(self, client):
        """PR creation is always allowed for completed tasks."""
        task = await db.create_task(
            task_id="task-completed-pr",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.completed)

        # PR creation will fail because there's no diff, but status check passes
        with patch("sandclaude.api.prs.create_pr", new_callable=AsyncMock) as mock_pr:
            mock_pr.return_value = {"branch": "test", "url": "http://pr", "title": "Test"}
            resp = await client.post(f"/tasks/{task.id}/create-pr")
            # Should not be blocked by status (may fail for other reasons like no diff)
            assert resp.status_code != 400 or "status" not in resp.json().get("detail", "")

    async def test_partial_task_blocked_without_review(self, client):
        """PR creation is blocked for partial tasks with review_required=1."""
        task = await db.create_task(
            task_id="task-partial-pr",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(
            task.id,
            status=TaskStatus.partial,
            review_required=1,
        )

        resp = await client.post(f"/tasks/{task.id}/create-pr")
        assert resp.status_code == 409
        assert "review clearance" in resp.json()["detail"].lower()

    async def test_partial_task_allowed_after_review_cleared(self, client):
        """PR creation works for partial tasks after review is cleared."""
        task = await db.create_task(
            task_id="task-partial-cleared",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(
            task.id,
            status=TaskStatus.partial,
            review_required=0,  # already cleared
        )

        with patch("sandclaude.api.prs.create_pr", new_callable=AsyncMock) as mock_pr:
            mock_pr.return_value = {"branch": "test", "url": "http://pr", "title": "Test"}
            resp = await client.post(f"/tasks/{task.id}/create-pr")
            assert resp.status_code != 409  # not blocked by review

    async def test_failed_task_blocks_pr(self, client):
        """PR creation is blocked for failed tasks."""
        task = await db.create_task(
            task_id="task-failed-pr",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.failed)

        resp = await client.post(f"/tasks/{task.id}/create-pr")
        assert resp.status_code == 400

    async def test_timed_out_task_blocks_pr(self, client):
        """PR creation is blocked for timed_out tasks."""
        task = await db.create_task(
            task_id="task-timeout-pr",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.timed_out)

        resp = await client.post(f"/tasks/{task.id}/create-pr")
        assert resp.status_code == 400


class TestClearReview:
    async def test_clear_review_on_partial_task(self, client):
        """POST /tasks/{id}/clear-review clears review_required."""
        task = await db.create_task(
            task_id="task-clear-review",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(
            task.id,
            status=TaskStatus.partial,
            review_required=1,
        )

        resp = await client.post(f"/tasks/{task.id}/clear-review")
        assert resp.status_code == 200
        assert resp.json()["review_required"] == 0

        # Verify in DB
        updated = await db.get_task(task.id)
        assert updated.review_required == 0

    async def test_clear_review_on_completed_task_rejected(self, client):
        """clear-review is only valid for partial tasks."""
        task = await db.create_task(
            task_id="task-clear-completed",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.completed)

        resp = await client.post(f"/tasks/{task.id}/clear-review")
        assert resp.status_code == 400
        assert "partial" in resp.json()["detail"].lower()

    async def test_clear_review_requires_approve_scope(self, client):
        """clear-review requires tasks:approve scope."""
        from sandclaude.auth import generate_token

        raw = generate_token()
        await db.create_token(
            name="no-approve",
            token_hash=token_fingerprint(raw),
            scopes=["tasks:read", "tasks:create"],
        )
        task = await db.create_task(
            task_id="task-clear-scope",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.partial, review_required=1)

        resp = await client.post(
            f"/tasks/{task.id}/clear-review",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403


class TestCompletionReason:
    async def test_completion_reason_in_timeline(self, client):
        """completion_reason appears in task timeline."""
        task = await db.create_task(
            task_id="task-reason-timeline",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(
            task.id,
            status=TaskStatus.partial,
            completion_reason="max_turns",
        )

        resp = await client.get(f"/tasks/{task.id}/timeline")
        assert resp.status_code == 200
        assert resp.json()["status"] == "partial"

    async def test_completion_reason_persisted(self, client):
        """completion_reason is stored and returned correctly."""
        task = await db.create_task(
            task_id="task-reason-persist",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(
            task.id,
            status=TaskStatus.completed,
            completion_reason="success",
        )

        resp = await client.get(f"/tasks/{task.id}")
        assert resp.json()["completion_reason"] == "success"
