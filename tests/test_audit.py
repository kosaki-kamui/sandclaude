"""Tests for v0.4.0 audit clarity (Epic E)."""

from __future__ import annotations

from types import SimpleNamespace

from sandclaude.models import AuditLog
from sandclaude.runner.executor import (
    _build_audit,
    _build_operator_summary,
    _extract_audit,
    _extract_network_from_command,
)

# ── AuditLog schema version ─────────────────────────────────────────


class TestAuditLogSchema:
    def test_schema_version_default(self):
        log = AuditLog(task_id="t1", started_at="", completed_at="")
        assert log.schema_version == "2"

    def test_schema_version_in_json(self):
        log = AuditLog(task_id="t1", started_at="", completed_at="")
        data = log.model_dump()
        assert data["schema_version"] == "2"

    def test_operator_summary_default_empty(self):
        log = AuditLog(task_id="t1", started_at="", completed_at="")
        assert log.operator_summary == {}

    def test_backward_compat_no_schema_version(self):
        """v1 audit logs without schema_version should still parse."""
        data = {
            "task_id": "old",
            "started_at": "2026-01-01",
            "completed_at": "2026-01-02",
            "files_read": ["/a"],
            "files_written": [],
            "commands_executed": [],
            "network_requests": [],
            "tokens": {"input": 0, "output": 0},
            "estimated_cost_usd": 0.0,
            "warnings": [],
        }
        log = AuditLog(**data)
        assert log.schema_version == "2"  # default applied
        assert log.operator_summary == {}


# ── Network request source classification ────────────────────────────


class TestNetworkSourceClassification:
    def test_webfetch_is_observed(self):
        """WebFetch tool calls should be tagged source=observed."""
        block = SimpleNamespace(name="WebFetch", input={"url": "https://example.com/api"})
        requests: list[dict] = []
        _extract_audit(block, set(), set(), [], requests, ["example.com"])
        assert len(requests) == 1
        assert requests[0]["source"] == "observed"
        assert requests[0]["allowed"] is True

    def test_websearch_is_observed(self):
        """WebSearch tool calls should be tagged source=observed."""
        block = SimpleNamespace(name="WebSearch", input={"url": "https://google.com/search"})
        requests: list[dict] = []
        _extract_audit(block, set(), set(), [], requests, [])
        assert len(requests) == 1
        assert requests[0]["source"] == "observed"
        assert requests[0]["allowed"] is False

    def test_bash_curl_is_inferred(self):
        """Network requests inferred from bash commands should be source=inferred."""
        requests: list[dict] = []
        _extract_network_from_command(
            "curl https://registry.npmjs.org/express",
            requests,
            ["registry.npmjs.org"],
        )
        assert len(requests) == 1
        assert requests[0]["source"] == "inferred"
        assert requests[0]["destination"] == "registry.npmjs.org"
        assert requests[0]["allowed"] is True

    def test_pip_install_is_inferred(self):
        """pip install URLs should be source=inferred."""
        requests: list[dict] = []
        _extract_network_from_command(
            "pip install https://files.pythonhosted.org/packages/some-package.tar.gz",
            requests,
            [],
        )
        assert len(requests) == 1
        assert requests[0]["source"] == "inferred"
        assert requests[0]["allowed"] is False

    def test_no_network_in_bash(self):
        """Regular bash commands should not create network entries."""
        requests: list[dict] = []
        _extract_network_from_command("ls -la /workspace", requests, [])
        assert len(requests) == 0


# ── Operator summary ────────────────────────────────────────────────


class TestOperatorSummary:
    def test_basic_counts(self):
        summary = _build_operator_summary(
            files_read={"a.py", "b.py", "c.py"},
            files_written={"a.py"},
            commands_executed=["ls", "pytest", "npm install"],
            network_requests=[
                {"destination": "api.anthropic.com", "allowed": True, "source": "observed"},
                {"destination": "evil.com", "allowed": False, "source": "inferred"},
            ],
            warnings=["something happened"],
        )
        assert summary["files_read_count"] == 3
        assert summary["files_written_count"] == 1
        assert summary["commands_count"] == 3
        assert summary["network_observed_count"] == 1
        assert summary["network_inferred_count"] == 1
        assert summary["network_blocked_count"] == 1
        assert summary["blocked_destinations"] == ["evil.com"]
        assert summary["warning_count"] == 1

    def test_empty_audit(self):
        summary = _build_operator_summary(
            files_read=set(),
            files_written=set(),
            commands_executed=[],
            network_requests=[],
            warnings=[],
        )
        assert summary["files_read_count"] == 0
        assert summary["network_blocked_count"] == 0
        assert summary["blocked_destinations"] == []
        assert summary["warning_count"] == 0

    def test_multiple_blocked_deduped(self):
        """Blocked destinations should be deduplicated."""
        summary = _build_operator_summary(
            files_read=set(),
            files_written=set(),
            commands_executed=[],
            network_requests=[
                {"destination": "evil.com", "allowed": False, "source": "inferred"},
                {"destination": "evil.com", "allowed": False, "source": "inferred"},
                {"destination": "bad.com", "allowed": False, "source": "observed"},
            ],
            warnings=[],
        )
        assert summary["network_blocked_count"] == 3
        assert summary["blocked_destinations"] == ["bad.com", "evil.com"]


# ── _build_audit integration ────────────────────────────────────────


class TestBuildAudit:
    def test_build_includes_schema_version(self):
        audit = _build_audit(
            task_id="t1",
            started_at="2026-01-01",
            completed_at="2026-01-02",
            files_read=set(),
            files_written=set(),
            commands_executed=[],
            network_requests=[],
            tokens_input=100,
            tokens_output=50,
            estimated_cost_usd=0.01,
        )
        assert audit.schema_version == "2"

    def test_build_includes_operator_summary(self):
        audit = _build_audit(
            task_id="t2",
            started_at="2026-01-01",
            completed_at="2026-01-02",
            files_read={"src/main.py"},
            files_written={"src/main.py", "src/utils.py"},
            commands_executed=["pytest tests/"],
            network_requests=[
                {"destination": "api.anthropic.com", "allowed": True, "source": "observed"},
            ],
            tokens_input=5000,
            tokens_output=2000,
            estimated_cost_usd=0.05,
            warnings=["test warning"],
        )
        s = audit.operator_summary
        assert s["files_read_count"] == 1
        assert s["files_written_count"] == 2
        assert s["commands_count"] == 1
        assert s["network_observed_count"] == 1
        assert s["network_inferred_count"] == 0
        assert s["network_blocked_count"] == 0
        assert s["warning_count"] == 1

    def test_build_json_roundtrip(self):
        """Audit should survive JSON serialization."""
        import json

        audit = _build_audit(
            task_id="t3",
            started_at="2026-01-01",
            completed_at="2026-01-02",
            files_read={"a.py"},
            files_written=set(),
            commands_executed=[],
            network_requests=[],
            tokens_input=0,
            tokens_output=0,
            estimated_cost_usd=0.0,
        )
        data = json.loads(audit.model_dump_json())
        assert data["schema_version"] == "2"
        assert "operator_summary" in data
        reparsed = AuditLog(**data)
        assert reparsed.schema_version == "2"


# ── Extract audit from tool blocks ──────────────────────────────────


class TestExtractAudit:
    def test_read_tool(self):
        block = SimpleNamespace(name="Read", input={"file_path": "/workspace/foo.py"})
        files_read: set[str] = set()
        _extract_audit(block, files_read, set(), [], [], [])
        assert "/workspace/foo.py" in files_read

    def test_edit_tool(self):
        block = SimpleNamespace(
            name="Edit",
            input={"file_path": "/workspace/bar.py", "old_string": "a", "new_string": "b"},
        )
        files_written: set[str] = set()
        _extract_audit(block, set(), files_written, [], [], [])
        assert "/workspace/bar.py" in files_written

    def test_write_tool(self):
        block = SimpleNamespace(
            name="Write", input={"file_path": "/workspace/new.py", "content": "x"}
        )
        files_written: set[str] = set()
        _extract_audit(block, set(), files_written, [], [], [])
        assert "/workspace/new.py" in files_written

    def test_bash_tool(self):
        block = SimpleNamespace(name="Bash", input={"command": "pytest tests/"})
        cmds: list[str] = []
        _extract_audit(block, set(), set(), cmds, [], [])
        assert cmds == ["pytest tests/"]

    def test_non_dict_input_ignored(self):
        """Blocks with non-dict input should be silently skipped."""
        block = SimpleNamespace(name="Read", input="not a dict")
        files_read: set[str] = set()
        _extract_audit(block, files_read, set(), [], [], [])
        assert len(files_read) == 0
