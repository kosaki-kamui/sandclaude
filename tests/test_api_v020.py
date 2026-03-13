"""API-level integration tests for v0.2.0 endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import get_token, init_token, token_fingerprint
from sandclaude.db import store as db
from sandclaude.db.store import init_db
from sandclaude.models import ApprovalStatus, TaskStatus


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
# Token management endpoints
# ---------------------------------------------------------------------------


class TestTokenEndpoints:
    async def test_create_token(self, client: AsyncClient):
        resp = await client.post(
            "/tokens",
            json={
                "name": "ci-bot",
                "scopes": ["tasks:create", "tasks:read"],
                "expires_in_days": 30,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "ci-bot"
        assert "token" in data  # raw token shown once
        assert data["scopes"] == ["tasks:create", "tasks:read"]
        assert data["expires_at"] is not None

    async def test_list_tokens(self, client: AsyncClient):
        await client.post(
            "/tokens",
            json={"name": "list-test", "scopes": ["tasks:read"]},
        )
        resp = await client.get("/tokens")
        assert resp.status_code == 200
        tokens = resp.json()
        names = [t["name"] for t in tokens]
        assert "list-test" in names
        # Verify token_hash is never exposed
        for t in tokens:
            assert "token_hash" not in t

    async def test_revoke_token(self, client: AsyncClient):
        create_resp = await client.post(
            "/tokens",
            json={"name": "revoke-me", "scopes": ["tasks:read"]},
        )
        token_id = create_resp.json()["id"]
        resp = await client.post(f"/tokens/{token_id}/revoke")
        assert resp.status_code == 200
        assert resp.json()["revoked"] == token_id

    async def test_scoped_token_cannot_create_tokens(self, client: AsyncClient):
        """A token without admin:tokens scope should get 403."""
        create_resp = await client.post(
            "/tokens",
            json={"name": "limited", "scopes": ["tasks:create"]},
        )
        raw_token = create_resp.json()["token"]
        # Use the limited token to try creating another token
        resp = await client.post(
            "/tokens",
            json={"name": "should-fail", "scopes": ["tasks:read"]},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Policy preset endpoints
# ---------------------------------------------------------------------------


class TestPolicyEndpoints:
    async def test_create_and_get_policy(self, client: AsyncClient):
        resp = await client.put(
            "/policies/test-preset",
            json={"allowed_commands": ["npm"], "max_cost_usd": 1.0},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-preset"

        get_resp = await client.get("/policies/test-preset")
        assert get_resp.status_code == 200
        assert get_resp.json()["config"]["max_cost_usd"] == 1.0

    async def test_list_policies(self, client: AsyncClient):
        await client.put("/policies/p1", json={"max_cost_usd": 1.0})
        await client.put("/policies/p2", json={"max_cost_usd": 2.0})
        resp = await client.get("/policies")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert "p1" in names
        assert "p2" in names

    async def test_delete_policy(self, client: AsyncClient):
        await client.put("/policies/delete-me", json={})
        resp = await client.delete("/policies/delete-me")
        assert resp.status_code == 200
        get_resp = await client.get("/policies/delete-me")
        assert get_resp.status_code == 404

    async def test_invalid_preset_name_rejected(self, client: AsyncClient):
        resp = await client.put(
            "/policies/INVALID NAME!",
            json={"max_cost_usd": 1.0},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Approval gate endpoints
# ---------------------------------------------------------------------------


class TestApprovalEndpoints:
    async def _create_task_with_gate(self, client: AsyncClient) -> str:
        """Helper: create a task and add an approval gate."""
        task = await db.create_task(
            task_id="task-approve-test",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.update_task(task.id, status=TaskStatus.completed)
        return task.id

    async def test_list_approvals(self, client: AsyncClient):
        task_id = await self._create_task_with_gate(client)
        resp = await client.get(f"/tasks/{task_id}/approvals")
        assert resp.status_code == 200
        gates = resp.json()
        assert len(gates) == 1
        assert gates[0]["action"] == "create_pr"
        assert gates[0]["status"] == "pending"

    async def test_approve_gate(self, client: AsyncClient):
        task_id = await self._create_task_with_gate(client)
        resp = await client.post(
            f"/tasks/{task_id}/approve/create_pr",
            json={"reason": "LGTM"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    async def test_reject_gate(self, client: AsyncClient):
        task_id = await self._create_task_with_gate(client)
        resp = await client.post(
            f"/tasks/{task_id}/reject/create_pr",
            json={"reason": "Too risky"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    async def test_create_pr_blocked_by_pending_gate(self, client: AsyncClient):
        task_id = await self._create_task_with_gate(client)
        resp = await client.post(f"/tasks/{task_id}/create-pr")
        assert resp.status_code == 409
        assert "requires approval" in resp.json()["detail"].lower()

    async def test_create_pr_blocked_by_rejected_gate(self, client: AsyncClient):
        task_id = await self._create_task_with_gate(client)
        await db.decide_approval_gate(
            task_id,
            "create_pr",
            decision=ApprovalStatus.rejected,
            decided_by="fp_test",
        )
        resp = await client.post(f"/tasks/{task_id}/create-pr")
        assert resp.status_code == 403

    async def test_scoped_token_without_approve_gets_403(self, client: AsyncClient):
        """A token with tasks:create but not tasks:approve cannot approve."""
        task_id = await self._create_task_with_gate(client)

        # Create a scoped token without approve
        create_resp = await client.post(
            "/tokens",
            json={"name": "no-approve", "scopes": ["tasks:create", "tasks:read"]},
        )
        limited_token = create_resp.json()["token"]

        resp = await client.post(
            f"/tasks/{task_id}/approve/create_pr",
            headers={"Authorization": f"Bearer {limited_token}"},
        )
        assert resp.status_code == 403
        assert "tasks:approve" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Approval link generation
# ---------------------------------------------------------------------------


class TestApprovalLinks:
    async def test_generate_approval_link(self, client: AsyncClient):
        task = await db.create_task(
            task_id="task-link-test",
            repo=".",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.create_approval_gate(task.id, "create_pr")
        resp = await client.post(f"/tasks/{task.id}/approval-link/create_pr")
        assert resp.status_code == 200
        data = resp.json()
        assert "approval_url" in data
        assert "task-link-test" in data["approval_url"]
        assert data["expires_in_seconds"] == 3600


# ---------------------------------------------------------------------------
# Retry endpoint
# ---------------------------------------------------------------------------


class TestRetryEndpoint:
    @patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
    async def test_retry_creates_follow_up(self, mock_submit, client: AsyncClient):
        task = await db.create_task(
            task_id="task-retry-api",
            repo="https://github.com/org/repo",
            prompt="Fix the bug",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.completed)

        resp = await client.post(
            f"/tasks/{task.id}/retry",
            json={"prompt": "Also fix the tests", "max_turns": 15},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["repo"] == "https://github.com/org/repo"
        assert "Also fix the tests" in data["prompt"]
        mock_submit.assert_called_once()


# ---------------------------------------------------------------------------
# Task bundle export
# ---------------------------------------------------------------------------


class TestBundleEndpoint:
    async def test_bundle_export(self, client: AsyncClient):
        task = await db.create_task(
            task_id="task-bundle-api",
            repo=".",
            prompt="bundle export test",
            policy_preset="bugfix-pr",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.completed)

        resp = await client.get(f"/tasks/{task.id}/bundle")
        assert resp.status_code == 200
        bundle = resp.json()
        assert bundle["version"] == "0.2.5"
        assert bundle["task"]["id"] == "task-bundle-api"
        assert "approval_gates" in bundle
        assert "secrets_audit" in bundle


# ---------------------------------------------------------------------------
# Repo/branch policy enforcement at task creation
# ---------------------------------------------------------------------------


class TestRepoBranchPolicyAtCreation:
    @patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
    async def test_blocked_repo_rejected(self, mock_submit, client: AsyncClient):
        # Create a preset that restricts repos
        await client.put(
            "/policies/repo-restricted",
            json={
                "allowed_repos": ["https://github.com/allowed-org/"],
                "max_cost_usd": 1.0,
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": "https://github.com/other-org/repo",
                "prompt": "test",
                "policy_preset": "repo-restricted",
            },
        )
        assert resp.status_code == 403
        assert "not in the allowed repos" in resp.json()["detail"]
        mock_submit.assert_not_called()

    @patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
    async def test_blocked_branch_rejected(self, mock_submit, client: AsyncClient):
        await client.put(
            "/policies/branch-restricted",
            json={
                "blocked_branches": ["main", "production"],
                "max_cost_usd": 1.0,
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": ".",
                "prompt": "test",
                "branch": "main",
                "policy_preset": "branch-restricted",
            },
        )
        assert resp.status_code == 403
        assert "blocked" in resp.json()["detail"]
        mock_submit.assert_not_called()

    @patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
    async def test_allowed_repo_and_branch_accepted(self, mock_submit, client: AsyncClient):
        await client.put(
            "/policies/permissive",
            json={
                "allowed_repos": ["https://github.com/my-org/"],
                "blocked_branches": ["production"],
                "max_cost_usd": 5.0,
            },
        )
        resp = await client.post(
            "/tasks",
            json={
                "repo": "https://github.com/my-org/repo",
                "prompt": "test",
                "branch": "feature/fix",
                "policy_preset": "permissive",
            },
        )
        assert resp.status_code == 201
        mock_submit.assert_called_once()
