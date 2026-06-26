"""IsThereAnyDeal — pricing & deal data (v2 API).

Endpoints verified against the working Game Spending Website tool:
  - GET  /games/lookup/v1?key=&title=  (or &appid=)  -> {found, game:{id}}
  - POST /games/overview/v2?key=&country=US  body=[ "<uuid>", ... ]
        (Content-Type text/plain to avoid the JSON CORS preflight ITAD blocks)
"""
from __future__ import annotations

import json
from typing import Any

from .base import make_client
from ..config import settings

BASE = "https://api.isthereanydeal.com"


async def lookup(*, title: str | None = None, appid: int | None = None) -> str | None:
    """Resolve a game to ITAD's internal UUID by title or Steam appid."""
    params: dict[str, Any] = {"key": settings.itad_api_key}
    if appid is not None:
        params["appid"] = appid
    elif title:
        params["title"] = title
    else:
        raise ValueError("lookup requires title or appid")

    async with make_client() as client:
        resp = await client.get(f"{BASE}/games/lookup/v1", params=params)
        resp.raise_for_status()
        data = resp.json()
    if data.get("found") and data.get("game"):
        return data["game"]["id"]
    return None


async def overview(ids: list[str], *, country: str = "US") -> dict[str, dict[str, Any]]:
    """Current price + regular price + best shop, keyed by ITAD UUID."""
    if not ids:
        return {}
    async with make_client() as client:
        resp = await client.post(
            f"{BASE}/games/overview/v2",
            params={"key": settings.itad_api_key, "country": country},
            headers={"Content-Type": "text/plain"},
            content=json.dumps(ids),
        )
        resp.raise_for_status()
        data = resp.json()

    out: dict[str, dict[str, Any]] = {}
    for p in data.get("prices", []):
        cur = p.get("current")
        if cur:
            out[p["id"]] = {
                "amount": cur["price"]["amount"],
                "regular": (cur.get("regular") or cur["price"])["amount"],
                "shop": (cur.get("shop") or {}).get("name", "store"),
                "cut": cur.get("cut", 0),
            }
        else:
            out[p["id"]] = None
    return out


async def price_for_appid(appid: int, *, country: str = "US") -> dict[str, Any] | None:
    """Convenience: Steam appid -> current price summary."""
    uuid = await lookup(appid=appid)
    if not uuid:
        return None
    return (await overview([uuid], country=country)).get(uuid)
