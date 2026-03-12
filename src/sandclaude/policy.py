"""
v0.2.0: Policy engine — preset resolution, merge logic, approval gate creation.

SECURITY-CRITICAL: The merge semantics are restrictive by design.
Task-level overrides can only narrow access, never widen it.
- Allowlists (commands, domains, paths, secrets): intersection
- Deny lists (blocked_branches, requires_approval_for): union
- Numerics (max_turns, max_cost_usd, timeout_s): minimum wins
- Restriction booleans (pr_only): most restrictive wins (True > False)
- Permissive booleans (allow_pr_creation): most restrictive wins (False > True)
- Strings (model): task override wins (non-security-relevant)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sandclaude.db import store as db
from sandclaude.models import PolicyPresetConfig, Task

logger = logging.getLogger(__name__)

# Fields where task overrides intersect with preset (can only narrow)
_ALLOWLIST_FIELDS = frozenset(
    {
        "allowed_commands",
        "allowed_write_paths",
        "allowed_domains",
        "allowed_secrets",
        "allowed_repos",
    }
)

# Fields where task overrides union with preset (can only add restrictions)
_DENYLIST_FIELDS = frozenset(
    {
        "blocked_branches",
        "requires_approval_for",
    }
)

# Numeric fields where the minimum (most restrictive) wins
_NUMERIC_CEILING_FIELDS = frozenset(
    {
        "max_turns",
        "max_cost_usd",
        "timeout_s",
    }
)

# Boolean fields where True is more restrictive
_RESTRICTIVE_BOOL_FIELDS = frozenset(
    {
        "pr_only",
    }
)

# Boolean fields where False is more restrictive
_PERMISSIVE_BOOL_FIELDS = frozenset(
    {
        "allow_pr_creation",
    }
)


def merge_policy(preset: dict[str, Any], task_overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge a preset config with per-task overrides using restrictive semantics.

    Task overrides can only narrow access, never widen it:
    - Allowlists: intersection (task can remove items, not add)
    - Deny lists: union (task can add restrictions, not remove)
    - Numerics: minimum wins (task can lower ceiling, not raise)
    - Restriction bools: most restrictive wins
    - Strings: task override wins (non-security fields like model)
    """
    merged = {**preset}
    for key, value in task_overrides.items():
        if value is None:
            continue
        preset_value = merged.get(key)

        if key in _ALLOWLIST_FIELDS:
            if isinstance(preset_value, list) and isinstance(value, list):
                # Intersection: task can only narrow, not widen
                merged[key] = list(set(preset_value) & set(value))
            elif preset_value is None:
                # No preset restriction — task value becomes the restriction
                merged[key] = value
            # If preset has a list but task doesn't provide one, preset wins

        elif key in _DENYLIST_FIELDS:
            if isinstance(preset_value, list) and isinstance(value, list):
                # Union: task can add deny entries, not remove
                merged[key] = list(set(preset_value) | set(value))
            elif preset_value is None:
                merged[key] = value

        elif key in _NUMERIC_CEILING_FIELDS:
            if isinstance(preset_value, (int, float)) and isinstance(value, (int, float)):
                # Minimum: task can lower ceiling, not raise
                merged[key] = min(value, preset_value)
            elif preset_value is None:
                merged[key] = value

        elif key in _RESTRICTIVE_BOOL_FIELDS:
            if isinstance(preset_value, bool) and isinstance(value, bool):
                # True is more restrictive — OR
                merged[key] = preset_value or value
            elif preset_value is None:
                merged[key] = value

        elif key in _PERMISSIVE_BOOL_FIELDS:
            if isinstance(preset_value, bool) and isinstance(value, bool):
                # False is more restrictive — AND
                merged[key] = preset_value and value
            elif preset_value is None:
                merged[key] = value

        else:
            # Non-security fields (model, etc.): task override wins
            merged[key] = value

    return merged


async def resolve_effective_policy(task: Task) -> PolicyPresetConfig:
    """Resolve the effective policy for a task by merging preset + task overrides.

    Returns the default (fully permissive) policy if no preset is configured.
    """
    base_config: dict[str, Any] = {}

    if task.policy_preset:
        preset = await db.get_policy_preset(task.policy_preset)
        if preset:
            base_config = preset.config
        else:
            logger.warning(
                "Task %s references unknown preset '%s'",
                task.id,
                task.policy_preset,
            )

    # Build task-level overrides from task fields
    task_overrides: dict[str, Any] = {}
    if task.allowed_domains:
        try:
            task_overrides["allowed_domains"] = json.loads(task.allowed_domains)
        except (json.JSONDecodeError, TypeError):
            pass
    if task.cost_budget_usd is not None:
        task_overrides["max_cost_usd"] = task.cost_budget_usd
    if task.max_turns:
        task_overrides["max_turns"] = task.max_turns

    effective = merge_policy(base_config, task_overrides)
    return PolicyPresetConfig(**effective)


async def create_required_gates(task_id: str, policy: PolicyPresetConfig) -> int:
    """Create approval gates for actions that require approval per the policy.

    Returns the number of gates created.
    """
    actions = policy.requires_approval_for or []
    count = 0
    for action in actions:
        await db.create_approval_gate(task_id, action)
        count += 1
    if count > 0:
        await db.update_task(task_id, requires_approval=1)
    return count


def check_secret_allowed(policy: PolicyPresetConfig, secret_name: str) -> bool:
    """Check if a secret is allowed by the policy."""
    if policy.allowed_secrets is None:
        # No restriction — all secrets allowed
        return True
    return secret_name in policy.allowed_secrets


def check_repo_allowed(policy: PolicyPresetConfig, repo: str) -> str | None:
    """Check if a repo is allowed by the policy.

    Returns None if allowed, or an error message if blocked.
    """
    if policy.allowed_repos is None:
        return None  # no restriction
    for pattern in policy.allowed_repos:
        if pattern == repo:
            return None
        if pattern == "." and repo == ".":
            return None
        # Simple prefix matching for URL patterns
        if repo.startswith(pattern):
            return None
    return f"Repo '{repo}' is not in the allowed repos for this preset"


def check_branch_allowed(policy: PolicyPresetConfig, branch: str | None) -> str | None:
    """Check if a branch is allowed by the policy.

    Returns None if allowed, or an error message if blocked.
    """
    if not branch or not policy.blocked_branches:
        return None
    for blocked in policy.blocked_branches:
        if branch == blocked:
            return f"Branch '{branch}' is blocked by policy"
    return None
