# Design Decisions

> Every decision is backed by an observation or measurement from the sandclaude build.

## 1. Docker Dual-Network Instead of Firecracker

**Decision:** Use Docker with two bridge networks (setup-net, agent-net) rather than Firecracker microVMs.

**Reasoning:**
- Docker is ubiquitous: every developer machine has it. Firecracker requires Linux + KVM.
- Docker Compose makes the setup a single `docker compose up` command.
- The iptables-based network isolation is sufficient for the threat model (data residency, not adversarial AI containment).
- Firecracker adds ~2s boot time overhead but provides kernel-level isolation. Worth it for enterprise, not for MVP.

**Trade-off:** Docker container isolation is weaker than VM-level. A kernel exploit could escape. Documented in README.md "Limitations" section.

## 2. Two-Phase Network Instead of Full Isolation

**Decision:** Allow full internet during setup phase (repo clone), restrict to Anthropic API + configurable `allowed_domains` during agent phase.

**Reasoning:**
- The setup phase needs full internet for `git clone` from any Git host.
- Dependency installation is handled by Claude during the agent phase, using `allowed_domains` to access package registries (e.g., `registry.npmjs.org`, `pypi.org`). This lets Claude decide what to install based on what it sees in the project.
- Pre-baking all dependencies into the Docker image is impractical because each task may target a different repo with different deps.
- The two-phase model is the minimum viable security boundary: clone with full network, agent runs with restricted network.

**Trade-off:** Previously there was a brief window between setup completion and network switch. This was eliminated by the S3 fix: iptables rules are now applied *before* the network switch, so the container is firewalled before it leaves setup-net. The setup phase also does not run any agent code.

## 3. `maxTurns` as Primary Safety Valve

**Decision:** Use the Agent SDK's `maxTurns` parameter as the main mechanism to prevent runaway tasks.

**Reasoning:**
- **Observed (Day 1 spike):** A simple 3-bug fix completed in 6 turns. Setting maxTurns to 50 provides ample room for complex tasks while preventing infinite loops.
- The SDK returns `SDKResultError` with `subtype: "error_max_turns"` when the limit is reached (confirmed via type definitions; not triggered in testing since tasks completed within limits).
- Combined with `TASK_TIMEOUT_S` (wall clock timeout, default 30 min), this provides two independent safety mechanisms.

**Trade-off:** If maxTurns is set too low, complex tasks may be truncated. The SDK's behavior at the limit (whether partial results are captured) is documented but not fully verified (README.md "Limitations" section).

## 4. Audit Trail Is Mandatory, Not Optional

**Decision:** Every task produces a structured audit log. There is no way to disable it.

**Reasoning:**
- Teams benefit from audit trails for reviewing agent behavior and tracking what changed.
- The audit log is the primary "wow" moment in demos: it demonstrates transparency.
- Storage cost is negligible (JSON files, a few KB per task).
- Making it optional would introduce a configuration surface that users could misconfigure.

**Trade-off:** The audit trail captures tool calls and shell commands, but network requests are best-effort (inferred from tool calls, not captured at packet level). Documented in README.md "Limitations" section.

## 5. stdio MCP Transport Over HTTP

**Decision:** The Claude Code plugin uses stdio (stdin/stdout) transport, not HTTP or SSE.

**Reasoning:**
- Claude Code's convention for MCP servers is stdio; it is the simplest and most reliable transport.
- stdio doesn't require port management, firewall rules, or CORS configuration.
- The plugin authenticates to the API server via Bearer token over HTTP, but the MCP transport itself is local stdio.
- HTTP-based MCP transport would require the MCP server to run as a long-lived process with its own port, adding complexity.

**Trade-off:** The plugin must be registered with `claude mcp add` and runs as a subprocess of Claude Code. It can't be shared across multiple Claude Code sessions (each gets its own instance).

## 6. SQLite Over In-Memory State

**Decision:** Task state is persisted to SQLite, not held in memory.

**Reasoning:**
- **Problem observed:** If the API server crashes while tasks are running, in-memory state is lost. Running tasks become invisible, and containers are leaked.
- SQLite with WAL mode handles concurrent reads/writes efficiently.
- On startup, orphan detection scans for tasks in `running`/`setup` state and checks if their containers are still alive. This would be impossible without persistence.
- Task history is queryable for cost tracking and audit.

**Trade-off:** SQLite adds ~30ms overhead per write operation. For a task that takes 30+ seconds, this is negligible.

## 7. In-Container iptables Over Host-Level Rules

**Decision:** Network isolation rules are applied inside the container via `docker exec`, not on the Docker host.

**Reasoning:**
- Host-level iptables requires `sudo` and manual setup.
- In-container rules are self-contained, so no host configuration is needed.
- The container runs with `NET_ADMIN` capability, which is the minimum privilege needed.
- Rules are applied programmatically by `container.py` before the network switch, eliminating a race window and requiring no manual host setup.

**Trade-off:** `NET_ADMIN` capability is required, which some managed container services (AWS Fargate) don't support. Documented in SECURITY.md "Known Limitations" section.

## 8. Runner Pool with Priority Queue

**Decision:** The API server manages a fixed-size pool of concurrent runners with priority-based queue draining.

**Reasoning:**
- **Measured (pool-e2e test):** 3 tasks run simultaneously, 4th queues until a slot opens. Queue drains in priority order: high, normal, low.
- Without a pool, submitting many tasks would spawn unlimited containers, exhausting Docker resources.
- Priority queuing lets urgent tasks jump ahead of batch work.
- The pool size is configurable via `MAX_CONCURRENT` env var (default 3).

**Trade-off:** A fixed pool means some tasks wait. For teams needing higher concurrency, increase `MAX_CONCURRENT` (bounded by available Docker resources).

## 9. Why Python Over TypeScript

**Decision:** Rewrite the sandclaude engine in Python rather than continuing with the TypeScript implementation.

**Reasoning:**
- **SDK parity:** The Claude Agent SDK is available in both TypeScript and Python. Python is the more common choice for ML/AI infrastructure teams.
- **Target audience:** Platform engineers and ML teams are more likely to be Python-native. Self-hosting infrastructure appeals to teams that want to customize, and Python has a lower barrier for that audience.
- **FastAPI DX:** FastAPI provides automatic OpenAPI docs, Pydantic validation, and async-first design out of the box. Equivalent functionality in Express/Fastify requires more manual wiring.
- **asyncio.Semaphore:** Python's `asyncio.Semaphore` provides a clean, stdlib-native concurrency primitive for the runner pool. No external dependency needed.

**Trade-off:** Python is slower than Node.js for raw I/O throughput, but the bottleneck is Docker container operations and Claude API calls, not the API server itself.

## 10. Why asyncio.to_thread for docker-py

**Decision:** Use `asyncio.to_thread()` to wrap synchronous docker-py calls rather than adopting aiodocker.

**Reasoning:**
- **Fewer dependencies:** docker-py (the `docker` package) is the official Docker SDK for Python, well-maintained by Docker Inc. aiodocker is a community project with fewer maintainers.
- **Sufficient for MVP:** Docker operations (create container, start, exec, remove) are infrequent, at only a few calls per task. The overhead of `to_thread()` is negligible compared to the operations themselves.
- **API stability:** docker-py mirrors the Docker Engine API closely. aiodocker has its own abstraction layer that may diverge.

**Trade-off:** Each `to_thread()` call occupies a thread from the default executor pool. For 3 concurrent tasks, this means ~3-6 threads, well within default limits (min 5, typically 32+).

## 11. gosu Entrypoint for API Container

**Decision:** Use a gosu-based entrypoint script (`docker-entrypoint-api.sh`) instead of the Dockerfile `USER` directive.

**Reasoning:**
- **Bind-mount ownership problem:** When Docker creates `./data` on the host (or the host user owns it as root), the container's non-root app user (uid 1000) can't write to it. This causes `sqlite3.OperationalError: unable to open database file` on startup.
- **USER directive is too early:** `USER sandclaude` in the Dockerfile switches to non-root before the container can fix filesystem ownership.
- **Entrypoint pattern:** The container starts as root, checks if `/app/data` is owned by uid 1000, runs `chown -R` only if needed, then drops to the app user via `exec gosu sandclaude "$@"`. This is the same pattern used by official postgres, redis, and mysql Docker images.
- **Optimization:** The `stat -c '%u'` check avoids a slow recursive `chown` on every startup when ownership is already correct.

**Trade-off:** The container briefly runs as root during the entrypoint. The root phase only performs a conditional `chown` and then irrevocably drops privileges via `exec gosu`. gosu uses `execve()` directly (no shell re-invocation), so there is no window for the app code to run as root.

## 12. Why aiosqlite Over Raw sqlite3

**Decision:** Use aiosqlite for database access rather than raw sqlite3 with `to_thread()`.

**Reasoning:**
- **Non-blocking event loop:** aiosqlite wraps sqlite3 in a dedicated background thread with an async interface, keeping the FastAPI event loop responsive.
- **Context manager support:** `async with aiosqlite.connect()` provides clean connection lifecycle management.
- **WAL mode compatibility:** aiosqlite supports WAL mode, enabling concurrent readers with a single writer. This is important when multiple API requests read task status while a runner writes updates.
- **Minimal overhead:** aiosqlite is a thin wrapper (~300 lines) and adds negligible complexity over raw sqlite3.

**Trade-off:** Adds one dependency, but it's a well-established package (100M+ downloads) with no transitive dependencies beyond stdlib.

## 13. AI-Generated PR Summaries

**Decision:** Use Claude Haiku to generate a human-readable summary for PR descriptions.

**Reasoning:**
- PR bodies without context force reviewers to read raw diffs to understand intent.
- Claude Haiku is fast (~1-2s) and cheap (~$0.001 per summary), adding negligible latency to PR creation.
- The summary is placed between the task prompt quote and the metadata section, giving reviewers immediate context.
- Falls back gracefully to the template-only body if the API call fails (network error, auth issue, timeout).

**Trade-off:** Adds an API call during PR creation. If the Anthropic API is unreachable, the PR is still created — just without the AI summary.

## 14. Why uv Over pip/poetry

**Decision:** Use uv as the primary package manager and build tool, with pip as a fallback.

**Reasoning:**
- **Speed:** uv resolves and installs dependencies 10-100x faster than pip. For a project with ~15 dependencies, `uv sync` completes in <1 second vs 5-10 seconds for `pip install`.
- **Anthropic ecosystem alignment:** Anthropic uses uv internally and recommends it in their SDK documentation. Aligning with the ecosystem reduces friction for contributors.
- **Lockfile support:** `uv.lock` provides reproducible builds without the complexity of poetry's `poetry.lock` + `pyproject.toml` dual-file setup.
- **pip compatibility:** uv generates standard wheels and supports `pip install .` as a fallback, so users who prefer pip are not excluded.

**Trade-off:** uv is newer and less battle-tested than pip/poetry. However, it's backed by Astral (the ruff team) and is rapidly becoming the standard in the Python ecosystem.
