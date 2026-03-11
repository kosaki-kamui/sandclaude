"""Tests for container path validation (symlinks, sensitive paths, boundary checks)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from sandclaude.runner.container import _validate_local_path


@pytest.fixture(autouse=True)
def _setup_env():
    import sandclaude.config as cfg

    cfg.settings.environment = "development"
    cfg.settings.allowed_repo_base = ""
    cfg.settings.host_cwd = ""
    yield


@pytest.fixture
def safe_tmp():
    """Create a temp directory outside /tmp for tests that need a non-sensitive path.

    On CI (Linux), pytest's tmp_path is under /tmp which is in the sensitive
    path blocklist. This fixture creates a directory under /home or the user's
    home directory instead.
    """
    # Use home directory as base (not /tmp)
    home = os.path.expanduser("~")
    base = os.path.join(home, ".sandclaude-test")
    os.makedirs(base, exist_ok=True)
    d = tempfile.mkdtemp(prefix="sc-", dir=base)
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


class TestSensitivePaths:
    def test_blocks_etc(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/etc")

    def test_blocks_etc_subpath(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/etc/shadow")

    def test_blocks_root_home(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/root")

    def test_blocks_proc(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/proc/1/environ")

    def test_blocks_var_log(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/var/log/syslog")

    def test_blocks_root_fs(self):
        with pytest.raises(RuntimeError, match="root filesystem"):
            _validate_local_path("/")

    def test_blocks_traversal_to_etc(self):
        with pytest.raises(RuntimeError):
            _validate_local_path("/home/user/../../../etc/shadow")

    def test_blocks_traversal_to_root(self):
        with pytest.raises(RuntimeError):
            _validate_local_path("/home/user/../../..")

    def test_blocks_private_etc_macos(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/private/etc")

    def test_blocks_private_var_log_macos(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/private/var/log")

    def test_blocks_private_tmp_macos(self):
        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path("/private/tmp")

    def test_allows_normal_path(self):
        _validate_local_path("/home/user/projects/myrepo")


class TestSymlinkResolution:
    def test_symlink_to_sensitive_path_blocked(self, safe_tmp):
        link = safe_tmp / "sneaky-link"
        link.symlink_to("/etc")

        with pytest.raises(RuntimeError, match="sensitive"):
            _validate_local_path(str(link))

    def test_symlink_to_root_blocked(self, safe_tmp):
        link = safe_tmp / "root-link"
        link.symlink_to("/")

        with pytest.raises(RuntimeError, match="root filesystem|sensitive"):
            _validate_local_path(str(link))

    def test_symlink_to_normal_path_allowed(self, safe_tmp):
        target = safe_tmp / "real-repo"
        target.mkdir()
        link = safe_tmp / "repo-link"
        link.symlink_to(target)

        _validate_local_path(str(link))  # Should not raise


class TestAllowedRepoBase:
    def test_allowed_base_enforced(self, safe_tmp):
        import sandclaude.config as cfg

        cfg.settings.allowed_repo_base = str(safe_tmp)

        sub = safe_tmp / "repo"
        sub.mkdir()
        _validate_local_path(str(sub))

        with pytest.raises(RuntimeError, match="not under any allowed"):
            _validate_local_path("/home/other/repo")

    def test_symlink_escaping_allowed_base(self, safe_tmp):
        import sandclaude.config as cfg

        allowed = safe_tmp / "allowed"
        allowed.mkdir()
        cfg.settings.allowed_repo_base = str(allowed)

        escape_link = allowed / "escape"
        outside = safe_tmp / "outside"
        outside.mkdir()
        escape_link.symlink_to(outside)

        with pytest.raises(RuntimeError, match="not under any allowed"):
            _validate_local_path(str(escape_link))


class TestProductionMode:
    def test_production_requires_host_cwd_or_allowed_base(self):
        import sandclaude.config as cfg

        cfg.settings.environment = "production"
        cfg.settings.host_cwd = ""
        cfg.settings.allowed_repo_base = ""

        with pytest.raises(RuntimeError, match="HOST_CWD or ALLOWED_REPO_BASE"):
            _validate_local_path("/home/user/repo")

    def test_production_with_host_cwd(self, safe_tmp):
        import sandclaude.config as cfg

        cfg.settings.environment = "production"
        cfg.settings.host_cwd = str(safe_tmp)

        sub = safe_tmp / "repo"
        sub.mkdir()
        _validate_local_path(str(sub))  # Should not raise

        with pytest.raises(RuntimeError, match="not under HOST_CWD"):
            _validate_local_path("/other/path")
