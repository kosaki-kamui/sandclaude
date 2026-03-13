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


def evaluate_approval_rules(
    rules: list[dict[str, Any]],
    action: str,
    *,
    risk_level: str | None = None,
    predicted_cost: float | None = None,
    has_secrets: bool = False,
    repo: str = "",
    preset_name: str | None = None,
) -> str | None:
    """Evaluate approval rules for an action. First matching rule wins.

    Returns "auto_approve", "require_approval", or None (no match).
    """
    for rule in rules:
        rule_action = rule.get("action", "")
        if rule_action != "*" and rule_action != action:
            continue

        when = rule.get("when", {})
        if not _predicates_match(
            when,
            risk_level=risk_level,
            predicted_cost=predicted_cost,
            has_secrets=has_secrets,
            repo=repo,
            preset_name=preset_name,
        ):
            continue

        condition = rule.get("condition")
        if condition in ("auto_approve", "require_approval"):
            return condition

    return None


def _predicates_match(
    when: dict[str, Any],
    *,
    risk_level: str | None = None,
    predicted_cost: float | None = None,
    has_secrets: bool = False,
    repo: str = "",
    preset_name: str | None = None,
) -> bool:
    """Check if all predicates in a 'when' dict match. All must match (AND)."""
    for key, value in when.items():
        if key == "risk_level":
            if risk_level is None:
                return False  # can't evaluate without risk data
            if isinstance(value, list):
                if risk_level not in value:
                    return False
            elif risk_level != value:
                return False

        elif key == "predicted_cost_below":
            if predicted_cost is None:
                return False
            if predicted_cost >= value:
                return False

        elif key == "has_secrets":
            if has_secrets != value:
                return False

        elif key == "repo_matches":
            if not repo.startswith(value):
                return False

        elif key == "preset":
            if preset_name != value:
                return False

        else:
            # Unknown predicate — skip (don't block)
            logger.warning("Unknown approval rule predicate: %s", key)

    return True


async def create_required_gates(
    task_id: str,
    policy: PolicyPresetConfig,
    *,
    risk_level: str | None = None,
    predicted_cost: float | None = None,
    has_secrets: bool = False,
    repo: str = "",
    preset_name: str | None = None,
) -> int:
    """Create approval gates for actions that require approval per the policy.

    If the policy has approval_rules, evaluates them first. Rules can
    auto-approve (skip gate) or force approval. Falls back to the static
    requires_approval_for list for unmatched actions.

    Returns the number of pending gates created.
    """
    actions = policy.requires_approval_for or []
    rules = policy.approval_rules or []
    count = 0

    for action in actions:
        if rules:
            decision = evaluate_approval_rules(
                rules,
                action,
                risk_level=risk_level,
                predicted_cost=predicted_cost,
                has_secrets=has_secrets,
                repo=repo,
                preset_name=preset_name,
            )
            if decision == "auto_approve":
                # Create gate as already-approved
                await db.create_approval_gate(task_id, action)
                from sandclaude.models import ApprovalStatus

                await db.decide_approval_gate(
                    task_id,
                    action,
                    decision=ApprovalStatus.approved,
                    decided_by="system:auto_approve",
                    reason="Auto-approved by policy rule",
                )
                logger.info("Auto-approved gate '%s' for task %s", action, task_id)
                continue

        # Default: create pending gate
        await db.create_approval_gate(task_id, action)
        count += 1

    if count > 0:
        await db.update_task(task_id, requires_approval=1)
    return count


async def evaluate_post_execution_rules(
    task_id: str,
    policy: PolicyPresetConfig,
    *,
    risk_level: str | None = None,
    predicted_cost: float | None = None,
    has_secrets: bool = False,
    repo: str = "",
    preset_name: str | None = None,
) -> int:
    """Re-evaluate approval rules after execution with full context (including risk).

    Auto-approves any pending gates that now match auto_approve rules.
    Returns number of gates auto-approved.
    """
    rules = policy.approval_rules
    if not rules:
        return 0

    gates = await db.get_approval_gates(task_id)
    auto_approved = 0

    for gate in gates:
        if gate.status.value != "pending":
            continue

        decision = evaluate_approval_rules(
            rules,
            gate.action,
            risk_level=risk_level,
            predicted_cost=predicted_cost,
            has_secrets=has_secrets,
            repo=repo,
            preset_name=preset_name,
        )
        if decision == "auto_approve":
            from sandclaude.models import ApprovalStatus

            await db.decide_approval_gate(
                task_id,
                gate.action,
                decision=ApprovalStatus.approved,
                decided_by="system:auto_approve",
                reason=f"Auto-approved after execution (risk={risk_level})",
            )
            auto_approved += 1
            logger.info(
                "Post-execution auto-approved gate '%s' for task %s (risk=%s)",
                gate.action,
                task_id,
                risk_level,
            )

    if auto_approved > 0:
        # Check if all gates are now decided
        if not await db.has_pending_gates(task_id):
            await db.update_task(task_id, requires_approval=0)

    return auto_approved


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
