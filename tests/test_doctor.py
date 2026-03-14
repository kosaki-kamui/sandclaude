"""Tests for v0.3.0 deployment doctor endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import init_token
from sandclaude.db.store import init_db


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
async def client():
    from sandclaude.auth import get_token

    token = get_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


class TestDoctorEndpoint:
    async def test_doctor_returns_checks(self, client):
        """GET /admin/doctor returns structured check results."""
        resp = await client.get("/admin/doctor")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "checks" in data
        assert data["summary"]["passed"] >= 0
        assert data["summary"]["warned"] >= 0
        assert data["summary"]["failed"] >= 0
        # Should have at least the basic checks
        check_names = [c["name"] for c in data["checks"]]
        assert "api_key" in check_names
        assert "data_dir" in check_names
        assert "templates" in check_names
        assert "network_isolation" in check_names

    async def test_api_key_check_passes(self, client):
        """API key check should pass when key is set."""
        resp = await client.get("/admin/doctor")
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert checks["api_key"]["status"] == "pass"
        assert "test-key" not in checks["api_key"]["message"]  # masked

    async def test_data_dir_check_passes(self, client):
        """Data dir check should pass when dir exists and has .token."""
        resp = await client.get("/admin/doctor")
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert checks["data_dir"]["status"] == "pass"

    async def test_templates_check(self, client):
        """Templates check should find approve.html."""
        resp = await client.get("/admin/doctor")
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert checks["templates"]["status"] == "pass"

    async def test_github_oauth_warns_when_not_configured(self, client):
        """GitHub OAuth check should warn when not configured."""
        resp = await client.get("/admin/doctor")
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert checks["github_oauth"]["status"] == "warn"

    async def test_github_oauth_fails_with_partial_config(self, client):
        """GitHub OAuth check should fail with client_id but no secret."""
        import sandclaude.config as cfg

        cfg.settings.github_client_id = "test-id"
        cfg.settings.github_client_secret = ""
        try:
            resp = await client.get("/admin/doctor")
            checks = {c["name"]: c for c in resp.json()["checks"]}
            assert checks["github_oauth"]["status"] == "fail"
        finally:
            cfg.settings.github_client_id = ""

    async def test_network_isolation_warns_in_test(self, client):
        """Network isolation check should warn when skipped in test env."""
        import sandclaude.config as cfg

        cfg.settings.skip_network_isolation = True
        try:
            resp = await client.get("/admin/doctor")
            checks = {c["name"]: c for c in resp.json()["checks"]}
            assert checks["network_isolation"]["status"] == "warn"
        finally:
            cfg.settings.skip_network_isolation = False

    async def test_network_isolation_passes_when_enabled(self, client):
        """Network isolation check should pass when enabled."""
        resp = await client.get("/admin/doctor")
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert checks["network_isolation"]["status"] == "pass"

    async def test_non_admin_gets_403(self, client):
        """Non-admin scoped tokens should get 403."""
        from sandclaude.auth import generate_token, token_fingerprint
        from sandclaude.db import store as db

        raw = generate_token()
        await db.create_token(
            name="limited",
            token_hash=token_fingerprint(raw),
            scopes=["tasks:create"],
        )
        resp = await client.get(
            "/admin/doctor",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403
