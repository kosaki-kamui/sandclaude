"""Tests for v0.4.0 sandbox privilege reduction (Epic C)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import init_token
from sandclaude.config import SandboxMode
from sandclaude.db.store import init_db

# ── Unit tests: SandboxMode config ──────────────────────────────────


class TestSandboxModeConfig:
    def test_default_is_standard(self):
        import sandclaude.config as cfg

        assert cfg.settings.sandbox_mode == SandboxMode.standard

    def test_enum_values(self):
        assert SandboxMode.standard.value == "standard"
        assert SandboxMode.strict.value == "strict"

    def test_enum_from_string(self):
        assert SandboxMode("standard") == SandboxMode.standard
        assert SandboxMode("strict") == SandboxMode.strict

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            SandboxMode("nonexistent")


# ── Unit tests: container creation kwargs ────────────────────────────


class TestContainerSandboxKwargs:
    """Verify container creation uses correct Docker kwargs per sandbox mode."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        import sandclaude.config as cfg
        import sandclaude.db.store as store

        cfg.settings.data_dir = tmp_path
        cfg.settings.environment = "test"
        cfg.settings.skip_network_isolation = True
        cfg.settings.anthropic_api_key = "test-key"
        store.DB_PATH = tmp_path / "tasks.db"

    async def _run_with_mock(self, sandbox_mode):
        """Helper: run a task with mocked Docker, return the containers.run call kwargs."""
        import sandclaude.config as cfg

        cfg.settings.sandbox_mode = sandbox_mode

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = f"test-{sandbox_mode.value}"
        mock_container.status = "exited"
        mock_container.attrs = {"State": {"ExitCode": 0}}
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_client.containers.run.return_value = mock_container

        await init_db()
        from sandclaude.db import store as db

        task = await db.create_task(
            task_id=f"t-{sandbox_mode.value}-{id(self)}",
            repo="https://github.com/test/repo",
            prompt="test",
        )

        with patch("sandclaude.runner.container._get_client", return_value=mock_client):
            from sandclaude.runner.container import run_task_in_container

            try:
                await run_task_in_container(task)
            except Exception:
                pass

        assert mock_client.containers.run.call_args is not None
        _, kwargs = mock_client.containers.run.call_args
        return kwargs

    async def test_standard_mode_no_readonly(self):
        """Standard mode should NOT set read_only or security_opt."""
        kwargs = await self._run_with_mock(SandboxMode.standard)
        assert "read_only" not in kwargs
        assert "security_opt" not in kwargs
        assert "tmpfs" not in kwargs

    async def test_strict_mode_sets_readonly_and_security(self):
        """Strict mode should set read_only, tmpfs, and no-new-privileges."""
        kwargs = await self._run_with_mock(SandboxMode.strict)
        assert kwargs["read_only"] is True
        assert "no-new-privileges:true" in kwargs["security_opt"]
        tmpfs = kwargs["tmpfs"]
        assert "/tmp" in tmpfs
        assert "/home/agent" in tmpfs
        assert "/root" in tmpfs

    async def test_sandbox_mode_in_container_env(self):
        """Container environment should include SANDBOX_MODE."""
        kwargs = await self._run_with_mock(SandboxMode.strict)
        assert kwargs["environment"]["SANDBOX_MODE"] == "strict"

    async def test_both_modes_have_net_admin(self):
        """Both modes should add NET_ADMIN (needed for iptables setup)."""
        for mode in [SandboxMode.standard, SandboxMode.strict]:
            kwargs = await self._run_with_mock(mode)
            assert "NET_ADMIN" in kwargs["cap_add"], f"NET_ADMIN missing in {mode.value}"


# ── Unit tests: NET_ADMIN drop in entrypoint ─────────────────────────


class TestNetAdminDrop:
    """Test _drop_net_admin function logic."""

    @patch("ctypes.util.find_library", return_value=None)
    def test_drop_warns_if_no_libc(self, mock_find, capsys):
        """Should warn (not crash) if libc not found."""
        from sandclaude.runner.entrypoint import _drop_net_admin

        _drop_net_admin()
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "could not find libc" in captured.out

    @patch("ctypes.util.find_library", return_value="libc.so.6")
    def test_drop_calls_prctl(self, mock_find):
        """Should call prctl with PR_CAPBSET_DROP and CAP_NET_ADMIN."""
        mock_libc = MagicMock()
        mock_libc.prctl.return_value = 0

        with patch("ctypes.CDLL", return_value=mock_libc):
            from sandclaude.runner.entrypoint import _drop_net_admin

            _drop_net_admin()

            mock_libc.prctl.assert_called_once_with(
                24, 12, 0, 0, 0
            )  # PR_CAPBSET_DROP, CAP_NET_ADMIN

    @patch("ctypes.util.find_library", return_value="libc.so.6")
    def test_drop_warns_on_prctl_failure(self, mock_find, capsys):
        """Should warn if prctl returns non-zero."""
        mock_libc = MagicMock()
        mock_libc.prctl.return_value = -1

        with patch("ctypes.CDLL", return_value=mock_libc):
            with patch("ctypes.get_errno", return_value=1):
                from sandclaude.runner.entrypoint import _drop_net_admin

                _drop_net_admin()

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "prctl" in captured.out


# ── Doctor endpoint tests ────────────────────────────────────────────


@pytest.fixture
async def _setup_doctor(tmp_path):
    import sandclaude.config as cfg
    import sandclaude.db.store as store

    cfg.settings.data_dir = tmp_path
    cfg.settings.anthropic_api_key = "test-key"
    cfg.settings.environment = "test"
    cfg.settings.github_client_id = ""
    cfg.settings.github_client_secret = ""
    cfg.settings.sandbox_mode = SandboxMode.standard
    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()
    init_token()


@pytest.fixture
async def client(_setup_doctor):
    from sandclaude.auth import get_token

    token = get_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


class TestDoctorSandboxCheck:
    async def test_sandbox_mode_in_doctor(self, client):
        """Doctor should include sandbox_mode check."""
        resp = await client.get("/admin/doctor")
        assert resp.status_code == 200
        check_names = [c["name"] for c in resp.json()["checks"]]
        assert "sandbox_mode" in check_names

    async def test_standard_mode_passes_in_test(self, client):
        """Standard mode should pass (not warn) in test environment."""
        import sandclaude.config as cfg

        cfg.settings.sandbox_mode = SandboxMode.standard
        resp = await client.get("/admin/doctor")
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert checks["sandbox_mode"]["status"] == "pass"
        assert "standard" in checks["sandbox_mode"]["message"]

    async def test_standard_mode_warns_in_production(self, client):
        """Standard mode should warn in production."""
        import sandclaude.config as cfg

        cfg.settings.sandbox_mode = SandboxMode.standard
        cfg.settings.environment = "production"
        try:
            resp = await client.get("/admin/doctor")
            checks = {c["name"]: c for c in resp.json()["checks"]}
            assert checks["sandbox_mode"]["status"] == "warn"
            assert "strict" in checks["sandbox_mode"]["message"]
        finally:
            cfg.settings.environment = "test"

    async def test_strict_mode_passes(self, client):
        """Strict mode should always pass."""
        import sandclaude.config as cfg

        cfg.settings.sandbox_mode = SandboxMode.strict
        try:
            resp = await client.get("/admin/doctor")
            checks = {c["name"]: c for c in resp.json()["checks"]}
            assert checks["sandbox_mode"]["status"] == "pass"
            assert "strict" in checks["sandbox_mode"]["message"]
            assert "read-only" in checks["sandbox_mode"]["message"]
        finally:
            cfg.settings.sandbox_mode = SandboxMode.standard
