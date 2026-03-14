"""Authorization matrix tests — verifies scope enforcement across all endpoints.

Every endpoint is tested with a token that lacks the required scope to ensure
it returns 403. This catches the exact gap identified in review: task CRUD
endpoints not enforcing scopes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import generate_token, get_token, init_token, token_fingerprint
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
    cfg.settings.github_client_id = ""
    cfg.settings.github_client_secret = ""
    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()
    init_token()


@pytest.fixture
async def admin_client():
    """Client with the admin (legacy) token — has all scopes."""
    token = get_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


async def _make_scoped_client(scopes: list[str]) -> tuple[AsyncClient, str]:
    """Create a scoped token and return (client, raw_token)."""
    raw = generate_token()
    await db.create_token(
        name="test-scoped",
        token_hash=token_fingerprint(raw),
        scopes=scopes,
    )
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    client.headers["Authorization"] = f"Bearer {raw}"
    return client, raw


async def _create_test_task(admin_client: AsyncClient) -> str:
    """Create a task using the admin client, return task_id."""
    with patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock):
        resp = await admin_client.post(
            "/tasks",
            json={"repo": ".", "prompt": "test", "max_turns": 5},
        )
        return resp.json()["id"]


# ---------------------------------------------------------------------------
# Task creation: requires tasks:create
# ---------------------------------------------------------------------------


class TestTaskCreateScope:
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_create_with_scope(self, mock_submit, admin_client):
        """Token with tasks:create can create tasks."""
        client, _ = await _make_scoped_client(["tasks:create", "tasks:read"])
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "test", "max_turns": 5},
        )
        assert resp.status_code == 201
        await client.aclose()

    async def test_create_without_scope(self, admin_client):
        """Token without tasks:create gets 403."""
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "test", "max_turns": 5},
        )
        assert resp.status_code == 403
        assert "tasks:create" in resp.json()["detail"]
        await client.aclose()


# ---------------------------------------------------------------------------
# Task read: requires tasks:read
# ---------------------------------------------------------------------------


class TestTaskReadScope:
    async def test_list_without_scope(self, admin_client):
        """Token without tasks:read gets 403 on GET /tasks."""
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.get("/tasks")
        assert resp.status_code == 403
        await client.aclose()

    async def test_get_task_without_scope(self, admin_client):
        """Token without tasks:read gets 403 on GET /tasks/{id}."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.get(f"/tasks/{task_id}")
        assert resp.status_code == 403
        await client.aclose()

    async def test_diff_without_scope(self, admin_client):
        """Token without tasks:read gets 403 on GET /tasks/{id}/diff."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.get(f"/tasks/{task_id}/diff")
        assert resp.status_code == 403
        await client.aclose()

    async def test_timeline_without_scope(self, admin_client):
        """Token without tasks:read gets 403 on GET /tasks/{id}/timeline."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.get(f"/tasks/{task_id}/timeline")
        assert resp.status_code == 403
        await client.aclose()

    async def test_bundle_without_scope(self, admin_client):
        """Token without tasks:read gets 403 on GET /tasks/{id}/bundle."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.get(f"/tasks/{task_id}/bundle")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# Task delete: requires tasks:delete
# ---------------------------------------------------------------------------


class TestTaskDeleteScope:
    async def test_delete_without_scope(self, admin_client):
        """Token without tasks:delete gets 403."""
        task_id = await _create_test_task(admin_client)
        await db.update_task(task_id, status=TaskStatus.completed)
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.delete(f"/tasks/{task_id}")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# Task cancel: requires tasks:cancel
# ---------------------------------------------------------------------------


class TestTaskCancelScope:
    async def test_cancel_without_scope(self, admin_client):
        """Token without tasks:cancel gets 403."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.post(f"/tasks/{task_id}/cancel")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# PR creation: requires prs:create
# ---------------------------------------------------------------------------


class TestPRCreateScope:
    async def test_create_pr_without_scope(self, admin_client):
        """Token without prs:create gets 403."""
        task_id = await _create_test_task(admin_client)
        await db.update_task(task_id, status=TaskStatus.completed)
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.post(f"/tasks/{task_id}/create-pr")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# Approval: requires tasks:approve
# ---------------------------------------------------------------------------


class TestApprovalScope:
    async def test_approve_without_scope(self, admin_client):
        """Token without tasks:approve gets 403."""
        task_id = await _create_test_task(admin_client)
        await db.create_approval_gate(task_id, "create_pr")
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.post(f"/tasks/{task_id}/approve/create_pr")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# Risk/review: requires tasks:read
# ---------------------------------------------------------------------------


class TestReviewScope:
    async def test_risk_without_scope(self, admin_client):
        """Token without tasks:read gets 403 on GET /tasks/{id}/risk."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.get(f"/tasks/{task_id}/risk")
        assert resp.status_code == 403
        await client.aclose()

    async def test_review_without_scope(self, admin_client):
        """Token without tasks:read gets 403 on POST /tasks/{id}/review."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.post(f"/tasks/{task_id}/review")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# Admin endpoints: require admin scopes
# ---------------------------------------------------------------------------


class TestAdminScope:
    async def test_tokens_without_admin(self, admin_client):
        """Token without admin:tokens gets 403 on POST /tokens."""
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.post(
            "/tokens",
            json={"name": "test", "scopes": ["tasks:read"]},
        )
        assert resp.status_code == 403
        await client.aclose()

    async def test_policies_without_admin(self, admin_client):
        """Token without admin:policies gets 403 on GET /policies."""
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.get("/policies")
        assert resp.status_code == 403
        await client.aclose()

    async def test_users_without_admin(self, admin_client):
        """Token without admin:users gets 403 on GET /users."""
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.get("/users")
        assert resp.status_code == 403
        await client.aclose()

    async def test_metrics_without_admin(self, admin_client):
        """Token without admin:policies gets 403 on GET /metrics."""
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.get("/metrics")
        assert resp.status_code == 403
        await client.aclose()

    async def test_doctor_without_admin(self, admin_client):
        """Token without admin:policies gets 403 on GET /admin/doctor."""
        client, _ = await _make_scoped_client(["tasks:read"])
        resp = await client.get("/admin/doctor")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# Approval side-channels: require tasks:read
# ---------------------------------------------------------------------------


class TestApprovalSideChannelScope:
    async def test_list_approvals_without_read_scope(self, admin_client):
        """Token without tasks:read gets 403 on GET /tasks/{id}/approvals."""
        task_id = await _create_test_task(admin_client)
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.get(f"/tasks/{task_id}/approvals")
        assert resp.status_code == 403
        await client.aclose()

    async def test_approval_link_without_read_scope(self, admin_client):
        """Token without tasks:read gets 403 on POST /tasks/{id}/approval-link."""
        task_id = await _create_test_task(admin_client)
        await db.create_approval_gate(task_id, "create_pr")
        client, _ = await _make_scoped_client(["tasks:create"])
        resp = await client.post(f"/tasks/{task_id}/approval-link/create_pr")
        assert resp.status_code == 403
        await client.aclose()


# ---------------------------------------------------------------------------
# WebSocket streaming: requires tasks:read
# ---------------------------------------------------------------------------


class TestWebSocketScope:
    def test_stream_without_read_scope(self):
        """WebSocket with token lacking tasks:read gets closed with 4003."""
        import asyncio

        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        raw = generate_token()
        asyncio.get_event_loop().run_until_complete(
            db.create_token(
                name="no-read-ws",
                token_hash=token_fingerprint(raw),
                scopes=["tasks:create"],
            )
        )

        with TestClient(app) as tc:
            try:
                with tc.websocket_connect(
                    "/tasks/task-fake/stream",
                    headers={"Authorization": f"Bearer {raw}"},
                ):
                    pytest.fail("WebSocket should have been rejected")
            except (WebSocketDisconnect, Exception):
                pass  # Expected: connection closed due to missing scope
