"""Tests for v0.2.0 Phase 2: PR risk summary, review mode, presets."""

from __future__ import annotations

import pytest

from sandclaude.db import store as db
from sandclaude.presets import BUILTIN_PRESETS, seed_builtin_presets
from sandclaude.risk import (
    RiskSummary,
    format_risk_summary_markdown,
    generate_risk_summary,
)

# ---------------------------------------------------------------------------
# Risk summary: file classification
# ---------------------------------------------------------------------------


class TestRiskFileClassification:
    def _make_diff(self, files: list[str]) -> str:
        lines = []
        for f in files:
            lines.append(f"diff --git a/{f} b/{f}")
            lines.append(f"--- a/{f}")
            lines.append(f"+++ b/{f}")
            lines.append("@@ -1,1 +1,2 @@")
            lines.append("+changed")
        return "\n".join(lines)

    def test_code_classification(self):
        diff = self._make_diff(["src/auth.py", "src/main.py"])
        summary = generate_risk_summary(diff, {})
        assert "code" in summary.categories
        assert "src/auth.py" in summary.categories["code"]

    def test_test_classification(self):
        diff = self._make_diff(["tests/test_auth.py"])
        summary = generate_risk_summary(diff, {})
        assert "tests" in summary.categories

    def test_config_classification(self):
        diff = self._make_diff(["docker-compose.yml", "Dockerfile"])
        summary = generate_risk_summary(diff, {})
        assert "config" in summary.categories
        assert len(summary.config_files_changed) == 2

    def test_ci_classification(self):
        diff = self._make_diff([".github/workflows/ci.yml"])
        summary = generate_risk_summary(diff, {})
        assert "ci" in summary.categories
        assert len(summary.ci_files_changed) == 1

    def test_docs_classification(self):
        diff = self._make_diff(["README.md", "docs/guide.rst"])
        summary = generate_risk_summary(diff, {})
        assert "docs" in summary.categories

    def test_dependency_classification(self):
        diff = self._make_diff(["package.json", "package-lock.json"])
        summary = generate_risk_summary(diff, {})
        assert "dependencies" in summary.categories
        assert summary.new_dependencies is True
        assert summary.lockfiles_modified is True

    def test_mixed_categories(self):
        diff = self._make_diff(
            [
                "src/auth.py",
                "tests/test_auth.py",
                "README.md",
            ]
        )
        summary = generate_risk_summary(diff, {})
        assert len(summary.categories) == 3


# ---------------------------------------------------------------------------
# Risk summary: risk level determination
# ---------------------------------------------------------------------------


class TestRiskLevel:
    def _make_diff(self, files: list[str]) -> str:
        lines = []
        for f in files:
            lines.append(f"diff --git a/{f} b/{f}")
            lines.append(f"--- a/{f}")
            lines.append(f"+++ b/{f}")
            lines.append("@@ -1,1 +1,2 @@")
            lines.append("+changed")
        return "\n".join(lines)

    def test_low_risk_docs_only(self):
        diff = self._make_diff(["README.md"])
        summary = generate_risk_summary(diff, {})
        assert summary.risk_level == "low"

    def test_medium_risk_sensitive_file(self):
        diff = self._make_diff(["src/auth.py"])
        summary = generate_risk_summary(diff, {})
        assert summary.risk_level == "medium"

    def test_high_risk_ci_changes(self):
        diff = self._make_diff([".github/workflows/deploy.yml"])
        summary = generate_risk_summary(diff, {})
        assert summary.risk_level == "high"

    def test_medium_risk_no_tests_run(self):
        diff = self._make_diff(["src/handler.py"])
        summary = generate_risk_summary(diff, {})
        # Code changed without tests = medium
        assert summary.risk_level in ("medium", "high")
        assert any("test" in r.lower() for r in summary.risk_reasons)

    def test_sensitive_files_detected(self):
        diff = self._make_diff(["src/credentials.py", ".env.example"])
        summary = generate_risk_summary(diff, {})
        assert len(summary.sensitive_files) >= 1


# ---------------------------------------------------------------------------
# Risk summary: audit analysis
# ---------------------------------------------------------------------------


class TestRiskAuditAnalysis:
    def test_command_classification(self):
        diff = "diff --git a/x b/x\n"
        audit = {
            "commands_executed": [
                "npm install express",
                "pytest tests/",
                "ls -la",
            ],
        }
        summary = generate_risk_summary(diff, audit)
        assert len(summary.install_commands) == 1
        assert len(summary.test_commands) == 1
        assert summary.commands_executed == 3

    def test_blocked_network_requests(self):
        diff = "diff --git a/x b/x\n"
        audit = {
            "network_requests": [
                {"destination": "pypi.org", "allowed": True},
                {"destination": "evil.com", "allowed": False},
            ],
        }
        summary = generate_risk_summary(diff, audit)
        assert summary.external_network_access is True
        assert summary.blocked_network_requests == 1

    def test_cost_tracking(self):
        diff = "diff --git a/x b/x\n"
        summary = generate_risk_summary(
            diff,
            {},
            tokens_input=5000,
            tokens_output=1500,
            cost_usd=0.05,
        )
        assert summary.tokens_input == 5000
        assert summary.cost_usd == 0.05


# ---------------------------------------------------------------------------
# Risk summary: markdown formatting
# ---------------------------------------------------------------------------


class TestRiskMarkdownFormat:
    def test_format_includes_risk_badge(self):
        summary = RiskSummary(risk_level="high")
        md = format_risk_summary_markdown(summary)
        assert "High Risk" in md

    def test_format_includes_reasons(self):
        summary = RiskSummary(
            risk_level="medium",
            risk_reasons=["Sensitive files modified"],
        )
        md = format_risk_summary_markdown(summary)
        assert "Sensitive files modified" in md

    def test_format_includes_categories(self):
        summary = RiskSummary(
            categories={"code": ["src/main.py"], "tests": ["tests/test_main.py"]},
        )
        md = format_risk_summary_markdown(summary)
        assert "code" in md
        assert "tests" in md


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------


class TestBuiltinPresets:
    def test_all_presets_have_required_fields(self):
        for name, config in BUILTIN_PRESETS.items():
            assert "allow_pr_creation" in config, f"{name} missing allow_pr_creation"
            assert "max_turns" in config, f"{name} missing max_turns"
            assert "max_cost_usd" in config, f"{name} missing max_cost_usd"

    def test_review_only_preset_blocks_writes(self):
        config = BUILTIN_PRESETS["review-only"]
        assert config["allowed_write_paths"] == []
        assert config["allow_pr_creation"] is False

    def test_bugfix_preset_requires_approval(self):
        config = BUILTIN_PRESETS["bugfix-pr"]
        assert "create_pr" in config["requires_approval_for"]

    def test_docs_only_preset_restricts_domains(self):
        config = BUILTIN_PRESETS["docs-only"]
        assert config["allowed_domains"] == []

    @pytest.mark.asyncio
    async def test_seed_presets(self):
        await db.init_db()
        count = await seed_builtin_presets()
        assert count == len(BUILTIN_PRESETS)

        # Second seed should skip all
        count2 = await seed_builtin_presets()
        assert count2 == 0

        # Verify presets exist
        for name in BUILTIN_PRESETS:
            preset = await db.get_policy_preset(name)
            assert preset is not None, f"Preset {name} not found"

    @pytest.mark.asyncio
    async def test_presets_are_valid_policy_configs(self):
        """Each preset config should be parseable as PolicyPresetConfig."""
        from sandclaude.models import PolicyPresetConfig

        for name, config in BUILTIN_PRESETS.items():
            parsed = PolicyPresetConfig(**config)
            assert parsed.max_turns is not None, f"{name} should have max_turns"
