# Contributing to sandclaude (Python)

## Development Setup

```bash
# Clone the repo
git clone https://github.com/kosaki-kamui/sandclaude.git
cd sandclaude

# Install dependencies (using uv)
uv sync

# Or with pip
pip install -e ".[dev]"

# Build the runner image (required for container-based task execution)
docker build -t sandclaude-runner -f Dockerfile.runner .

# Initialize (generates auth token)
sandclaude init

# Start the API server (requires ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=sk-ant-... uvicorn sandclaude.api.main:app --port 3271

# In another terminal, run tests
pytest
```

## Prerequisites

- **Python 3.10+**
- **Docker** (for container-based task execution)
- **Anthropic API key** (for running the agent)
- **GitHub CLI (`gh`)** (optional, for PR creation feature)

## Project Structure

```
src/sandclaude/
├── api/
│   └── main.py            # API server (FastAPI) - all REST endpoints
├── runner/
│   ├── executor.py        # Core engine - runs Claude Agent SDK headless
│   ├── container.py       # Docker container lifecycle + network isolation
│   ├── entrypoint.py      # Runs inside container (setup + agent phases)
│   ├── pool.py            # Runner pool - concurrency control + priority queue
│   └── webhook.py         # Webhook notifications (generic + Slack)
├── mcp_plugin/
│   ├── __main__.py        # MCP plugin entrypoint (python -m sandclaude.mcp_plugin)
│   └── server.py          # MCP server for Claude Code plugin
├── db/
│   └── store.py           # aiosqlite persistence
├── auth.py                # Bearer token authentication
├── cli.py                 # CLI entrypoint (sandclaude init)
├── config.py              # Configuration from environment variables
├── github.py              # GitHub PR creation via gh CLI + AI-generated summaries
└── models.py              # Pydantic models and shared types

docker-entrypoint-api.sh   # API container entrypoint (gosu privilege drop)
tests/                     # pytest test suite
demo/                      # Demo app for presentations
```

## Running Tests

```bash
# Run all tests (excludes Docker tests by default)
pytest

# Run specific test file
pytest tests/test_pool.py

# Run Docker-dependent tests (requires Docker daemon + runner image)
docker build -t sandclaude-runner -f Dockerfile.runner .
pytest tests/ -k docker
```

## Code Style

- Python 3.10+ with type hints throughout
- Linting and formatting with ruff
- Pydantic for data validation
- No unnecessary abstractions - keep it simple

```bash
# Lint
ruff check src/ tests/

# Format
ruff format src/ tests/
```

## Security-Critical Code

The following files contain security-critical logic. Changes require careful review:

- **`src/sandclaude/runner/container.py`** - Network isolation (iptables rules), container lifecycle
- **`src/sandclaude/auth.py`** - Token generation and validation
- **`src/sandclaude/runner/entrypoint.py`** - Sandbox boundary (setup vs agent phases)

Look for `SECURITY-CRITICAL` comments in these files.

## Submitting Changes

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run tests (`pytest`) and lint (`ruff check src/ tests/`)
5. Commit with a clear message
6. Open a PR against `main`

## Reporting Issues

- **Security vulnerabilities**: See [SECURITY.md](SECURITY.md)
- **Bugs and feature requests**: Open a GitHub issue
- **Community behavior expectations**: See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
