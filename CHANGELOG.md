# Changelog

All notable changes to sandclaude will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-03-12

### Added

- **Approval gates** — policy-driven gates on high-risk actions (PR creation, push). Tasks enter `pending_approval` status when gates are required. Approve/reject via API with `tasks:approve` scope enforcement
- **Scoped token registry** — named tokens with scopes (`tasks:create`, `tasks:read`, `tasks:approve`, `prs:create`, `admin:tokens`, `admin:policies`), expiry, and revocation. Legacy tokens retain full admin access
- **Policy presets** — named config bundles (`bugfix-pr`, `docs-only`, `tests-only`, `deps-upgrade`, `review-only`) with restrictive merge semantics (allowlists intersect, denylists union, numerics min)
- **Secrets management** — tasks declare needed secrets, server resolves against policy, injects per-phase, scrubs before agent phase. Audit logs record names (never values)
- **PR risk summary** — structured risk assessment (file categorization, sensitive file detection, dependency changes, test coverage, risk level) included in PR body and available via `GET /tasks/{id}/risk`
- **AI code review** — `POST /tasks/{id}/review` uses Claude to analyze diffs for risks, missing tests, suspicious changes, and security concerns
- **Approval UI** — server-rendered page at `/approve/{task_id}/{action}` with HMAC-signed short-lived links (1h TTL). View access via link; approve/reject requires user's own API token
- **Task retry** — `POST /tasks/{id}/retry` creates a follow-up task inheriting repo, branch, model, preset, and budget
- **Task bundle export** — `GET /tasks/{id}/bundle` returns reproducible JSON with task metadata, diff, audit, result, approval gates, secrets audit, applied policy, and risk summary
- **Cost budget enforcement** — `cost_budget_usd` field on tasks; enforced after execution, task fails if exceeded
- **Repository and branch policy** — presets can restrict `allowed_repos` and `blocked_branches`; enforced at task creation time (403 if blocked)
- **GitHub Actions examples** — workflows for auto-fixing CI failures, reviewing PRs, and weekly dependency upgrades
- **Network failure explainability** — DNS resolution failures and private IP blocks now include IP class and actionable error messages
- **Startup quickstart guide** — 30-minute zero-to-working guide with scoped tokens, presets, approval flow, and CI integration

### Changed

- **Auth model upgraded** — `_require_auth` now accepts both legacy tokens and registry tokens via `verify_token_with_scopes()`
- **Policy merge uses restrictive semantics** — task overrides can only narrow access (intersection for allowlists), replacing the previous union-based merge
- **GIT_ASKPASS uses `shlex.quote()`** — prevents shell injection from token values containing metacharacters
- **PR body includes risk assessment** — risk badge, change categories, attention files, and cost summary
- **Version bumped to 0.2.0** — pyproject.toml, health endpoint, and API title

### Fixed

- **Shell injection in GIT_ASKPASS** — tokens with `$`, backticks, or quotes were embedded raw in shell scripts
- **Registry tokens rejected by `_require_auth`** — scoped tokens created via `POST /tokens` got 401 on all endpoints because `verify_token()` only checked legacy tokens

## [0.1.0] - 2026-03-12

### Added

- **Fire-and-forget task execution** via REST API (`POST /tasks`)
- **Two-phase network sandbox** — full internet for setup, iptables-locked for agent execution
- **Structured audit trail** — every file read/written, command executed, and network requests inferred from tool calls
- **Concurrent task pool** with priority queue (high/normal/low) and configurable parallelism
- **Per-task cost tracking** — tokens in/out, USD cost
- **Webhook + Slack notifications** with inline diff, cost summary, and audit stats
- **One-click GitHub PRs** from completed tasks with AI-generated summaries (requires `gh` CLI + `GIT_TOKEN`)
- **Claude Code MCP plugin** — submit and manage tasks from inside Claude Code
- **Real-time streaming** via WebSocket (`/tasks/{task_id}/stream`)
- **Automatic cleanup** — old tasks auto-deleted after configurable retention period
- **Orphan recovery** — stale containers detected and cleaned up on server restart
- **Multi-token auth** — primary token + optional `AUTH_TOKENS` for team deployments
- **Docker socket proxy** — never mounts Docker socket directly into the API container
- **gosu-based entrypoint** — proper privilege drop for bind-mount ownership fixes

[0.2.0]: https://github.com/kosaki-kamui/sandclaude/releases/tag/v0.2.0
[0.1.0]: https://github.com/kosaki-kamui/sandclaude/releases/tag/v0.1.0
