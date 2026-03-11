"""Shared test fixtures."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Override data_dir before importing any sandclaude modules
_tmp = tempfile.mkdtemp(prefix="sandclaude-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["ENVIRONMENT"] = "test"


@pytest.fixture(autouse=True)
def _reset_data_dir(tmp_path: Path):
    """Give each test a fresh data directory."""
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path
    cfg.settings.environment = "test"
    # Also update the DB path
    import sandclaude.db.store as store

    store.DB_PATH = tmp_path / "tasks.db"
    yield
