"""
v0.2.0: Built-in policy presets.

These are seeded into the database on first run and can be customized
by the operator via the API.
"""

from __future__ import annotations

# Each preset is a name -> PolicyPresetConfig dict
BUILTIN_PRESETS: dict[str, dict] = {
    "docs-only": {
        "allowed_commands": ["cat", "ls", "find", "grep", "head", "tail"],
        "allowed_write_paths": ["*.md", "*.rst", "*.txt", "docs/"],
        "allowed_domains": [],
        "allow_pr_creation": True,
        "requires_approval_for": [],
        "max_turns": 15,
        "max_cost_usd": 0.50,
    },
    "tests-only": {
        "allowed_commands": [
            "pytest", "jest", "mocha", "vitest", "cargo test", "go test",
            "npm test", "yarn test", "make test", "pip", "npm install",
        ],
        "allowed_write_paths": ["tests/", "test/", "spec/", "__tests__/"],
        "allowed_domains": ["registry.npmjs.org", "pypi.org", "files.pythonhosted.org"],
        "allow_pr_creation": True,
        "requires_approval_for": [],
        "max_turns": 25,
        "max_cost_usd": 2.00,
    },
    "bugfix-pr": {
        "allowed_commands": None,  # no restriction
        "allowed_write_paths": None,  # no restriction
        "allowed_domains": ["registry.npmjs.org", "pypi.org", "files.pythonhosted.org"],
        "allow_pr_creation": True,
        "requires_approval_for": ["create_pr"],
        "max_turns": 30,
        "max_cost_usd": 5.00,
    },
    "deps-upgrade": {
        "allowed_commands": [
            "npm", "yarn", "pip", "pip3", "cargo", "go",
            "npm install", "npm update", "pip install",
            "pytest", "jest", "make test",
        ],
        "allowed_domains": [
            "registry.npmjs.org", "pypi.org", "files.pythonhosted.org",
            "crates.io", "proxy.golang.org",
        ],
        "allow_pr_creation": True,
        "requires_approval_for": ["create_pr"],
        "max_turns": 25,
        "max_cost_usd": 3.00,
    },
    "review-only": {
        "allowed_commands": ["cat", "ls", "find", "grep", "head", "tail", "wc"],
        "allowed_write_paths": [],  # no writes allowed
        "allowed_domains": [],
        "allow_pr_creation": False,
        "requires_approval_for": [],
        "max_turns": 10,
        "max_cost_usd": 1.00,
    },
}


async def seed_builtin_presets() -> int:
    """Seed built-in presets into the database. Skips presets that already exist.

    Returns the number of presets seeded (0 if all already exist).
    """
    from sandclaude.db import store as db

    count = 0
    for name, config in BUILTIN_PRESETS.items():
        existing = await db.get_policy_preset(name)
        if not existing:
            await db.save_policy_preset(name, config)
            count += 1
    return count
