"""
v0.2.0: PR risk summary generator.

Analyzes a completed task's diff and audit log to produce a structured
risk assessment for human reviewers. Surfaces what changed, where the
risk is, and what deserves extra attention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# File classification patterns
_CATEGORY_PATTERNS: list[tuple[str, list[str]]] = [
    ("tests", [r"test[s_]", r"spec[s_]", r"__tests__", r"\.test\.", r"\.spec\."]),
    ("ci", [r"\.github/workflows", r"\.gitlab-ci", r"Jenkinsfile", r"\.circleci"]),
    ("config", [
        r"\.env", r"\.yml$", r"\.yaml$", r"\.toml$", r"\.ini$", r"\.cfg$",
        r"Makefile", r"Dockerfile", r"docker-compose", r"\.dockerignore",
    ]),
    ("docs", [r"\.md$", r"\.rst$", r"\.txt$", r"docs/", r"README", r"CHANGELOG"]),
    ("dependencies", [
        r"package\.json$", r"package-lock\.json$", r"yarn\.lock$", r"pnpm-lock",
        r"requirements.*\.txt$", r"Pipfile", r"poetry\.lock", r"pyproject\.toml$",
        r"Cargo\.toml$", r"Cargo\.lock$", r"go\.mod$", r"go\.sum$",
        r"Gemfile", r"Gemfile\.lock",
    ]),
]

_SENSITIVE_PATTERNS: list[str] = [
    r"\.env", r"secret", r"credential", r"password", r"token",
    r"\.pem$", r"\.key$", r"\.cert$", r"\.crt$",
    r"auth", r"permission", r"rbac", r"policy",
    r"migration", r"schema",
]

_LOCKFILE_PATTERNS: list[str] = [
    r"package-lock\.json$", r"yarn\.lock$", r"pnpm-lock\.yaml$",
    r"poetry\.lock$", r"Pipfile\.lock$", r"Cargo\.lock$",
    r"go\.sum$", r"Gemfile\.lock$", r"composer\.lock$",
]


@dataclass
class RiskSummary:
    """Structured risk assessment for a completed task."""

    # File categorization
    files_changed: list[str] = field(default_factory=list)
    categories: dict[str, list[str]] = field(default_factory=dict)
    # categories maps: "code" -> ["src/auth.py"], "tests" -> ["tests/test_auth.py"]

    # Risk signals
    sensitive_files: list[str] = field(default_factory=list)
    new_dependencies: bool = False
    lockfiles_modified: bool = False
    config_files_changed: list[str] = field(default_factory=list)
    ci_files_changed: list[str] = field(default_factory=list)
    external_network_access: bool = False
    blocked_network_requests: int = 0

    # Commands and tests
    commands_executed: int = 0
    test_commands: list[str] = field(default_factory=list)
    install_commands: list[str] = field(default_factory=list)

    # Cost
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0

    # Reviewer attention
    attention_files: list[str] = field(default_factory=list)
    risk_level: str = "low"  # "low", "medium", "high"
    risk_reasons: list[str] = field(default_factory=list)


def _classify_file(path: str) -> str:
    """Classify a file path into a category."""
    for category, patterns in _CATEGORY_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, path, re.IGNORECASE):
                return category
    return "code"


def _is_sensitive(path: str) -> bool:
    for pattern in _SENSITIVE_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return True
    return False


def _is_lockfile(path: str) -> bool:
    for pattern in _LOCKFILE_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return True
    return False


def _is_dep_file(path: str) -> bool:
    for pattern in _CATEGORY_PATTERNS:
        if pattern[0] == "dependencies":
            for p in pattern[1]:
                if re.search(p, path, re.IGNORECASE):
                    return True
    return False


def _is_test_command(cmd: str) -> bool:
    test_indicators = [
        "pytest", "jest", "mocha", "vitest", "cargo test",
        "go test", "npm test", "yarn test", "make test",
        "rspec", "unittest",
    ]
    cmd_lower = cmd.lower()
    return any(t in cmd_lower for t in test_indicators)


def _is_install_command(cmd: str) -> bool:
    install_indicators = [
        "pip install", "npm install", "yarn add", "cargo add",
        "go get", "apt install", "apt-get install", "brew install",
        "gem install", "composer require",
    ]
    cmd_lower = cmd.lower()
    return any(t in cmd_lower for t in install_indicators)


def generate_risk_summary(
    diff: str,
    audit: dict,
    *,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cost_usd: float = 0.0,
) -> RiskSummary:
    """Generate a structured risk summary from a diff and audit log."""
    summary = RiskSummary(
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
    )

    # Extract and classify files
    files = _extract_changed_files(diff)
    summary.files_changed = files

    categories: dict[str, list[str]] = {}
    for f in files:
        cat = _classify_file(f)
        categories.setdefault(cat, []).append(f)
        if _is_sensitive(f):
            summary.sensitive_files.append(f)
        if _is_lockfile(f):
            summary.lockfiles_modified = True
        if _is_dep_file(f):
            summary.new_dependencies = True
    summary.categories = categories
    summary.config_files_changed = categories.get("config", [])
    summary.ci_files_changed = categories.get("ci", [])

    # Analyze commands
    commands = audit.get("commands_executed", [])
    summary.commands_executed = len(commands)
    for cmd in commands:
        if _is_test_command(cmd):
            summary.test_commands.append(cmd)
        if _is_install_command(cmd):
            summary.install_commands.append(cmd)

    # Network analysis
    net_reqs = audit.get("network_requests", [])
    summary.external_network_access = len(net_reqs) > 0
    summary.blocked_network_requests = sum(
        1 for r in net_reqs if not r.get("allowed")
    )

    # Determine attention files and risk level
    summary.attention_files = list(summary.sensitive_files)
    for f in summary.config_files_changed:
        if f not in summary.attention_files:
            summary.attention_files.append(f)
    for f in summary.ci_files_changed:
        if f not in summary.attention_files:
            summary.attention_files.append(f)

    # Risk level determination
    reasons: list[str] = []
    if summary.sensitive_files:
        reasons.append(
            f"Sensitive files modified: {', '.join(summary.sensitive_files[:5])}"
        )
    if summary.lockfiles_modified:
        reasons.append("Lockfiles modified — verify dependency changes")
    if summary.ci_files_changed:
        reasons.append("CI/CD configuration changed")
    if summary.blocked_network_requests > 0:
        reasons.append(
            f"{summary.blocked_network_requests} network request(s) were blocked"
        )
    if summary.new_dependencies and not summary.lockfiles_modified:
        reasons.append(
            "Dependency files changed but no lockfile update — verify"
        )
    if not summary.test_commands and "code" in categories:
        reasons.append("Code changed but no tests were run")

    summary.risk_reasons = reasons
    if len(reasons) >= 3 or summary.ci_files_changed:
        summary.risk_level = "high"
    elif len(reasons) >= 1:
        summary.risk_level = "medium"
    else:
        summary.risk_level = "low"

    return summary


def _extract_changed_files(diff: str) -> list[str]:
    """Extract file paths from a unified diff."""
    files = []
    for line in diff.split("\n"):
        if line.startswith("diff --git"):
            m = re.search(r"b/(.+)$", line)
            if m:
                files.append(m.group(1))
    return files


def format_risk_summary_markdown(summary: RiskSummary) -> str:
    """Format a risk summary as markdown for PR body inclusion."""
    parts: list[str] = []

    # Risk badge
    badge = {
        "low": "🟢 Low Risk",
        "medium": "🟡 Medium Risk",
        "high": "🔴 High Risk",
    }
    parts.append(f"## Risk Assessment: {badge.get(summary.risk_level, '?')}")
    parts.append("")

    # Risk reasons
    if summary.risk_reasons:
        for reason in summary.risk_reasons:
            parts.append(f"- ⚠️ {reason}")
        parts.append("")

    # Change categories table
    parts.append("### Change Categories")
    parts.append("")
    parts.append("| Category | Files |")
    parts.append("|----------|-------|")
    for cat, files in sorted(summary.categories.items()):
        file_list = ", ".join(f"`{f}`" for f in files[:5])
        if len(files) > 5:
            file_list += f" (+{len(files) - 5} more)"
        parts.append(f"| {cat} | {file_list} |")
    parts.append("")

    # Attention files
    if summary.attention_files:
        parts.append("### Files Requiring Extra Review")
        parts.append("")
        for f in summary.attention_files:
            parts.append(f"- `{f}`")
        parts.append("")

    # Dependency changes
    if summary.new_dependencies or summary.lockfiles_modified:
        parts.append("### Dependency Changes")
        parts.append("")
        if summary.new_dependencies:
            parts.append("- Dependency manifest files were modified")
        if summary.lockfiles_modified:
            parts.append("- Lockfiles were updated")
        if summary.install_commands:
            parts.append("")
            parts.append("Install commands run:")
            for cmd in summary.install_commands[:10]:
                parts.append(f"  - `{cmd[:120]}`")
        parts.append("")

    # Test summary
    if summary.test_commands:
        parts.append("### Tests Run")
        parts.append("")
        for cmd in summary.test_commands[:10]:
            parts.append(f"- `{cmd[:120]}`")
        parts.append("")

    # Network
    if summary.external_network_access:
        parts.append("### Network Activity")
        parts.append("")
        if summary.blocked_network_requests > 0:
            parts.append(
                f"- **{summary.blocked_network_requests} blocked** request(s)"
            )
        parts.append(
            "- Network audit is best-effort (inferred from tool calls)"
        )
        parts.append("")

    # Cost
    parts.append("### Cost")
    parts.append("")
    parts.append(
        f"- Tokens: {summary.tokens_input:,} in / {summary.tokens_output:,} out"
    )
    if summary.cost_usd > 0:
        parts.append(f"- Estimated: ${summary.cost_usd:.4f}")
    parts.append("")

    return "\n".join(parts)
