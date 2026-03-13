"""Tests for approval UI context and approve-and-create-pr endpoint."""

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


async def _create_completed_task_with_gate() -> str:
    task = await db.create_task(
        task_id="task-apr-test",
        repo="https://github.com/org/repo",
        branch="feature/fix",
        prompt="Fix the auth bug",
        owner_token_hash=token_fingerprint(get_token()),
    )
    await db.create_approval_gate(task.id, "create_pr")
    await db.update_task(task.id, status=TaskStatus.completed)
    return task.id


# ---------------------------------------------------------------------------
# Approval UI context: repo, branch, PR branches
# ---------------------------------------------------------------------------


class TestApprovalUIContext:
    async def test_approval_page_shows_repo_and_branch(self, client: AsyncClient):
        """Approval page render context must include repo/branch info."""
        task = await db.create_task(
            task_id="task-ui-ctx",
            repo="https://github.com/org/repo",
            branch="feature/fix",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.update_task(task.id, status=TaskStatus.completed)

        from sandclaude.auth import create_approval_link_token

        link_token = create_approval_link_token(task.id, "create_pr")
        resp = await client.get(f"/approve/{task.id}/create_pr?token={link_token}")
        assert resp.status_code == 200
        html = resp.text
        assert "https://github.com/org/repo" in html
        assert "feature/fix" in html
        assert f"sandclaude/{task.id}" in html

    async def test_approval_page_default_branch_when_none(self, client: AsyncClient):
        """When task.branch is None, show '(default branch)' for target."""
        task = await db.create_task(
            task_id="task-ui-nobranch",
            repo="https://github.com/org/repo",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.create_approval_gate(task.id, "create_pr")
        await db.update_task(task.id, status=TaskStatus.completed)

        from sandclaude.auth import create_approval_link_token

        link_token = create_approval_link_token(task.id, "create_pr")
        resp = await client.get(f"/approve/{task.id}/create_pr?token={link_token}")
        assert resp.status_code == 200
        assert "(default branch)" in resp.text


# ---------------------------------------------------------------------------
# POST /tasks/{id}/approve-and-create-pr
# ---------------------------------------------------------------------------


class TestApproveAndCreatePR:
    @patch("sandclaude.api.prs.create_pr", new_callable=AsyncMock)
    async def test_pending_gate_approve_and_create(self, mock_pr, client: AsyncClient):
        """Pending gate -> approve + create PR succeeds."""
        mock_pr.return_value = {
            "branch": "sandclaude/task-apr-test",
            "url": "https://github.com/org/repo/pull/1",
            "title": "fix: auth bug",
        }
        task_id = await _create_completed_task_with_gate()

        resp = await client.post(
            f"/tasks/{task_id}/approve-and-create-pr",
            json={"reason": "LGTM"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert data["pr"]["url"] == "https://github.com/org/repo/pull/1"
        mock_pr.assert_called_once()

        # Gate should now be approved
        gates = await db.get_approval_gates(task_id)
        assert gates[0].status == ApprovalStatus.approved
        assert gates[0].reason == "LGTM"

    @patch("sandclaude.api.prs.create_pr", new_callable=AsyncMock)
    async def test_already_approved_gate_creates_pr(self, mock_pr, client: AsyncClient):
        """Already approved gate -> skip approval, create PR."""
        mock_pr.return_value = {
            "branch": "sandclaude/task-apr-test",
            "url": "https://github.com/org/repo/pull/2",
            "title": "fix: auth bug",
        }
        task_id = await _create_completed_task_with_gate()

        # Pre-approve the gate
        await db.decide_approval_gate(
            task_id,
            "create_pr",
            decision=ApprovalStatus.approved,
            decided_by="fp_pre",
        )

        resp = await client.post(f"/tasks/{task_id}/approve-and-create-pr")
        assert resp.status_code == 200
        assert resp.json()["pr"]["url"] == "https://github.com/org/repo/pull/2"

    async def test_rejected_gate_returns_403(self, client: AsyncClient):
        """Rejected gate -> 403."""
        task_id = await _create_completed_task_with_gate()
        await db.decide_approval_gate(
            task_id,
            "create_pr",
            decision=ApprovalStatus.rejected,
            decided_by="fp_rej",
        )
        resp = await client.post(f"/tasks/{task_id}/approve-and-create-pr")
        assert resp.status_code == 403

    async def test_scoped_token_without_approve_gets_403(self, client: AsyncClient):
        """Token without tasks:approve -> 403."""
        task_id = await _create_completed_task_with_gate()

        create_resp = await client.post(
            "/tokens",
            json={
                "name": "no-approve",
                "scopes": ["tasks:create", "tasks:read"],
            },
        )
        limited_token = create_resp.json()["token"]

        resp = await client.post(
            f"/tasks/{task_id}/approve-and-create-pr",
            headers={"Authorization": f"Bearer {limited_token}"},
        )
        assert resp.status_code == 403

    @patch("sandclaude.api.prs.create_pr", new_callable=AsyncMock)
    async def test_no_gate_creates_pr_directly(self, mock_pr, client: AsyncClient):
        """No create_pr gate -> create PR directly."""
        mock_pr.return_value = {
            "branch": "sandclaude/task-nogate",
            "url": "https://github.com/org/repo/pull/3",
            "title": "fix: something",
        }
        task = await db.create_task(
            task_id="task-nogate",
            repo="https://github.com/org/repo",
            prompt="test",
            owner_token_hash=token_fingerprint(get_token()),
        )
        await db.update_task(task.id, status=TaskStatus.completed)

        resp = await client.post(f"/tasks/{task.id}/approve-and-create-pr")
        assert resp.status_code == 200
        assert resp.json()["pr"]["url"] == "https://github.com/org/repo/pull/3"
