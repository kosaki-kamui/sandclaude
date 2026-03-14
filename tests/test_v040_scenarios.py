"""v0.4.0 Epic G — cross-cutting scenario tests.

Tests that verify interactions between multiple v0.4.0 epics working together.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import init_token
from sandclaude.config import SandboxMode
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
    cfg.settings.approval_expiry_s = 86400
    cfg.settings.sandbox_mode = SandboxMode.standard
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


# ── Token rotation (Epic F) ─────────────────────────────────────────


class TestTokenRotation:
    async def test_rotate_active_token(self, client):
        """Rotating an active token should revoke old and create new."""
        # Create a token
        resp = await client.post(
            "/tokens",
            json={"name": "rotate-test", "scopes": ["tasks:create", "tasks:read"]},
        )
        assert resp.status_code == 201
        old_id = resp.json()["id"]
        old_raw = resp.json()["token"]

        # Rotate it
        resp = await client.post(f"/tokens/{old_id}/rotate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["rotated_from"] == old_id
        assert "new_token" in data
        new_raw = data["new_token"]["token"]
        assert new_raw != old_raw
        assert data["new_token"]["scopes"] == ["tasks:create", "tasks:read"]
        assert "(rotated)" in data["new_token"]["name"]

        # Old token should be revoked
        tokens = await client.get("/tokens")
        token_map = {t["id"]: t for t in tokens.json()}
        assert token_map[old_id]["is_active"] is False

    async def test_rotate_revoked_token_fails(self, client):
        """Cannot rotate a revoked token."""
        resp = await client.post(
            "/tokens",
            json={"name": "revoke-me", "scopes": ["tasks:create"]},
        )
        token_id = resp.json()["id"]
        await client.post(f"/tokens/{token_id}/revoke")

        resp = await client.post(f"/tokens/{token_id}/rotate")
        assert resp.status_code == 409

    async def test_rotate_nonexistent_token(self, client):
        """Rotating a nonexistent token returns 404."""
        resp = await client.post("/tokens/99999/rotate")
        assert resp.status_code == 404

    async def test_rotate_preserves_expiry(self, client):
        """Rotated token should preserve remaining expiry time."""
        resp = await client.post(
            "/tokens",
            json={
                "name": "expiring",
                "scopes": ["tasks:read"],
                "expires_in_days": 30,
            },
        )
        old_id = resp.json()["id"]
        old_expires = resp.json()["expires_at"]

        resp = await client.post(f"/tokens/{old_id}/rotate")
        assert resp.status_code == 200
        new_expires = resp.json()["new_token"]["expires_at"]
        assert new_expires is not None
        # New expiry should be close to old expiry (within a few seconds)
        old_dt = datetime.fromisoformat(old_expires)
        new_dt = datetime.fromisoformat(new_expires)
        delta = abs((old_dt - new_dt).total_seconds())
        assert delta < 60  # within 1 minute tolerance


# ── Legacy token deprecation header (Epic F) ─────────────────────────


class TestLegacyDeprecationHeader:
    async def test_legacy_token_gets_deprecation_header(self, client):
        """Requests with the legacy .token should get deprecation header."""
        # Use an authenticated endpoint (health doesn't require auth)
        resp = await client.get("/pool")
        assert resp.status_code == 200
        dep = resp.headers.get("x-sandclaude-deprecation")
        assert dep is not None
        assert "POST /tokens" in dep

    async def test_registry_token_no_deprecation_header(self, client):
        """Requests with scoped registry tokens should NOT get the header."""
        # Create a scoped token
        resp = await client.post(
            "/tokens",
            json={"name": "scoped", "scopes": ["tasks:read", "admin:tokens"]},
        )
        scoped_raw = resp.json()["token"]

        resp = await client.get("/tokens", headers={"Authorization": f"Bearer {scoped_raw}"})
        assert resp.status_code == 200
        dep = resp.headers.get("x-sandclaude-deprecation")
        assert dep is None

    async def test_unauthenticated_no_deprecation_header(self):
        """Unauthenticated requests should not get the header."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/health")
            assert resp.status_code == 200
            assert "x-sandclaude-deprecation" not in resp.headers


# ── Partial task review flow (Epic A + B cross-cutting) ──────────────


class TestPartialTaskReviewFlow:
    async def test_clear_review_on_partial_task(self, client):
        """POST /tasks/{id}/clear-review should clear review flag."""
        await db.create_task(
            task_id="partial-review-1",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        await db.update_task(
            "partial-review-1",
            status=TaskStatus.partial,
            review_required=1,
            completion_reason="max_turns",
        )

        resp = await client.post("/tasks/partial-review-1/clear-review")
        assert resp.status_code == 200
        assert resp.json()["review_required"] == 0

        # Verify in DB
        t = await db.get_task("partial-review-1")
        assert t is not None
        assert t.review_required == 0

    async def test_clear_review_on_non_partial_fails(self, client):
        """clear-review should reject non-partial tasks."""
        await db.create_task(
            task_id="completed-1",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        await db.update_task("completed-1", status=TaskStatus.completed)

        resp = await client.post("/tasks/completed-1/clear-review")
        assert resp.status_code == 400


# ── Approval expiry + requires_approval (Epic B cross-cutting) ───────


class TestApprovalExpiryCascade:
    async def test_expired_gate_clears_has_pending(self):
        """When all gates expire, has_pending_gates should return False."""
        import aiosqlite

        from sandclaude.db.store import _db_path

        await db.create_task(
            task_id="expiry-cascade-1",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        gate = await db.create_approval_gate("expiry-cascade-1", "create_pr")

        # Verify pending
        assert await db.has_pending_gates("expiry-cascade-1") is True

        # Backdate to expired
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        async with aiosqlite.connect(_db_path()) as conn:
            await conn.execute(
                "UPDATE approval_gates SET expires_at = ? WHERE id = ?",
                (past, gate.id),
            )
            await conn.commit()

        # Now should report no pending gates
        assert await db.has_pending_gates("expiry-cascade-1") is False


# ── Audit schema in bundle (Epic E cross-cutting) ────────────────────


class TestAuditInBundle:
    async def test_bundle_version_040(self, client):
        """Bundle export should use version 0.4.0."""
        await db.create_task(
            task_id="bundle-v040",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        await db.update_task("bundle-v040", status=TaskStatus.completed)

        resp = await client.get("/tasks/bundle-v040/bundle")
        assert resp.status_code == 200
        assert resp.json()["version"] == "0.4.0"


# ── Health endpoint version (Epic F) ─────────────────────────────────


class TestVersionBump:
    async def test_health_reports_040(self, client):
        """Health endpoint should report version 0.4.0."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["version"] == "0.4.0"


# ── Doctor sandbox check (Epic C cross-cutting) ─────────────────────


class TestDoctorSandboxIntegration:
    async def test_doctor_includes_sandbox_check(self, client):
        """Doctor should include both network_isolation and sandbox_mode checks."""
        resp = await client.get("/admin/doctor")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["checks"]]
        assert "network_isolation" in names
        assert "sandbox_mode" in names
        # In test env with standard mode, both should pass
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert checks["sandbox_mode"]["status"] == "pass"


# ── Scheduler protocol (Epic D) ─────────────────────────────────────


class TestSchedulerProtocol:
    def test_local_scheduler_is_default(self):
        from sandclaude.runner.pool import LocalScheduler, get_scheduler

        scheduler = get_scheduler()
        assert isinstance(scheduler, LocalScheduler)

    async def test_scheduler_stats_structure(self):
        from sandclaude.runner.pool import get_scheduler

        stats = await get_scheduler().stats()
        assert set(stats.keys()) == {"max_concurrent", "active", "queued"}


# ── Security fix: approve-and-create-pr requires prs:create ──────────


class TestApproveAndCreatePrScope:
    async def test_approve_and_create_pr_requires_prs_create(self, client):
        """Token with tasks:approve but NOT prs:create should be rejected."""
        from sandclaude.auth import generate_token, token_fingerprint

        # Create a token with tasks:approve but no prs:create
        raw = generate_token()
        await db.create_token(
            name="approve-only",
            token_hash=token_fingerprint(raw),
            scopes=["tasks:approve", "tasks:read", "tasks:create"],
        )

        await db.create_task(
            task_id="scope-test-1",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        await db.update_task("scope-test-1", status=TaskStatus.completed)

        resp = await client.post(
            "/tasks/scope-test-1/approve-and-create-pr",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403
        assert "prs:create" in resp.json()["detail"]

    async def test_approve_and_create_pr_works_with_both_scopes(self, client):
        """Token with both tasks:approve + prs:create should pass scope check."""
        from unittest.mock import AsyncMock, patch

        from sandclaude.auth import generate_token, token_fingerprint

        raw = generate_token()
        await db.create_token(
            name="full-pr",
            token_hash=token_fingerprint(raw),
            scopes=["tasks:approve", "prs:create", "tasks:read"],
        )

        await db.create_task(
            task_id="scope-test-2",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        await db.update_task("scope-test-2", status=TaskStatus.completed)

        # Mock create_pr to avoid needing real GitHub
        with patch("sandclaude.api.prs.create_pr", new_callable=AsyncMock) as mock_pr:
            mock_pr.return_value = {"pr_url": "https://github.com/test/repo/pull/1"}
            resp = await client.post(
                "/tasks/scope-test-2/approve-and-create-pr",
                headers={"Authorization": f"Bearer {raw}"},
            )
            # Should pass scope check (may fail on PR creation details, but not 403)
            assert resp.status_code != 403


class TestSessionAuthApproveAndCreatePr:
    async def test_session_user_can_approve_and_create_pr(self, client):
        """GitHub OAuth session users should have prs:create for the approval UI flow."""
        from unittest.mock import AsyncMock, patch

        from sandclaude.auth import create_session_cookie

        # Create a task in completed state
        await db.create_task(
            task_id="session-pr-1",
            repo="https://github.com/test/repo",
            prompt="test",
        )
        await db.update_task("session-pr-1", status=TaskStatus.completed)

        # Create a session cookie (simulates GitHub OAuth login)
        cookie = create_session_cookie(user_id=1, username="testuser")

        with patch("sandclaude.api.prs.create_pr", new_callable=AsyncMock) as mock_pr:
            mock_pr.return_value = {"pr_url": "https://github.com/test/repo/pull/1"}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/tasks/session-pr-1/approve-and-create-pr",
                    cookies={"sandclaude_session": cookie},
                )
                # Should NOT get 403 — session has prs:create
                assert resp.status_code != 403
