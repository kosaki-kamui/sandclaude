"""Tests for auth module."""

import pytest
from fastapi import HTTPException

from sandclaude.auth import get_token, init_token, verify_token


def test_generates_token(tmp_path):
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path

    token = init_token()
    assert len(token) > 20  # token_urlsafe(32) is ~43 chars
    assert (tmp_path / ".token").exists()


def test_loads_existing_token(tmp_path):
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path

    t1 = init_token()
    t2 = init_token()
    assert t1 == t2


def test_verify_correct_token(tmp_path):
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path

    token = init_token()
    verify_token(token)  # Should not raise


def test_verify_wrong_token(tmp_path):
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path

    init_token()
    with pytest.raises(HTTPException) as exc_info:
        verify_token("wrong-token")
    assert exc_info.value.status_code == 401


def test_get_token_after_init(tmp_path):
    import sandclaude.config as cfg

    cfg.settings.data_dir = tmp_path

    token = init_token()
    assert get_token() == token
