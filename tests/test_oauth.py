"""API-level integration tests for v0.3.0 OAuth routes and session cookies."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sandclaude.api.main import app
from sandclaude.auth import create_session_cookie, init_token
from sandclaude.db import store as db
from sandclaude.db.store import init_db


@pytest.fixture(autouse=True)
async def _setup(tmp_path):
    import sandclaude.config as cfg
    import sandclaude.db.store as store

    cfg.settings.data_dir = tmp_path
    cfg.settings.anthropic_api_key = "test-key"
    cfg.settings.environment = "test"
    # Reset OAuth config to disabled by default
    cfg.settings.github_client_id = ""
    cfg.settings.github_client_secret = ""
    store.DB_PATH = tmp_path / "tasks.db"
    await init_db()
    init_token()

    # Create bootstrap admin user (lifespan is bypassed in tests)
    await db.create_user(
        username="admin",
        display_name="Admin",
        email=None,
        github_username=None,
    )


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def oauth_client(tmp_path):
    """Client with GitHub OAuth configured."""
    import sandclaude.config as cfg

    cfg.settings.github_client_id = "test-id"
    cfg.settings.github_client_secret = "test-secret"
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /auth/github — redirect to GitHub
# ---------------------------------------------------------------------------


class TestGitHubLogin:
    async def test_github_redirect_when_configured(self, oauth_client: AsyncClient):
        resp = await oauth_client.get("/auth/github")
        assert resp.status_code == 307
        location = resp.headers["location"]
        assert "github.com/login/oauth/authorize" in location
        assert "client_id=test-id" in location

    async def test_github_returns_501_when_not_configured(self, client: AsyncClient):
        resp = await client.get("/auth/github", follow_redirects=False)
        assert resp.status_code == 501
        assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /auth/github/callback — exchange code for session
# ---------------------------------------------------------------------------


def _mock_github_api(github_username: str = "octocat"):
    """Build mock for httpx.AsyncClient that simulates GitHub token + user APIs."""

    async def _mock_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"access_token": "gho_fake_token"}
        return resp

    async def _mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"login": github_username, "id": 12345}
        return resp

    mock_client = AsyncMock()
    mock_client.post = _mock_post
    mock_client.get = _mock_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestGitHubCallback:
    async def test_callback_sets_cookie_for_known_user(self, oauth_client: AsyncClient):
        """Valid code + known GitHub user results in session cookie."""
        # Create a user linked to the GitHub account
        await db.create_user(
            username="octocat",
            display_name="Octo Cat",
            github_username="octocat",
        )

        mock_client = _mock_github_api("octocat")
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await oauth_client.get("/auth/github/callback", params={"code": "valid-code"})

        assert resp.status_code == 302
        # Session cookie should be set
        cookies = resp.cookies
        assert "sandclaude_session" in cookies

    async def test_callback_unknown_github_user_403(self, oauth_client: AsyncClient):
        """Valid code but no sandclaude user linked to that GitHub user."""
        mock_client = _mock_github_api("unknown-gh-user")
        with patch("httpx.AsyncClient", return_value=mock_client):
            resp = await oauth_client.get("/auth/github/callback", params={"code": "valid-code"})

        assert resp.status_code == 403
        assert "no sandclaude user" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /auth/me — session info
# ---------------------------------------------------------------------------


class TestAuthMe:
    async def test_auth_me_with_valid_cookie(self, client: AsyncClient):
        admin = await db.get_user_by_username("admin")
        cookie = create_session_cookie(admin.id, admin.username)
        resp = await client.get(
            "/auth/me",
            cookies={"sandclaude_session": cookie},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == admin.id
        assert data["username"] == "admin"

    async def test_auth_me_without_cookie_401(self, client: AsyncClient):
        resp = await client.get("/auth/me")
        assert resp.status_code == 401
        assert "not logged in" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /auth/logout — clear cookie
# ---------------------------------------------------------------------------


class TestLogout:
    async def test_logout_clears_cookie(self, client: AsyncClient):
        admin = await db.get_user_by_username("admin")
        cookie = create_session_cookie(admin.id, admin.username)
        resp = await client.post(
            "/auth/logout",
            cookies={"sandclaude_session": cookie},
        )
        assert resp.status_code == 200
        assert resp.json()["logged_out"] is True
        # The response should instruct the browser to delete the cookie
        set_cookie = resp.headers.get("set-cookie", "")
        assert "sandclaude_session" in set_cookie


# ---------------------------------------------------------------------------
# Session cookie as auth fallback in _require_auth
# ---------------------------------------------------------------------------


class TestSessionCookieFallback:
    async def test_session_cookie_works_for_protected_endpoint(self, client: AsyncClient):
        """Session cookie should work as auth for a protected endpoint (no Bearer token)."""
        admin = await db.get_user_by_username("admin")
        cookie = create_session_cookie(admin.id, admin.username)

        # Call a protected endpoint using only the session cookie (no Authorization header)
        resp = await client.get(
            "/tasks",
            cookies={"sandclaude_session": cookie},
            headers={"Authorization": ""},  # clear the default auth
        )
        # Session cookies have tasks:read scope, so this should succeed
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Expired / tampered cookies
# ---------------------------------------------------------------------------


class TestInvalidCookies:
    async def test_expired_session_cookie_401(self, client: AsyncClient):
        """An expired session cookie should be rejected."""
        admin = await db.get_user_by_username("admin")
        # Create a cookie that expired 1 second ago
        cookie = create_session_cookie(admin.id, admin.username, max_age_s=-1)

        resp = await client.get(
            "/auth/me",
            cookies={"sandclaude_session": cookie},
        )
        assert resp.status_code == 401

    async def test_tampered_session_cookie_401(self, client: AsyncClient):
        """A cookie with a modified payload should be rejected."""
        admin = await db.get_user_by_username("admin")
        cookie = create_session_cookie(admin.id, admin.username)

        # Tamper with the payload (change user_id)
        parts = cookie.rsplit(".", 1)
        assert len(parts) == 2
        tampered_payload = parts[0].replace(f'"user_id":{admin.id}', '"user_id":99999')
        tampered_cookie = f"{tampered_payload}.{parts[1]}"

        resp = await client.get(
            "/auth/me",
            cookies={"sandclaude_session": tampered_cookie},
        )
        assert resp.status_code == 401
