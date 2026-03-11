.PHONY: setup demo status result clean dev dev-demo dev-status dev-result test lint

# ── Docker Compose mode ────────────────────────────────────────

setup:
	@cp -n .env.example .env 2>/dev/null || true
	@echo "Edit .env to add your ANTHROPIC_API_KEY, then run 'make demo'"

demo:
	@echo "Starting sandclaude..."
	docker compose up -d --build
	@echo "Waiting for services... (10s)"
	@sleep 10
	@echo "Submitting task..."
	@curl -s -X POST http://localhost:3271/tasks \
	  -H "Authorization: Bearer $$(cat ./data/.token)" \
	  -H "Content-Type: application/json" \
	  -d '{"repo":"$(shell pwd)/demo/demo-repo","prompt":"Find and fix all security vulnerabilities and bugs in server.py. There are 3 bugs: (1) eval() code injection, (2) missing input validation, (3) unhandled errors in async route. Fix each one and add tests.","max_turns":15}' \
	  | python3 -m json.tool
	@echo "\nTask submitted! Run 'make status' to check progress."

status:
	@curl -s -H "Authorization: Bearer $$(cat ./data/.token)" \
	  http://localhost:3271/tasks | python3 -m json.tool

result:
	@curl -s -H "Authorization: Bearer $$(cat ./data/.token)" \
	  http://localhost:3271/tasks/$(TASK_ID) | python3 -m json.tool

clean:
	docker compose down -v

# ── Local dev mode ─────────────────────────────────────────────

dev:
	ANTHROPIC_API_KEY=$${ANTHROPIC_API_KEY} uvicorn sandclaude.api.main:app --port 3271 --reload

dev-demo:
	@test -f ./data/.token || sandclaude init
	@curl -s -X POST http://localhost:3271/tasks \
	  -H "Authorization: Bearer $$(cat ./data/.token)" \
	  -H "Content-Type: application/json" \
	  -d '{"repo":"$(shell pwd)/demo/demo-repo","prompt":"Find and fix all security vulnerabilities and bugs in server.py. Fix each one and add tests.","max_turns":15}' \
	  | python3 -m json.tool

dev-status:
	@curl -s -H "Authorization: Bearer $$(cat ./data/.token)" \
	  http://localhost:3271/tasks | python3 -m json.tool

dev-result:
	@curl -s -H "Authorization: Bearer $$(cat ./data/.token)" \
	  http://localhost:3271/tasks/$(TASK_ID) | python3 -m json.tool

# ── Dev commands ───────────────────────────────────────────────

test:
	pytest

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/
