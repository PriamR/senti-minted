"""IGDB (via Twitch OAuth) — objective game milestones / release dates.

Two-step: client-credentials token from Twitch, then Bearer-auth IGDB calls.
The token is cached in-process until shortly before expiry.
"""
from __future__ import annotations

import time
from typing import Any

from .base import make_client
from ..config import settings

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_BASE = "https://api.igdb.com/v4"

_token: str | None = None
_token_expiry: float = 0.0


async def _get_token() -> str:
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 60:
        return _token
    async with make_client() as client:
        resp = await client.post(
            TWITCH_TOKEN_URL,
            params={
                "client_id": settings.twitch_client_id,
                "client_secret": settings.twitch_client_secret,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    _token = data["access_token"]
    _token_expiry = time.time() + data.get("expires_in", 3600)
    return _token


async def _query(endpoint: str, body: str) -> list[dict[str, Any]]:
    token = await _get_token()
    async with make_client() as client:
        resp = await client.post(
            f"{IGDB_BASE}/{endpoint}",
            headers={
                "Client-ID": settings.twitch_client_id,
                "Authorization": f"Bearer {token}",
            },
            content=body,
        )
        resp.raise_for_status()
        return resp.json()


async def search_game(name: str, *, limit: int = 5) -> list[dict[str, Any]]:
    body = (
        f'search "{name}"; '
        "fields name,first_release_date,status,summary,"
        "involved_companies.company.name,involved_companies.developer,"
        "involved_companies.publisher,"
        "release_dates.date,release_dates.human,release_dates.platform.name; "
        f"limit {limit};"
    )
    return await _query("games", body)
