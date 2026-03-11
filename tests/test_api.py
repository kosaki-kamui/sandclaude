"""Tests for API server endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import init_token
from sandclaude.db.store import init_db


@pytest.fixture(autouse=True)
async def _setup(tmp_path):
    import sandclaude.config as cfg
    import sandclaude.db.store as store

    cfg.settings.data_dir = tmp_path
    cfg.settings.anthropic_api_key = "test-key"
    cfg.settings.environment = "test"
    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()
    init_token()


@pytest.fixture
async def client():
    from sandclaude.auth import get_token

    token = get_token()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


async def test_health(client: AsyncClient):
    # Health check should not require auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_auth_required(client: AsyncClient):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/tasks")
    assert resp.status_code == 401


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_create_task(mock_submit, client: AsyncClient):
    resp = await client.post(
        "/tasks",
        json={
            "repo": "https://github.com/test/repo",
            "prompt": "Fix bugs",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"].startswith("task-")
    assert data["status"] == "queued"
    assert data["repo"] == "https://github.com/test/repo"
    mock_submit.assert_called_once()


async def test_create_task_validation(client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": "."})
    assert resp.status_code == 422  # Missing prompt


async def test_reject_relative_repo_path(client: AsyncClient):
    """Relative paths (not '.') should be rejected."""
    resp = await client.post("/tasks", json={"repo": "foo/bar", "prompt": "test"})
    assert resp.status_code == 400
    resp = await client.post("/tasks", json={"repo": "./subdir", "prompt": "test"})
    assert resp.status_code == 400


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_list_tasks(mock_submit, client: AsyncClient):
    await client.post("/tasks", json={"repo": ".", "prompt": "A"})
    await client.post("/tasks", json={"repo": ".", "prompt": "B"})

    resp = await client.get("/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 2


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_task(mock_submit, client: AsyncClient):
    create_resp = await client.post("/tasks", json={"repo": ".", "prompt": "Test"})
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == task_id


async def test_get_nonexistent_task(client: AsyncClient):
    resp = await client.get("/tasks/nonexistent")
    assert resp.status_code == 404


async def test_reject_invalid_allowed_domain(client: AsyncClient):
    resp = await client.post(
        "/tasks",
        json={
            "repo": ".",
            "prompt": "test",
            "allowed_domains": ["api.anthropic.com; touch /tmp/pwned"],
        },
    )
    assert resp.status_code == 422


async def test_reject_private_webhook_url(client: AsyncClient):
    resp = await client.post(
        "/tasks",
        json={
            "repo": ".",
            "prompt": "test",
            "notify": {
                "webhook": "https://127.0.0.1/hook",
                "on": ["completed"],
            },
        },
    )
    assert resp.status_code == 422


async def test_pool_stats(client: AsyncClient):
    resp = await client.get("/pool")
    assert resp.status_code == 200
    data = resp.json()
    assert "max_concurrent" in data
    assert "active" in data
    assert "queued" in data


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_delete_task(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "delete me"})
    task_id = resp.json()["id"]

    resp = await client.delete(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == task_id

    # Verify it's gone
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 404


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_delete_running_task_blocked(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "running"})
    task_id = resp.json()["id"]

    # Simulate task in running state
    from sandclaude.db import store as db
    from sandclaude.models import TaskStatus

    await db.update_task(task_id, status=TaskStatus.running)

    resp = await client.delete(f"/tasks/{task_id}")
    assert resp.status_code == 409


@patch("sandclaude.api.main.cancel_container", new_callable=AsyncMock, return_value=True)
@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_cancel_queued_task(mock_submit, mock_cancel, client: AsyncClient):
    """Cancel endpoint should succeed for a queued task."""
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "cancel me"})
    task_id = resp.json()["id"]

    resp = await client.post(f"/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_cancel_completed_task_rejected(mock_submit, client: AsyncClient):
    """Cancel endpoint should return 400 for an already-completed task."""
    from sandclaude.db import store as db_mod
    from sandclaude.models import TaskStatus

    resp = await client.post("/tasks", json={"repo": ".", "prompt": "done"})
    task_id = resp.json()["id"]
    await db_mod.update_task(task_id, status=TaskStatus.completed, completed_at="now")

    resp = await client.post(f"/tasks/{task_id}/cancel")
    assert resp.status_code == 400
    assert "Cannot cancel" in resp.json()["detail"]


@patch("sandclaude.api.main.cancel_container", new_callable=AsyncMock, return_value=False)
@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_cancel_race_returns_409(mock_submit, mock_cancel, client: AsyncClient):
    """If cancel loses a race (task completed concurrently), return 409 not 500."""
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "race"})
    task_id = resp.json()["id"]

    resp = await client.post(f"/tasks/{task_id}/cancel")
    assert resp.status_code == 409
    assert "no longer cancellable" in resp.json()["detail"]


async def test_websocket_requires_auth():
    """WebSocket /tasks/{id}/stream requires bearer token via Authorization header."""
    from starlette.testclient import TestClient

    with TestClient(app) as tc:
        # No token - should be rejected
        with pytest.raises(Exception):
            with tc.websocket_connect("/tasks/test-id/stream"):
                pass

        # Query param token no longer accepted (removed for security)
        with pytest.raises(Exception):
            with tc.websocket_connect("/tasks/test-id/stream?token=wrong"):
                pass

        # Wrong token in header - should be rejected
        with pytest.raises(Exception):
            with tc.websocket_connect(
                "/tasks/test-id/stream",
                headers={"Authorization": "Bearer wrong-token"},
            ):
                pass


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_websocket_transcript_too_large_warning_then_done(mock_submit, client: AsyncClient):
    """WebSocket stream should emit a warning when transcript exceeds 10MB."""
    from starlette.testclient import TestClient

    import sandclaude.config as cfg
    from sandclaude.auth import get_token
    from sandclaude.db import store as db_mod
    from sandclaude.models import TaskStatus

    resp = await client.post("/tasks", json={"repo": ".", "prompt": "stream test"})
    task_id = resp.json()["id"]

    task_dir = cfg.settings.data_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    # File content is not parsed in the >10MB branch; size alone triggers warning.
    (task_dir / "transcript.json").write_bytes(b"x" * 10_000_001)

    await db_mod.update_task(task_id, status=TaskStatus.completed, completed_at="now")

    with TestClient(app) as tc:
        with tc.websocket_connect(
            f"/tasks/{task_id}/stream",
            headers={"Authorization": f"Bearer {get_token()}"},
        ) as ws:
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()

    assert msg1["type"] == "warning"
    assert "too large" in msg1["message"].lower()
    assert msg2["type"] == "done"
    assert msg2["status"] == "completed"


# ── Artifact endpoint tests ────────────────────────────────────


def _write_task_artifacts(data_dir, task_id):
    """Write sample artifact files for a task."""
    task_dir = data_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "diff.patch").write_text("diff --git a/f.py b/f.py\n+fixed\n")
    (task_dir / "audit.json").write_text(
        json.dumps({"task_id": task_id, "files_written": ["f.py"]})
    )
    (task_dir / "result.json").write_text(json.dumps({"success": True, "num_turns": 3}))
    (task_dir / "transcript.json").write_text(
        json.dumps([{"type": "assistant", "content": "done"}])
    )


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_diff(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "Fix it"})
    task_id = resp.json()["id"]

    import sandclaude.config as cfg

    _write_task_artifacts(cfg.settings.data_dir, task_id)

    resp = await client.get(f"/tasks/{task_id}/diff")
    assert resp.status_code == 200
    assert "diff --git" in resp.text


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_diff_rejects_oversized_artifact(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "Fix it"})
    task_id = resp.json()["id"]

    import sandclaude.config as cfg

    task_dir = cfg.settings.data_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    # Exceed 20MB read cap enforced by API artifact endpoints.
    (task_dir / "diff.patch").write_bytes(b"x" * 20_000_001)

    resp = await client.get(f"/tasks/{task_id}/diff")
    assert resp.status_code == 413


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_audit(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "Fix it"})
    task_id = resp.json()["id"]

    import sandclaude.config as cfg

    _write_task_artifacts(cfg.settings.data_dir, task_id)

    resp = await client.get(f"/tasks/{task_id}/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == task_id
    assert "files_written" in data


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_audit_rejects_malformed_json(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "Fix it"})
    task_id = resp.json()["id"]

    import sandclaude.config as cfg

    task_dir = cfg.settings.data_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "audit.json").write_text("{bad-json")

    resp = await client.get(f"/tasks/{task_id}/audit")
    assert resp.status_code == 502


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_result(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "Fix it"})
    task_id = resp.json()["id"]

    import sandclaude.config as cfg

    _write_task_artifacts(cfg.settings.data_dir, task_id)

    resp = await client.get(f"/tasks/{task_id}/result")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["num_turns"] == 3


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_transcript(mock_submit, client: AsyncClient):
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "Fix it"})
    task_id = resp.json()["id"]

    import sandclaude.config as cfg

    _write_task_artifacts(cfg.settings.data_dir, task_id)

    resp = await client.get(f"/tasks/{task_id}/transcript")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["type"] == "assistant"


async def test_get_diff_not_found(client: AsyncClient):
    resp = await client.get("/tasks/nonexistent/diff")
    assert resp.status_code == 404


async def test_get_audit_not_found(client: AsyncClient):
    resp = await client.get("/tasks/nonexistent/audit")
    assert resp.status_code == 404


async def test_get_result_not_found(client: AsyncClient):
    resp = await client.get("/tasks/nonexistent/result")
    assert resp.status_code == 404


async def test_get_transcript_not_found(client: AsyncClient):
    resp = await client.get("/tasks/nonexistent/transcript")
    assert resp.status_code == 404


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_get_artifacts_before_completion(mock_submit, client: AsyncClient):
    """Artifact endpoints return 404 if task exists but files don't yet."""
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "Fix it"})
    task_id = resp.json()["id"]

    for endpoint in ["diff", "audit", "result", "transcript"]:
        resp = await client.get(f"/tasks/{task_id}/{endpoint}")
        assert resp.status_code == 404


async def test_reject_http_repo(client: AsyncClient):
    """Plaintext http:// Git URLs should be rejected."""
    resp = await client.post(
        "/tasks", json={"repo": "http://github.com/user/repo", "prompt": "test"}
    )
    assert resp.status_code == 400
    assert "http://" in resp.json()["detail"]


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_rate_limit_enforcement(mock_submit, client: AsyncClient):
    """Rate limiter should return 429 after exceeding the window."""
    from sandclaude.api.main import CREATE_RATE_LIMIT_MAX_REQUESTS, _create_rate_buckets

    _create_rate_buckets.clear()

    for _ in range(CREATE_RATE_LIMIT_MAX_REQUESTS):
        resp = await client.post("/tasks", json={"repo": ".", "prompt": "rate limit test"})
        assert resp.status_code == 201

    # Next request should be rate-limited
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "should be blocked"})
    assert resp.status_code == 429
    _create_rate_buckets.clear()


async def test_null_owner_hash_blocks_secondary_token(tmp_path):
    """Tasks with NULL owner_token_hash should only be accessible by the primary token."""
    import sandclaude.config as cfg
    from sandclaude.auth import get_token, init_token
    from sandclaude.db import store as db_store
    from sandclaude.models import TaskPriority

    cfg.settings.data_dir = tmp_path
    cfg.settings.auth_tokens = "secondary-token-abc"
    init_token()
    primary_token = get_token()

    # Create a task with NULL owner_token_hash (simulating a legacy/migrated row)
    await db_store.create_task(
        task_id="legacy-task",
        repo=".",
        prompt="legacy",
        model="m",
        max_turns=5,
        priority=TaskPriority.normal,
        owner_token_hash=None,  # Legacy row
    )

    transport = ASGITransport(app=app)

    # Primary token should be able to access it
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {primary_token}"
        resp = await c.get("/tasks/legacy-task")
        assert resp.status_code == 200

    # Secondary token should be blocked (returns 404 to prevent ID enumeration)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = "Bearer secondary-token-abc"
        resp = await c.get("/tasks/legacy-task")
        assert resp.status_code == 404

    # Primary token should see legacy task in GET /tasks listing
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {primary_token}"
        resp = await c.get("/tasks")
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()]
        assert "legacy-task" in ids

    # Secondary token should NOT see legacy task in listing
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = "Bearer secondary-token-abc"
        resp = await c.get("/tasks")
        assert resp.status_code == 200
        ids = [t["id"] for t in resp.json()]
        assert "legacy-task" not in ids

    # Reset
    cfg.settings.auth_tokens = ""


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_cross_owner_denial(mock_submit, tmp_path):
    """Token A cannot access Token B's tasks."""
    import sandclaude.config as cfg
    from sandclaude.auth import init_token

    cfg.settings.data_dir = tmp_path
    cfg.settings.auth_tokens = "token-alpha,token-beta"
    cfg.settings.environment = "test"

    import sandclaude.db.store as store

    store.DB_PATH = tmp_path / "tasks.db"
    from sandclaude.db.store import init_db

    await init_db()
    init_token()

    transport = ASGITransport(app=app)

    # Token Alpha creates a task
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = "Bearer token-alpha"
        resp = await c.post("/tasks", json={"repo": ".", "prompt": "alpha's task"})
        assert resp.status_code == 201
        task_id = resp.json()["id"]

    # Token Beta cannot access it (returns 404 to prevent ID enumeration)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = "Bearer token-beta"
        resp = await c.get(f"/tasks/{task_id}")
        assert resp.status_code == 404

    # Token Alpha can access it
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = "Bearer token-alpha"
        resp = await c.get(f"/tasks/{task_id}")
        assert resp.status_code == 200

    cfg.settings.auth_tokens = ""


async def test_multi_token_auth_acceptance(tmp_path):
    """Secondary tokens from AUTH_TOKENS should be accepted for authentication."""
    import sandclaude.config as cfg
    from sandclaude.auth import init_token

    cfg.settings.data_dir = tmp_path
    cfg.settings.auth_tokens = "valid-secondary-token"
    cfg.settings.environment = "test"

    import sandclaude.db.store as store

    store.DB_PATH = tmp_path / "tasks.db"
    from sandclaude.db.store import init_db

    await init_db()
    init_token()

    transport = ASGITransport(app=app)

    # Secondary token should authenticate successfully
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = "Bearer valid-secondary-token"
        resp = await c.get("/tasks")
        assert resp.status_code == 200

    # Invalid token should fail
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["Authorization"] = "Bearer not-a-valid-token"
        resp = await c.get("/tasks")
        assert resp.status_code == 401

    cfg.settings.auth_tokens = ""


# ── Create-PR endpoint tests ──────────────────────────────


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_create_pr_requires_completed_status(mock_submit, client: AsyncClient):
    """create-pr should reject tasks that aren't completed."""
    resp = await client.post("/tasks", json={"repo": ".", "prompt": "pr test"})
    task_id = resp.json()["id"]

    resp = await client.post(f"/tasks/{task_id}/create-pr")
    assert resp.status_code == 400
    assert "Cannot create PR" in resp.json()["detail"]


@patch("sandclaude.api.main.create_pr", new_callable=AsyncMock)
@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_create_pr_success(mock_submit, mock_create_pr, client: AsyncClient):
    """create-pr should succeed for completed tasks."""
    from sandclaude.db import store as db_mod
    from sandclaude.models import TaskStatus

    mock_create_pr.return_value = {
        "branch": "sandclaude/task-123",
        "url": "https://github.com/test/repo/pull/1",
        "title": "sandclaude: fix bugs",
    }

    resp = await client.post("/tasks", json={"repo": ".", "prompt": "pr test"})
    task_id = resp.json()["id"]
    await db_mod.update_task(task_id, status=TaskStatus.completed, completed_at="now")

    resp = await client.post(f"/tasks/{task_id}/create-pr", json={"title": "My PR"})
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://github.com/test/repo/pull/1"


@patch("sandclaude.api.main.create_pr", new_callable=AsyncMock)
@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_create_pr_error_sanitized(mock_submit, mock_create_pr, client: AsyncClient):
    """create-pr errors should have paths sanitized."""
    from sandclaude.db import store as db_mod
    from sandclaude.models import TaskStatus

    mock_create_pr.side_effect = RuntimeError("Failed at /home/user/secret/repo/.git")

    resp = await client.post("/tasks", json={"repo": ".", "prompt": "pr test"})
    task_id = resp.json()["id"]
    await db_mod.update_task(task_id, status=TaskStatus.completed, completed_at="now")

    resp = await client.post(f"/tasks/{task_id}/create-pr")
    assert resp.status_code == 500
    assert "/home/user/secret" not in resp.json()["detail"]


@patch("sandclaude.api.main.submit_task", new_callable=AsyncMock)
async def test_task_error_sanitized_in_response(mock_submit, client: AsyncClient):
    """Task error field should have paths stripped in API responses."""
    from sandclaude.db import store as db_mod
    from sandclaude.models import TaskStatus

    resp = await client.post("/tasks", json={"repo": ".", "prompt": "err test"})
    task_id = resp.json()["id"]
    await db_mod.update_task(
        task_id,
        status=TaskStatus.failed,
        completed_at="now",
        error="Container exited at /home/user/.secret/repo with code 1",
    )

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    error = resp.json()["error"]
    assert "/home/user/.secret" not in error
    assert "<path>" in error
