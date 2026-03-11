"""
sandclaude MCP Server - stdio transport.

Exposes tools to Claude Code for async task management:
  cloud_submit    - Submit a coding task for async execution
  cloud_status    - Check status of all tasks
  cloud_result    - Get diff and audit log for a completed task
  cloud_cancel    - Cancel a running task
  cloud_create_pr - Create a GitHub PR from a completed task
  cloud_delete    - Delete a completed/failed/cancelled task

Environment:
  sandclaude_URL   - API server URL (default: http://localhost:3271)
  sandclaude_TOKEN - Bearer token for auth
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_URL = os.environ.get("sandclaude_URL", "http://localhost:3271")
TOKEN = os.environ.get("sandclaude_TOKEN", "")

server = Server("sandclaude")


# ── API client ────────────────────────────────────────────────


async def _api(method: str, path: str, json_body: dict | None = None) -> dict | list:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        if method == "GET":
            resp = await client.get(path, headers=headers)
        elif method == "DELETE":
            resp = await client.delete(path, headers=headers)
        else:
            resp = await client.post(path, json=json_body or {}, headers=headers)

    if not resp.is_success:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"API {method} {path} failed ({resp.status_code}): {detail}")

    return resp.json()


# ── Tools ─────────────────────────────────────────────────────


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="cloud_submit",
            description=(
                "Submit a coding task to sandclaude for async execution "
                "in an isolated Docker container. Returns a task ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Git URL or '.' for current directory",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Task description",
                    },
                    "model": {
                        "type": "string",
                        "description": "claude-sonnet-4-5 (default) or claude-opus-4-6",
                    },
                    "branch": {"type": "string", "description": "Git branch"},
                    "max_turns": {
                        "type": "integer",
                        "description": "Max agentic turns (default: 50)",
                    },
                    "allowed_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Extra domains the agent can access for deps "
                            "(e.g. ['registry.npmjs.org', 'pypi.org'])"
                        ),
                    },
                },
                "required": ["repo", "prompt"],
            },
        ),
        Tool(
            name="cloud_status",
            description="Check status of all sandclaude tasks.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="cloud_result",
            description=("Get the full result of a completed task: diff, audit log, cost."),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="cloud_cancel",
            description="Cancel a running or queued task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="cloud_create_pr",
            description="Create a GitHub PR from a completed task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                    "title": {"type": "string", "description": "PR title (optional)"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="cloud_delete",
            description="Delete a completed, failed, or cancelled task and its output files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                },
                "required": ["task_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "cloud_submit":
            return await _handle_submit(arguments)
        elif name == "cloud_status":
            return await _handle_status()
        elif name == "cloud_result":
            return await _handle_result(arguments)
        elif name == "cloud_cancel":
            return await _handle_cancel(arguments)
        elif name == "cloud_create_pr":
            return await _handle_create_pr(arguments)
        elif name == "cloud_delete":
            return await _handle_delete(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]


# ── Handlers ──────────────────────────────────────────────────


async def _handle_submit(args: dict) -> list[TextContent]:
    # Inject host CWD when repo="." so the API server can construct bind mounts
    if args.get("repo") == ".":
        args["host_cwd"] = os.getcwd()

    result = await _api("POST", "/tasks", args)
    task = result if isinstance(result, dict) else {}
    text = "\n".join(
        [
            "Task submitted successfully.",
            "",
            f"  Task ID: {task.get('id', '?')}",
            f"  Status:  {task.get('status', '?')}",
            f"  Model:   {task.get('model', '?')}",
            f"  Repo:    {task.get('repo', '?')}",
            "",
            "Use cloud_status to check progress.",
        ]
    )
    return [TextContent(type="text", text=text)]


async def _handle_status() -> list[TextContent]:
    tasks = await _api("GET", "/tasks")
    if not isinstance(tasks, list) or len(tasks) == 0:
        return [TextContent(type="text", text="No tasks found.")]

    header = f"  {'ID':<18}  {'Status':<10}  {'Cost':<10}"
    sep = f"  {'─' * 18}  {'─' * 10}  {'─' * 10}"
    lines = [header, sep]
    for t in tasks:
        cost_val = t.get("total_cost_usd")
        cost = f"${cost_val:.4f}" if cost_val is not None else "-"
        lines.append(f"  {t['id']:<18}  {t['status']:<10}  {cost:<10}")

    return [TextContent(type="text", text=f"Tasks ({len(tasks)}):\n\n" + "\n".join(lines))]


async def _handle_result(args: dict) -> list[TextContent]:
    task_id = args["task_id"]
    result = await _api("GET", f"/tasks/{task_id}")
    if not isinstance(result, dict):
        return [TextContent(type="text", text="Unexpected response")]

    if result.get("status") not in ("completed", "failed"):
        return [
            TextContent(
                type="text",
                text=f"Task {task_id} is still {result.get('status')}. Wait for completion.",
            )
        ]

    parts = [f"Task: {task_id}", f"Status: {result['status']}"]
    if result.get("error"):
        parts.append(f"Error: {result['error']}")
    parts.append("")

    if result.get("diff"):
        parts.append("── Diff ──────────────────────────────────")
        parts.append(result["diff"])
        parts.append("")

    if result.get("audit"):
        a = result["audit"]
        parts.append("── Audit Trail ───────────────────────────")
        parts.append(f"  Files read:    {len(a.get('files_read', []))}")
        parts.append(f"  Files written: {len(a.get('files_written', []))}")
        parts.append(f"  Commands run:  {len(a.get('commands_executed', []))}")
        net = a.get("network_requests", [])
        blocked = [r for r in net if not r.get("allowed")]
        parts.append(f"  Network allowed: {len(net) - len(blocked)}")
        parts.append(f"  Network blocked: {len(blocked)}")
        tokens = a.get("tokens", {})
        parts.append(f"  Tokens: {tokens.get('input', 0)} in / {tokens.get('output', 0)} out")
        parts.append(f"  Cost: ${a.get('estimated_cost_usd', 0):.4f}")

    return [TextContent(type="text", text="\n".join(parts))]


async def _handle_cancel(args: dict) -> list[TextContent]:
    await _api("POST", f"/tasks/{args['task_id']}/cancel")
    return [TextContent(type="text", text=f"Task {args['task_id']} cancelled.")]


async def _handle_create_pr(args: dict) -> list[TextContent]:
    result = await _api(
        "POST",
        f"/tasks/{args['task_id']}/create-pr",
        {
            "title": args.get("title"),
        },
    )
    if isinstance(result, dict):
        return [
            TextContent(
                type="text",
                text="\n".join(
                    [
                        "PR created successfully.",
                        "",
                        f"  Branch: {result.get('branch', '?')}",
                        f"  URL:    {result.get('url', '?')}",
                        f"  Title:  {result.get('title', '?')}",
                    ]
                ),
            )
        ]
    return [TextContent(type="text", text=str(result))]


async def _handle_delete(args: dict) -> list[TextContent]:
    result = await _api("DELETE", f"/tasks/{args['task_id']}")
    if isinstance(result, dict) and result.get("deleted"):
        return [TextContent(type="text", text=f"Task {result['deleted']} deleted.")]
    return [TextContent(type="text", text=str(result))]


# ── Main ──────────────────────────────────────────────────────


def _is_local_url(url: str) -> bool:
    """Check if a URL points to localhost/loopback."""
    from urllib.parse import urlparse

    hostname = urlparse(url).hostname or ""
    return hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def main() -> None:
    if not TOKEN:
        print(
            "ERROR: sandclaude_TOKEN is required.\n"
            "Register with: claude mcp add --transport stdio sandclaude \\\n"
            f"  --env sandclaude_URL={API_URL} \\\n"
            "  --env sandclaude_TOKEN=<your-token> \\\n"
            "  -- python -m sandclaude.mcp_plugin",
            file=sys.stderr,
        )
        sys.exit(1)

    # Reject non-local plaintext HTTP — bearer token would be sent in cleartext
    if API_URL.startswith("http://") and not _is_local_url(API_URL):
        print(
            f"ERROR: sandclaude_URL is set to a non-local HTTP endpoint ({API_URL}).\n"
            "Bearer tokens would be sent in cleartext over the network.\n"
            "Use https:// for non-local servers, or http://localhost for local development.",
            file=sys.stderr,
        )
        sys.exit(1)

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options()
            await server.run(read_stream, write_stream, init_options)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
