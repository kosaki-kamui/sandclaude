# v0.2.0 Architecture Decisions

Phase 0 decisions that must be resolved before feature work begins.

---

## 1. Task Lifecycle: `pending_approval` State

### Current lifecycle

```
queued → setup → running → completed | failed | cancelled
```

### Proposed lifecycle

```
queued → setup → running → pending_approval → completed | failed | cancelled
                                    ↑                ↓
                                    └── rejected ────┘ (→ failed with error="rejected")
```

### Design

- `pending_approval` is a **first-class task status** in the `TaskStatus` enum. A task transitions to `pending_approval` when execution completes and the policy requires approval for at least one action. The task's diff, audit log, and cost data are all available at this point.
- `rejected` is NOT a separate status. Rejection is handled per-gate: the gate status becomes `rejected`, and the gated action (e.g., PR creation) returns 403. The task itself remains in `completed` or `pending_approval` — it is not marked as failed unless all gates are rejected.
- Approval gates are checked **after** task execution completes, **before** gated actions (PR creation, push) are performed. The `POST /tasks/{id}/create-pr` endpoint returns 409 if a `create_pr` gate is pending, 403 if rejected.
- The `requires_approval` flag on the task tracks whether any gates are still pending. It is set to 1 when gates are created and cleared to 0 when all gates are resolved.

### Lifecycle with approval

```
queued → setup → running → completed (if no gates required)
                         → pending_approval (if gates required)
                              → completed (after all gates approved)

pending_approval is a terminal-ish state: execution is done,
the task is waiting for human decision on gated actions.
WebSocket streams report it as "done" so clients stop polling.
```

### Alternative considered

Gate execution itself (task sits in `pending_approval` before running). Rejected because:
- The diff doesn't exist yet, so there's nothing for the approver to review
- It would block a semaphore slot while waiting for human approval
- The value of sandclaude is unattended execution; gating execution defeats the purpose

### Why this approach

Tasks always run to completion. Approval gates control what happens *with the output*, not whether the agent runs. This preserves the fire-and-forget model while adding control over high-risk downstream actions.

---

## 2. Schema Design

### New tables

```sql
-- Approval gates: which actions require approval for which tasks
CREATE TABLE IF NOT EXISTS approval_gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action TEXT NOT NULL,          -- 'create_pr', 'push', 'install_package', etc.
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'approved', 'rejected'
    reason TEXT,                   -- human-provided reason (optional)
    decided_by TEXT,               -- token fingerprint of approver
    decided_at TEXT,               -- ISO timestamp
    created_at TEXT NOT NULL,
    UNIQUE(task_id, action)
);

-- Token registry: named tokens with scopes and expiry
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,   -- SHA256 of the actual token
    scopes TEXT NOT NULL DEFAULT '[]', -- JSON array: ["tasks:create","tasks:read","prs:create"]
    created_at TEXT NOT NULL,
    expires_at TEXT,                   -- NULL = no expiry
    revoked_at TEXT,                   -- NULL = active
    created_by TEXT                    -- token fingerprint of creator (NULL for primary)
);

-- Policy presets: named bundles of constraints
CREATE TABLE IF NOT EXISTS policy_presets (
    name TEXT PRIMARY KEY,
    config TEXT NOT NULL,              -- JSON blob: {allowed_commands, allowed_paths, ...}
    created_at TEXT NOT NULL,
    updated_at TEXT
);

-- Task secrets: which secrets a task declared and whether they were granted
CREATE TABLE IF NOT EXISTS task_secrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    secret_name TEXT NOT NULL,         -- e.g., 'NPM_TOKEN', 'DATABASE_URL'
    phase TEXT NOT NULL DEFAULT 'setup',  -- 'setup' or 'agent'
    granted INTEGER NOT NULL DEFAULT 0,  -- 0 = denied, 1 = granted
    UNIQUE(task_id, secret_name)
);
```

### Changes to existing `tasks` table

```sql
-- New columns via ALTER TABLE (migration)
ALTER TABLE tasks ADD COLUMN policy_preset TEXT;           -- name of applied preset
ALTER TABLE tasks ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0;  -- 1 if any gate is pending
ALTER TABLE tasks ADD COLUMN declared_secrets TEXT;        -- JSON array of requested secret names
ALTER TABLE tasks ADD COLUMN cost_budget_usd REAL;        -- max allowed cost (NULL = no limit)
```

### Migration strategy

- All migrations run in `init_db()` using `ALTER TABLE ... ADD COLUMN` with defaults
- SQLite supports ADD COLUMN but not DROP/RENAME, so column additions only
- New tables are created with `CREATE TABLE IF NOT EXISTS`
- No data migration needed for existing v0.1.0 tasks (new columns are nullable/defaulted)

---

## 3. Preset + Per-Task Override Merge Semantics

### Problem

If a preset says `allowed_commands: ["npm", "pip"]` and the task request says `allowed_commands: ["cargo"]`, what wins?

### Decision: restrictive composition — task overrides can only narrow, never widen

```
effective = preset ∩ task_overrides  (for allowlists)
effective = preset ∪ task_overrides  (for deny lists)
effective = min(preset, task)        (for numerics)
```

Specifically:

| Field type | Merge rule | Example |
|-----------|-----------|---------|
| Allowlists (allowed_commands, allowed_domains, allowed_paths, allowed_secrets, allowed_repos) | Intersection | preset `["npm", "pip"]` + task `["pip", "cargo"]` = `["pip"]` |
| Deny lists (blocked_branches, requires_approval_for) | Union | preset `["main"]` + task `["staging"]` = `["main", "staging"]` |
| Numerics (max_turns, max_cost_usd, timeout_s) | Minimum (most restrictive) | preset `max_cost=5.0` + task `max_cost=1.0` = `1.0` |
| Restriction booleans (pr_only) | Most restrictive wins (True > False) | preset `false` + task `true` = `true` |
| Permissive booleans (allow_pr_creation) | Most restrictive wins (False > True) | preset `true` + task `false` = `false` |
| Strings (model) | Task override wins | preset `claude-sonnet-4-5` + task `claude-opus-4-6` = `claude-opus-4-6` |

### Rationale

- **Allowlists use intersection** because a task should not be able to grant itself access beyond what the preset allows. If the preset says `["npm", "pip"]` and the task asks for `["cargo"]`, the result is `[]` — the task gets nothing outside the preset. This is the only safe default for a security boundary.
- **Deny lists use union** because a task should be able to add restrictions but not remove them. If the preset blocks `main` and the task also blocks `staging`, both are blocked.
- **Numerics use minimum** because a task should be able to lower its own budget but not exceed the preset ceiling.
- **Restriction booleans use OR** (True wins) because if either the preset or the task wants the restriction, it applies.
- **Permissive booleans use AND** (False wins) because both must agree to allow the action.
- **Strings override** because non-security fields like model selection are the task's choice.

### Implementation

See `policy.py:merge_policy()` — classifies each field by type (allowlist, denylist, numeric ceiling, restriction bool, permissive bool, or string) and applies the corresponding merge rule. Field classification is defined in module-level frozensets for auditability.

---

## 4. Compatibility Policy

### Decision: v0.2.0 is a breaking release

- **API**: New fields in `POST /tasks` request (policy_preset, declared_secrets, cost_budget). These are optional with backward-compatible defaults. Non-breaking.
- **API**: New endpoints (`POST /tasks/{id}/approve`, `POST /tasks/{id}/reject`, token management). Additive. Non-breaking.
- **API**: `POST /tasks/{id}/create-pr` may return 409 if approval is required. This is a behavior change for users who call create-pr without an approval step. **Breaking.**
- **DB**: Schema migration adds columns and tables. One-way migration. Users cannot downgrade to v0.1.0 without a DB backup. **Breaking.**
- **Config**: New environment variables (SECRET_*, POLICY_PRESET_*). Optional with defaults. Non-breaking.
- **Auth**: Token format changes from bare string to registered tokens with scopes. Old tokens continue working as "admin" scope for backward compatibility. Non-breaking for existing users.

### Migration path

1. v0.1.0 tokens become "legacy admin" tokens (all scopes granted)
2. `AUTH_TOKENS` continues to work but is deprecated in favor of the token registry
3. Users who never set a policy preset get the implicit "default" preset (no restrictions, no approval required) — preserving v0.1.0 behavior exactly
4. First run after upgrade: `init_db()` runs migrations automatically

### What this means for users

- Existing v0.1.0 deployments upgrade by pulling new code and restarting
- No manual migration steps required
- Behavior is identical to v0.1.0 unless a policy preset or approval gate is explicitly configured
- The only potential surprise: if a future default preset enables approval for PR creation (but v0.2.0 will NOT do this — the default preset is permissive)

---

## 5. Web Console Decision

### Decision: no web console in v0.2.0

Ship a server-rendered approval UI only — a single page served by FastAPI at `/approve/{task_id}` with approve/reject buttons. This is a Jinja2 template, not a frontend app.

### Rationale

- A full console (task list, diff viewer, audit explorer) is a frontend project that would delay v0.2.0 significantly
- The approval flow is the only workflow that genuinely needs a UI (clicking a link in a Slack notification to approve/reject)
- API + MCP plugin covers all other workflows
- A proper console can be built in v0.3.0 with a frontend framework, informed by v0.2.0 usage patterns

### Implementation

- Single Jinja2 template served by FastAPI (`/approve/{task_id}`)
- Shows: task summary, diff preview, audit highlights, approve/reject buttons
- POST to `/tasks/{task_id}/approve` or `/tasks/{task_id}/reject`
- Authenticated via a time-limited, single-use approval token embedded in the URL (generated when the approval notification is sent)
- No session management, no frontend build step, no JavaScript framework

---

## 6. Secrets Model

### Design

Secrets are declared in the task request and resolved by the server at execution time.

```json
POST /tasks
{
  "repo": "...",
  "prompt": "...",
  "declared_secrets": ["NPM_TOKEN", "DATABASE_URL"],
  "policy_preset": "bugfix-pr"
}
```

Server-side secret storage:

```
# .env or environment variables
SECRET_NPM_TOKEN=npm_abc123
SECRET_DATABASE_URL=postgres://...
```

### Resolution rules

1. Task declares secret names it needs
2. Server checks policy preset: is this secret allowed for this preset?
3. If allowed, server injects the secret into the container env for the declared phase (setup or agent)
4. If denied (not in preset allowlist or not configured), task fails with a clear error
5. Undeclared secrets are never injected, even if they exist in the server config
6. Audit log records: secret name, phase, granted/denied. Never records the value.
7. All injected secrets are scrubbed from the container environment before the agent phase (same pattern as GIT_TOKEN), unless the secret is explicitly declared for the agent phase.

### What this does NOT do

- No vault integration (secrets are env vars on the server)
- No secret rotation
- No per-user secret access (scoped to presets, not tokens)
- These are all reasonable v0.3.0+ features

---

## Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Approval model | Gate actions (PR, push), not execution | Preserve fire-and-forget; approver needs the diff to review |
| Rejection state | `failed` with `error="approval_rejected"` | Avoid new terminal state complexity |
| Schema approach | Additive migrations in init_db() | SQLite ADD COLUMN only; no data migration |
| Preset merge | Union for lists, cap for numerics, override for scalars | Presets set ceilings, tasks extend within bounds |
| Compatibility | Breaking release, but backward-compatible defaults | Existing users upgrade with zero config changes |
| Web console | Server-rendered approval page only | Full console deferred to v0.3.0 |
| Secrets | Declared in request, resolved by server, scoped to presets | Minimal viable model; no vault, no rotation |
