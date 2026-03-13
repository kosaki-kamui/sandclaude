"""API-level integration tests for v0.3.0 user CRUD endpoints."""

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

    # Create bootstrap admin user (lifespan is bypassed in tests)
    await db.create_user(
        username="admin",
        display_name="Admin",
        email=None,
        github_username=None,
    )


@pytest.fixture
async def client():
    token = get_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


# ---------------------------------------------------------------------------
# POST /users — create user
# ---------------------------------------------------------------------------


class TestCreateUser:
    async def test_create_user(self, client: AsyncClient):
        resp = await client.post(
            "/users",
            json={
                "username": "alice",
                "display_name": "Alice Smith",
                "email": "alice@example.com",
                "github_username": "alice-gh",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "alice"
        assert data["display_name"] == "Alice Smith"
        assert data["email"] == "alice@example.com"
        assert data["github_username"] == "alice-gh"
        assert data["is_service_account"] == 0
        assert data["id"] > 0
        assert data["created_at"]

    async def test_create_user_non_admin_token_403(self, client: AsyncClient):
        """A scoped token without admin:users should get 403."""
        create_resp = await client.post(
            "/tokens",
            json={"name": "limited", "scopes": ["tasks:create", "tasks:read"]},
        )
        limited_token = create_resp.json()["token"]

        resp = await client.post(
            "/users",
            json={"username": "bob", "display_name": "Bob"},
            headers={"Authorization": f"Bearer {limited_token}"},
        )
        assert resp.status_code == 403
        assert "admin:users" in resp.json()["detail"]

    async def test_create_user_duplicate_username_409(self, client: AsyncClient):
        await client.post(
            "/users",
            json={"username": "dupuser", "display_name": "Dup One"},
        )
        resp = await client.post(
            "/users",
            json={"username": "dupuser", "display_name": "Dup Two"},
        )
        assert resp.status_code == 409
        assert "already taken" in resp.json()["detail"].lower()

    async def test_create_service_account(self, client: AsyncClient):
        resp = await client.post(
            "/users",
            json={
                "username": "ci-bot",
                "display_name": "CI Bot",
                "is_service_account": True,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["is_service_account"] == 1
        assert data["username"] == "ci-bot"


# ---------------------------------------------------------------------------
# GET /users — list users
# ---------------------------------------------------------------------------


class TestListUsers:
    async def test_list_users_includes_admin(self, client: AsyncClient):
        resp = await client.get("/users")
        assert resp.status_code == 200
        users = resp.json()
        usernames = [u["username"] for u in users]
        assert "admin" in usernames


# ---------------------------------------------------------------------------
# GET /users/{id} — get user
# ---------------------------------------------------------------------------


class TestGetUser:
    async def test_get_user_by_id(self, client: AsyncClient):
        create_resp = await client.post(
            "/users",
            json={"username": "charlie", "display_name": "Charlie"},
        )
        user_id = create_resp.json()["id"]

        resp = await client.get(f"/users/{user_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "charlie"
        assert data["id"] == user_id

    async def test_get_nonexistent_user_404(self, client: AsyncClient):
        resp = await client.get("/users/99999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DELETE /users/{id} — delete user
# ---------------------------------------------------------------------------


class TestDeleteUser:
    async def test_delete_user(self, client: AsyncClient):
        create_resp = await client.post(
            "/users",
            json={"username": "deleteme", "display_name": "Delete Me"},
        )
        user_id = create_resp.json()["id"]

        resp = await client.delete(f"/users/{user_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == user_id

        # Confirm deletion
        get_resp = await client.get(f"/users/{user_id}")
        assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Token binding: token → user_id
# ---------------------------------------------------------------------------


class TestTokenUserBinding:
    async def test_token_creation_with_user_id(self, client: AsyncClient):
        """Creating a token with user_id binds that token to the user."""
        create_user_resp = await client.post(
            "/users",
            json={"username": "tokenuser", "display_name": "Token User"},
        )
        user_id = create_user_resp.json()["id"]

        resp = await client.post(
            "/tokens",
            json={
                "name": "user-bound-token",
                "scopes": ["tasks:create", "tasks:read"],
                "user_id": user_id,
            },
        )
        assert resp.status_code == 201

        # Verify in token list
        list_resp = await client.get("/tokens")
        tokens = list_resp.json()
        bound = [t for t in tokens if t["name"] == "user-bound-token"]
        assert len(bound) == 1
        assert bound[0]["user_id"] == user_id


# ---------------------------------------------------------------------------
# Task creation records created_by_user_id
# ---------------------------------------------------------------------------


class TestTaskUserTracking:
    @patch("sandclaude.api.tasks.submit_task", new_callable=AsyncMock)
    async def test_task_records_created_by_user_id(self, mock_submit, client: AsyncClient):
        """Task creation via API records created_by_user_id from auth."""
        resp = await client.post(
            "/tasks",
            json={"repo": ".", "prompt": "test user tracking"},
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        task = await db.get_task(task_id)
        assert task is not None
        # Legacy admin token resolves to admin user
        admin = await db.get_user_by_username("admin")
        assert task.created_by_user_id == admin.id


# ---------------------------------------------------------------------------
# Approval decision records decided_by_user_id
# ---------------------------------------------------------------------------


class TestApprovalUserTracking:
    async def test_approval_records_decided_by_user_id(self, client: AsyncClient):
        """Approving a gate records decided_by_user_id from auth."""
        task = await db.create_task(
            task_id="task-approve-user-test",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.update_task(task.id, status=TaskStatus.completed)

        resp = await client.post(
            f"/tasks/{task.id}/approve/create_pr",
            json={"reason": "Looks good"},
        )
        assert resp.status_code == 200

        # Verify decided_by_user_id was recorded in the DB
        admin = await db.get_user_by_username("admin")
        import aiosqlite

        import sandclaude.db.store as store

        async with aiosqlite.connect(store.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT decided_by_user_id FROM approval_gates WHERE task_id = ? AND action = ?",
                (task.id, "create_pr"),
            )
            row = await cursor.fetchone()
        assert row is not None
        assert row["decided_by_user_id"] == admin.id
