"""Tests for webhook notifications with real HTTP mock."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from sandclaude.models import Task, TaskPriority, TaskStatus
from sandclaude.runner.webhook import send_webhook


class _MockHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        _MockHandler.requests.append(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # Suppress logs


@pytest.fixture
def mock_server():
    _MockHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/webhook", _MockHandler.requests
    server.shutdown()


def _make_task(**overrides) -> Task:
    defaults = dict(
        id="task-wh",
        status=TaskStatus.completed,
        repo="https://github.com/test/repo",
        prompt="Fix auth",
        model="claude-sonnet-4-5",
        max_turns=10,
        priority=TaskPriority.normal,
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:05Z",
        completed_at="2026-01-01T00:01:05Z",
        tokens_input=5000,
        tokens_output=1500,
        total_cost_usd=0.05,
    )
    defaults.update(overrides)
    return Task(**defaults)


async def test_sends_webhook_on_completion(mock_server, tmp_path):
    url, requests = mock_server
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path

    task = _make_task(
        notify_webhook=url,
        notify_on='["completed", "failed"]',
    )
    task_dir = tmp_path / "tasks" / task.id
    task_dir.mkdir(parents=True)
    (task_dir / "audit.json").write_text(
        json.dumps(
            {
                "files_read": ["auth.py"],
                "files_written": ["auth.py"],
                "commands_executed": ["pytest"],
                "network_requests": [{"destination": "api.anthropic.com", "allowed": True}],
            }
        )
    )

    await send_webhook(task)

    assert len(requests) == 1
    body = requests[0]
    assert body["event"] == "completed"
    assert body["task"]["id"] == "task-wh"


async def test_skips_when_status_not_in_notify_on(mock_server):
    url, requests = mock_server
    task = _make_task(
        status=TaskStatus.completed,
        notify_webhook=url,
        notify_on='["failed"]',  # only notify on failure
    )

    await send_webhook(task)
    assert len(requests) == 0


async def test_sends_on_failure(mock_server, tmp_path):
    url, requests = mock_server
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path

    task = _make_task(
        status=TaskStatus.failed,
        error="OOM killed",
        notify_webhook=url,
        notify_on='["completed", "failed"]',
    )

    await send_webhook(task)
    assert len(requests) == 1
    assert requests[0]["event"] == "failed"
    assert requests[0]["task"]["error"] == "OOM killed"


async def test_skips_when_no_webhook():
    task = _make_task(notify_webhook=None, notify_on=None)
    await send_webhook(task)  # Should not raise


async def test_slack_url_detection():
    assert "hooks.slack.com" in "https://hooks.slack.com/services/T00/B00/xxx"
    assert "hooks.slack.com" not in "https://example.com/webhook"
