# Changelog

All notable changes to sandclaude will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-03-12

### Added

- **Fire-and-forget task execution** via REST API (`POST /tasks`)
- **Two-phase network sandbox** — full internet for setup, iptables-locked for agent execution
- **Complete audit trail** — every file read/written, command executed, and network request logged
- **Concurrent task pool** with priority queue (high/normal/low) and configurable parallelism
- **Per-task cost tracking** — tokens in/out, USD cost
- **Webhook + Slack notifications** with inline diff, cost summary, and audit stats
- **One-click GitHub PRs** from completed tasks with AI-generated summaries (Claude Haiku)
- **Claude Code MCP plugin** — submit and manage tasks from inside Claude Code
- **Real-time streaming** via WebSocket (`/tasks/{task_id}/stream`)
- **Automatic cleanup** — old tasks auto-deleted after configurable retention period
- **Orphan recovery** — stale containers detected and cleaned up on server restart
- **Multi-token auth** — primary token + optional `AUTH_TOKENS` for team deployments
- **Docker socket proxy** — never mounts Docker socket directly into the API container
- **gosu-based entrypoint** — proper privilege drop for bind-mount ownership fixes

[0.1.0]: https://github.com/kosaki-kamui/sandclaude/releases/tag/v0.1.0
