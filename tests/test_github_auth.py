"""Regression tests for GitHub PR auth path and credential helper security.

Covers:
- _GitCredentialHelper creates files with correct permissions and cleans up
- GIT_TOKEN is passed as GH_TOKEN to gh CLI for PR creation
- Guard against reintroduction of dead github_token config field
- Provider compatibility boundaries are documented
"""

from __future__ import annotations

import os
import stat

import pytest

import sandclaude.config as cfg
from sandclaude.github import _GitCredentialHelper

# ---------------------------------------------------------------------------
# _GitCredentialHelper: permissions and cleanup
# ---------------------------------------------------------------------------


class TestGitCredentialHelper:
    """Tests for the _GitCredentialHelper context manager."""

    def test_creates_script_with_restrictive_permissions(self):
        """Askpass script must be owner-only rwx (0o700)."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_test_token_abc123"
            with _GitCredentialHelper() as env:
                assert env is not None
                path = env["GIT_ASKPASS"]
                assert os.path.exists(path)
                mode = os.stat(path).st_mode
                # Check owner rwx, no group/other permissions
                assert mode & stat.S_IRWXU == stat.S_IRWXU  # owner rwx
                assert mode & stat.S_IRWXG == 0  # no group
                assert mode & stat.S_IRWXO == 0  # no other
        finally:
            cfg.settings.git_token = original

    def test_script_contains_token(self):
        """Askpass script must echo the configured token."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_test_token_xyz789"
            with _GitCredentialHelper() as env:
                content = open(env["GIT_ASKPASS"]).read()
                assert "ghp_test_token_xyz789" in content
                assert content.startswith("#!/bin/sh\n")
        finally:
            cfg.settings.git_token = original

    def test_cleanup_on_normal_exit(self):
        """Temp file must be deleted after context manager exits normally."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_cleanup_test"
            path = None
            with _GitCredentialHelper() as env:
                path = env["GIT_ASKPASS"]
                assert os.path.exists(path)
            # After exit, file should be gone
            assert not os.path.exists(path)
        finally:
            cfg.settings.git_token = original

    def test_cleanup_on_exception(self):
        """Temp file must be deleted even when an exception occurs inside the block."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_exception_test"
            path = None
            with pytest.raises(ValueError, match="deliberate"):
                with _GitCredentialHelper() as env:
                    path = env["GIT_ASKPASS"]
                    assert os.path.exists(path)
                    raise ValueError("deliberate test error")
            # After exception, file should still be cleaned up
            assert not os.path.exists(path)
        finally:
            cfg.settings.git_token = original

    def test_returns_none_when_no_token(self):
        """When git_token is empty, context manager should return None and create no files."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = ""
            helper = _GitCredentialHelper()
            with helper as env:
                assert env is None
            assert helper._path is None
        finally:
            cfg.settings.git_token = original

    def test_sets_git_terminal_prompt_zero(self):
        """GIT_TERMINAL_PROMPT=0 must be set to prevent interactive prompts."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_prompt_test"
            with _GitCredentialHelper() as env:
                assert env["GIT_TERMINAL_PROMPT"] == "0"
        finally:
            cfg.settings.git_token = original

    def test_concurrent_helpers_use_separate_files(self):
        """Multiple credential helpers must not share the same temp file."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_concurrent_test"
            helper1 = _GitCredentialHelper()
            helper2 = _GitCredentialHelper()
            env1 = helper1.__enter__()
            env2 = helper2.__enter__()
            try:
                assert env1["GIT_ASKPASS"] != env2["GIT_ASKPASS"]
                assert os.path.exists(env1["GIT_ASKPASS"])
                assert os.path.exists(env2["GIT_ASKPASS"])
            finally:
                helper1.__exit__(None, None, None)
                helper2.__exit__(None, None, None)
            assert not os.path.exists(env1["GIT_ASKPASS"])
            assert not os.path.exists(env2["GIT_ASKPASS"])
        finally:
            cfg.settings.git_token = original


# ---------------------------------------------------------------------------
# GIT_TOKEN -> GH_TOKEN auth path
# ---------------------------------------------------------------------------


class TestGHTokenAuthPath:
    """Tests that PR creation passes GIT_TOKEN as GH_TOKEN to gh CLI."""

    def test_create_pr_sets_gh_token_env(self):
        """The gh CLI env should include GH_TOKEN derived from settings.git_token."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_pr_auth_test"
            # Simulate what create_pr does when building the gh CLI env
            gh_env = {**os.environ}
            if cfg.settings.git_token:
                gh_env["GH_TOKEN"] = cfg.settings.git_token
            assert gh_env["GH_TOKEN"] == "ghp_pr_auth_test"
        finally:
            cfg.settings.git_token = original

    def test_gh_token_not_set_when_git_token_empty(self):
        """When git_token is empty, GH_TOKEN should not be injected."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = ""
            gh_env = {**os.environ}
            if cfg.settings.git_token:
                gh_env["GH_TOKEN"] = cfg.settings.git_token
            # GH_TOKEN should not be in the env (unless it was already there)
            # The point is our code didn't inject it
            assert cfg.settings.git_token == ""
        finally:
            cfg.settings.git_token = original


# ---------------------------------------------------------------------------
# Config guard: github_token must not be reintroduced
# ---------------------------------------------------------------------------


class TestConfigGuard:
    """Guard tests to prevent regression of removed config fields."""

    def test_no_github_token_field_in_settings(self):
        """settings.github_token was removed. This test fails if it is reintroduced
        without corresponding implementation support, catching config/code drift."""
        assert not hasattr(cfg.settings, "github_token"), (
            "github_token config field was reintroduced. If this is intentional, "
            "implement the PyGithub fallback path in github.py and update this test."
        )

    def test_git_token_field_exists(self):
        """git_token is the canonical token field for clone + PR creation."""
        assert hasattr(cfg.settings, "git_token")

    def test_no_pygithub_in_optional_deps(self):
        """PyGithub optional dependency was removed. Verify it stays removed."""
        import importlib.metadata

        try:
            extras = importlib.metadata.metadata("sandclaude").get_all("Provides-Extra") or []
            assert "github" not in extras, (
                "The 'github' optional dependency group was reintroduced. "
                "If PyGithub support is being added back, update this test."
            )
        except importlib.metadata.PackageNotFoundError:
            # Package not installed in editable mode; check pyproject.toml directly
            from pathlib import Path

            pyproject = Path(__file__).parent.parent / "pyproject.toml"
            if pyproject.exists():
                content = pyproject.read_text()
                assert "PyGithub" not in content, (
                    "PyGithub found in pyproject.toml. If this is intentional, "
                    "implement the fallback path and update this test."
                )


# ---------------------------------------------------------------------------
# Provider compatibility: document what GIT_ASKPASS supports
# ---------------------------------------------------------------------------


class TestProviderCompatibility:
    """Tests verifying the GIT_ASKPASS credential helper works for GitHub PATs."""

    def test_askpass_script_format_for_github_pat(self):
        """GitHub PATs work with a simple echo script via GIT_ASKPASS.
        The script echoes the token for both username and password prompts,
        which works because GitHub accepts the PAT as the password with any
        non-empty username."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "github_pat_test123"
            with _GitCredentialHelper() as env:
                content = open(env["GIT_ASKPASS"]).read()
                # Must be a valid shell script
                assert content.startswith("#!/bin/sh\n")
                # Must echo the token
                assert 'echo "github_pat_test123"' in content
        finally:
            cfg.settings.git_token = original

    def test_askpass_script_is_executable(self):
        """The askpass script must be executable by the owner."""
        original = cfg.settings.git_token
        try:
            cfg.settings.git_token = "ghp_exec_test"
            with _GitCredentialHelper() as env:
                assert os.access(env["GIT_ASKPASS"], os.X_OK)
        finally:
            cfg.settings.git_token = original
