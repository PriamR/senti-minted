"""RAWG — game metadata enrichment + Steam appid extraction. Key as query param."""
from __future__ import annotations

import re
from typing import Any

from .. import cache
from .base import make_client
from ..config import settings

BASE = "https://api.rawg.io/api"
_STEAM_APP_RE = re.compile(r"store\.steampowered\.com/app/(\d+)")


@cache.cached_call("rawg")
async def search(name: str, *, page_size: int = 5) -> list[dict[str, Any]]:
    async with make_client() as client:
        resp = await client.get(
            f"{BASE}/games",
            params={"key": settings.rawg_api_key, "search": name, "page_size": page_size},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])


@cache.cached_call("rawg")
async def get_game(rawg_id: int) -> dict[str, Any]:
    async with make_client() as client:
        resp = await client.get(f"{BASE}/games/{rawg_id}", params={"key": settings.rawg_api_key})
        resp.raise_for_status()
        return resp.json()


@cache.cached_call("rawg")
async def get_stores(rawg_id: int) -> list[dict[str, Any]]:
    async with make_client() as client:
        resp = await client.get(
            f"{BASE}/games/{rawg_id}/stores", params={"key": settings.rawg_api_key}
        )
        resp.raise_for_status()
        return resp.json().get("results", [])


def extract_steam_appid(stores: list[dict[str, Any]]) -> int | None:
    """Pull the Steam appid out of a RAWG stores list (store URLs)."""
    for entry in stores:
        m = _STEAM_APP_RE.search(entry.get("url", ""))
        if m:
            return int(m.group(1))
    return None
