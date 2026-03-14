"""Tests for v0.4.0 safer approval defaults (Epic B)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import init_token
from sandclaude.db import store as db
from sandclaude.db.store import init_db
from sandclaude.models import ApprovalStatus


@pytest.fixture(autouse=True)
async def _setup(tmp_path):
    import sandclaude.config as cfg
    import sandclaude.db.store as store

    cfg.settings.data_dir = tmp_path
    cfg.settings.anthropic_api_key = "test-key"
    cfg.settings.environment = "test"
    cfg.settings.approval_expiry_s = 86400  # 24 hours
    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()
    init_token()


@pytest.fixture
async def client():
    from sandclaude.auth import get_token

    token = get_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


# ── Approval expiry ──────────────────────────────────────────────────


class TestApprovalExpiry:
    async def test_gate_has_expires_at(self):
        """New gates should have expires_at set."""
        gate = await db.create_approval_gate("task-exp-1", "create_pr")
        assert gate.expires_at is not None
        # Should be ~24h from now
        exp = datetime.fromisoformat(gate.expires_at)
        now = datetime.now(timezone.utc)
        delta = (exp - now).total_seconds()
        assert 86300 < delta < 86500  # within 100s tolerance

    async def test_gate_no_expiry_when_disabled(self):
        """Gates should not expire when approval_expiry_s=0."""
        import sandclaude.config as cfg

        cfg.settings.approval_expiry_s = 0
        try:
            gate = await db.create_approval_gate("task-exp-2", "create_pr")
            assert gate.expires_at is None
        finally:
            cfg.settings.approval_expiry_s = 86400

    async def test_expired_gate_auto_rejected(self):
        """Expired pending gates should be auto-rejected on read."""
        # Create a gate, then backdate expires_at to the past
        gate = await db.create_approval_gate("task-exp-3", "create_pr")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        import aiosqlite

        from sandclaude.db.store import _db_path

        async with aiosqlite.connect(_db_path()) as conn:
            await conn.execute(
                "UPDATE approval_gates SET expires_at = ? WHERE id = ?",
                (past, gate.id),
            )
            await conn.commit()

        # Reading gates should auto-expire it
        gates = await db.get_approval_gates("task-exp-3")
        assert len(gates) == 1
        assert gates[0].status == ApprovalStatus.rejected
        assert gates[0].reason == "expired"

    async def test_non_expired_gate_stays_pending(self):
        """Gates with future expires_at should remain pending."""
        await db.create_task(
            task_id="task-exp-4", repo="https://github.com/test/repo", prompt="test"
        )
        await db.create_approval_gate("task-exp-4", "create_pr")
        gates = await db.get_approval_gates("task-exp-4")
        assert len(gates) == 1
        assert gates[0].status == ApprovalStatus.pending

    async def test_has_pending_excludes_expired(self):
        """has_pending_gates should exclude expired gates."""
        gate = await db.create_approval_gate("task-exp-5", "create_pr")
        assert await db.has_pending_gates("task-exp-5") is True

        # Backdate to past
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        import aiosqlite

        from sandclaude.db.store import _db_path

        async with aiosqlite.connect(_db_path()) as conn:
            await conn.execute(
                "UPDATE approval_gates SET expires_at = ? WHERE id = ?",
                (past, gate.id),
            )
            await conn.commit()

        assert await db.has_pending_gates("task-exp-5") is False


# ── Approval gate in API response ────────────────────────────────────


class TestApprovalGateAPI:
    async def test_approvals_include_expires_at(self, client):
        """GET /tasks/{id}/approvals should include expires_at."""
        await db.create_task(
            task_id="task-api-exp-1",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        await db.create_approval_gate("task-api-exp-1", "create_pr")

        resp = await client.get("/tasks/task-api-exp-1/approvals")
        assert resp.status_code == 200
        gates = resp.json()
        assert len(gates) == 1
        assert "expires_at" in gates[0]
        assert gates[0]["expires_at"] is not None


# ── Config ───────────────────────────────────────────────────────────


class TestApprovalConfig:
    def test_default_expiry(self):
        import sandclaude.config as cfg

        assert cfg.settings.approval_expiry_s == 86400


# ── Webhook for approval events ──────────────────────────────────────


class TestApprovalWebhook:
    async def test_approval_webhook_called_on_approve(self, client):
        """Approving a gate should attempt to send a webhook."""
        from unittest.mock import AsyncMock, patch

        await db.create_task(
            task_id="task-wh-1",
            repo="https://github.com/test/repo",
            prompt="test",
            notify_webhook="https://hooks.slack.com/test",
            notify_on=["approval"],
        )
        await db.create_approval_gate("task-wh-1", "create_pr")

        with patch(
            "sandclaude.api.approvals.send_approval_webhook", new_callable=AsyncMock
        ) as mock_wh:
            resp = await client.post("/tasks/task-wh-1/approve/create_pr")
            assert resp.status_code == 200
            mock_wh.assert_called_once()
            args = mock_wh.call_args
            assert args[0][1] == "create_pr"
            assert args[0][2] == "approval_approved"

    async def test_rejection_webhook_called(self, client):
        """Rejecting a gate should send rejection webhook."""
        from unittest.mock import AsyncMock, patch

        await db.create_task(
            task_id="task-wh-2",
            repo="https://github.com/test/repo",
            prompt="test",
            notify_webhook="https://hooks.slack.com/test",
            notify_on=["approval"],
        )
        await db.create_approval_gate("task-wh-2", "create_pr")

        with patch(
            "sandclaude.api.approvals.send_approval_webhook", new_callable=AsyncMock
        ) as mock_wh:
            resp = await client.post("/tasks/task-wh-2/reject/create_pr")
            assert resp.status_code == 200
            mock_wh.assert_called_once()
            args = mock_wh.call_args
            assert args[0][2] == "approval_rejected"
