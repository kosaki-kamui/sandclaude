"""GitHub OAuth routes for browser-based approval UI (v0.3.0)."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from sandclaude.auth import (
    create_session_cookie,
    verify_session_cookie,
)
from sandclaude.config import settings
from sandclaude.db import store as db

router = APIRouter()


def _oauth_enabled() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


@router.get("/auth/github")
async def github_login(request: Request, return_to: str = "/") -> RedirectResponse:
    """Redirect to GitHub OAuth authorization page."""
    if not _oauth_enabled():
        raise HTTPException(status_code=501, detail="GitHub OAuth is not configured")

    redirect_uri = f"{settings.api_url}/auth/github/callback"
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={settings.github_client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=read:user,user:email"
        f"&state={return_to}"
    )
    return RedirectResponse(url)


@router.get("/auth/github/callback")
async def github_callback(code: str = "", state: str = "/") -> RedirectResponse:
    """Exchange GitHub OAuth code for access token, then set session cookie."""
    if not _oauth_enabled():
        raise HTTPException(status_code=501, detail="GitHub OAuth is not configured")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code parameter")

    import httpx

    # Exchange code for access token
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="GitHub token exchange failed")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=400,
                detail=token_data.get("error_description", "OAuth failed"),
            )

        # Fetch GitHub user info
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch GitHub user")
        gh_user = user_resp.json()

    github_username = gh_user.get("login")
    if not github_username:
        raise HTTPException(status_code=502, detail="GitHub user has no login")

    # Match to sandclaude user by github_username
    user = await db.get_user_by_github_username(github_username)
    if not user:
        raise HTTPException(
            status_code=403,
            detail=f"No sandclaude user linked to GitHub user '{github_username}'. "
            "Ask an admin to create a user with this github_username.",
        )

    # Set session cookie and redirect
    cookie_value = create_session_cookie(user.id, user.username)
    response = RedirectResponse(url=state, status_code=302)
    response.set_cookie(
        key="sandclaude_session",
        value=cookie_value,
        httponly=True,
        samesite="lax",
        max_age=28800,  # 8 hours
        path="/",
    )
    return response


@router.get("/auth/me")
async def auth_me(
    sandclaude_session: str | None = Cookie(None),
) -> dict:
    """Return the currently logged-in user from session cookie."""
    if not sandclaude_session:
        raise HTTPException(status_code=401, detail="Not logged in")
    result = verify_session_cookie(sandclaude_session)
    if not result:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return {
        "user_id": result.user_id,
        "username": result.username,
    }


@router.post("/auth/logout")
async def auth_logout() -> JSONResponse:
    """Clear session cookie."""
    response = JSONResponse({"logged_out": True})
    response.delete_cookie("sandclaude_session", path="/")
    return response
