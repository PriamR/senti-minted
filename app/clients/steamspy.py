"""SteamSpy — player-assigned tags + owner/CCU estimates. Keyless.

Used for the *taxonomy* (tags as the grouping axis), not for hard numbers.
Be gentle: SteamSpy asks for <=1 req/sec for appdetails.
"""
from __future__ import annotations

from typing import Any

from .base import make_client

BASE = "https://steamspy.com/api.php"


async def get_app_details(appid: int) -> dict[str, Any]:
    async with make_client() as client:
        resp = await client.get(BASE, params={"request": "appdetails", "appid": appid})
        resp.raise_for_status()
        return resp.json()


async def get_tags(appid: int) -> dict[str, int]:
    """`{tag_name: vote_count}` for a single app."""
    data = await get_app_details(appid)
    return data.get("tags", {}) or {}
