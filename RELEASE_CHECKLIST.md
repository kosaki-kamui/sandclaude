# Release Checklist

Use this checklist before tagging a new release.

## Code Quality

- [ ] `python3 -m pytest tests/ -x -q` — all tests pass
- [ ] `python3 -m ruff check src/ tests/` — no lint errors
- [ ] `python3 -m ruff format --check src/ tests/` — formatting clean
- [ ] `python3 -m mypy src/sandclaude/ --ignore-missing-imports` — no type errors

## Version Consistency

- [ ] `pyproject.toml` version matches release tag
- [ ] `GET /health` returns the correct version (check `src/sandclaude/api/system.py`)
- [ ] `CHANGELOG.md` has an entry for this version with correct date
- [ ] CHANGELOG links at bottom include the new version

## Documentation Accuracy

- [ ] `README.md` feature list matches implementation
- [ ] `README.md` security claims are accurate (not overstated)
- [ ] `README.md` limitations section is current
- [ ] `docs/GETTING_STARTED.md` instructions work end-to-end
- [ ] `docs/QUICKSTART_STARTUPS.md` examples are correct
- [ ] API table in README matches actual endpoints
- [ ] Configuration table in README includes all env vars

## Packaging

- [ ] `pip install .` succeeds
- [ ] HTML templates included in package (`templates/approve.html`)
- [ ] `python3 -c "from sandclaude.api.main import app; print(len(app.routes))"` succeeds
- [ ] MCP plugin starts: `sandclaude_URL=http://localhost:3271 sandclaude_TOKEN=x python3 -m sandclaude.mcp_plugin`

## Docker

- [ ] `docker build -t sandclaude-runner -f Dockerfile.runner .` succeeds
- [ ] `docker compose up -d --build` starts without errors
- [ ] `curl http://localhost:3271/health` returns correct version
- [ ] `GET /admin/doctor` reports no failures

## Security-Sensitive Checks

- [ ] No secrets in committed code (`.env`, tokens, API keys)
- [ ] `GIT_ASKPASS` uses `shlex.quote()` (no shell injection)
- [ ] Token comparison uses `secrets.compare_digest()` (constant-time)
- [ ] Task ownership returns 404 (not 403) on mismatch
- [ ] Network isolation iptables rules applied BEFORE network switch
- [ ] `SKIP_NETWORK_ISOLATION` blocked in production
