"""Reddit — timestamped public discussion (pre/post-launch sampling).

Raw async OAuth2 against Reddit's API (no PRAW dependency, keeps everything
async and key-free on the client side). Two supported modes:

  * username + password set  -> `password` grant (script app, user context)
  * otherwise                -> `client_credentials` grant (read-only app)

Credentials are still pending; once REDDIT_CLIENT_ID / _SECRET are filled in
`.env` this works with no code change.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .base import make_client
from ..config import settings

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE = "https://oauth.reddit.com"

_token: str | None = None
_token_expiry: float = 0.0


async def _get_token() -> str:
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 60:
        return _token

    auth = httpx.BasicAuth(settings.reddit_client_id, settings.reddit_client_secret)
    if settings.reddit_username and settings.reddit_password:
        data = {
            "grant_type": "password",
            "username": settings.reddit_username,
            "password": settings.reddit_password,
        }
    else:
        data = {"grant_type": "client_credentials"}

    async with make_client(headers={"User-Agent": settings.reddit_user_agent}) as client:
        resp = await client.post(TOKEN_URL, data=data, auth=auth)
        resp.raise_for_status()
        payload = resp.json()
    _token = payload["access_token"]
    _token_expiry = time.time() + payload.get("expires_in", 3600)
    return _token


async def search_subreddit(
    subreddit: str, query: str, *, sort: str = "relevance", limit: int = 25
) -> list[dict[str, Any]]:
    """Search one subreddit; returns the raw `data` blocks of each post."""
    token = await _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": settings.reddit_user_agent,
    }
    async with make_client(headers=headers) as client:
        resp = await client.get(
            f"{OAUTH_BASE}/r/{subreddit}/search",
            params={"q": query, "restrict_sr": 1, "sort": sort, "limit": limit},
        )
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
        return [c["data"] for c in children]
