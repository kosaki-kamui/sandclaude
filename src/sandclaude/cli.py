"""
CLI entrypoint for sandclaude.

Commands:
  init  - Generate auth token and initialize data directory
"""

from __future__ import annotations

import sys

from sandclaude.auth import init_token
from sandclaude.config import settings


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None

    if command == "init":
        _init()
    else:
        print("Usage: sandclaude <command>\n")
        print("Commands:")
        print("  init    Generate auth token and initialize data directory")
        print("\nTo start the server:")
        print("  ANTHROPIC_API_KEY=sk-ant-... uvicorn sandclaude.api.main:app --port 3271")
        sys.exit(0 if command is None else 1)


def _init() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # Initialize DB synchronously for CLI
    import asyncio

    from sandclaude.db.store import init_db

    asyncio.run(init_db())

    init_token()

    print("sandclaude initialized successfully.\n")
    print(f"Data directory: {settings.data_dir.resolve()}")
    token_path = settings.data_dir / ".token"
    print(f"Bearer token written to: {token_path.resolve()}")
    print("\nTo start the server:")
    port = settings.port
    print(f"  ANTHROPIC_API_KEY=sk-ant-... uvicorn sandclaude.api.main:app --port {port}\n")
    print("To register the MCP plugin in Claude Code:")
    print("  claude mcp add --transport stdio sandclaude \\")
    print(f"    --env sandclaude_URL=http://localhost:{settings.port} \\")
    print(f"    --env sandclaude_TOKEN=$(cat {token_path}) \\")
    print("    -- python -m sandclaude.mcp_plugin")


if __name__ == "__main__":
    main()
