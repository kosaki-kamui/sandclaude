"""
v0.2.0: Policy engine — preset resolution, merge logic, approval gate creation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sandclaude.db import store as db
from sandclaude.models import PolicyPresetConfig, Task

logger = logging.getLogger(__name__)


def merge_policy(preset: dict[str, Any], task_overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge a preset config with per-task overrides.

    Rules:
    - Lists (allowed_commands, allowed_domains, etc.): union
    - Numerics (max_turns, max_cost_usd, timeout_s): task can lower but not exceed preset ceiling
    - Booleans/strings: task override wins if explicitly set
    """
    merged = {**preset}
    for key, value in task_overrides.items():
        if value is None:
            continue
        preset_value = merged.get(key)
        if isinstance(preset_value, list) and isinstance(value, list):
            # Union for list fields
            merged[key] = list(set(preset_value) | set(value))
        elif isinstance(preset_value, (int, float)) and isinstance(value, (int, float)):
            # Task can lower but not exceed preset ceiling
            merged[key] = min(value, preset_value)
        else:
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
            logger.warning("Task %s references unknown preset '%s'", task.id, task.policy_preset)

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
