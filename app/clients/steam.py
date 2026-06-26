"""Steam — appreviews (storefront) + ISteamNews (Web API).

Both endpoints used here are usable without a key. STEAM_WEB_API_KEY is wired
through for future authenticated endpoints (e.g. player stats) but not required.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import httpx

from .base import make_client

STOREFRONT = "https://store.steampowered.com"
WEB_API = "https://api.steampowered.com"

_TRANSIENT = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any], *, retries: int = 3) -> dict[str, Any]:
    """GET + JSON with simple backoff on transient network errors."""
    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except _TRANSIENT:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    return {}


async def get_reviews(
    appid: int,
    *,
    cursor: str = "*",
    num_per_page: int = 100,
    filter: str = "all",
    language: str = "all",
) -> dict[str, Any]:
    """One page of reviews + the query_summary aggregate. Keyless.

    `filter="recent"` sorts by creation date (newest first) and paginates back
    through history via the returned cursor — this is what `iter_reviews` uses.
    `filter="all"` sorts by helpfulness and is best for the summary only.
    """
    async with make_client() as client:
        resp = await client.get(
            f"{STOREFRONT}/appreviews/{appid}",
            params={
                "json": 1,
                "cursor": cursor,
                "num_per_page": num_per_page,
                "filter": filter,
                "language": language,
                "review_type": "all",
                "purchase_type": "all",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def iter_reviews(
    appid: int,
    *,
    max_pages: int = 60,
    language: str = "all",
    until_ts: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield reviews newest-first by walking the cursor (filter=recent).

    Each yielded dict keeps just the fields the pipeline needs:
    text, timestamp (UNIX), voted_up (bool), language. Stops at `max_pages`
    (safety cap), an empty page, a repeated/empty cursor (Steam's end-of-list
    signal), or — once `until_ts` is given — after the page whose oldest review
    predates `until_ts` (so callers crawl just deep enough to cover a date).
    """
    cursor = "*"
    seen_cursors: set[str] = set()
    async with make_client() as client:  # one connection reused across pages
        for page in range(max_pages):
            data = await _get_json(
                client,
                f"{STOREFRONT}/appreviews/{appid}",
                {
                    "json": 1, "cursor": cursor, "num_per_page": 100,
                    "filter": "recent", "language": language,
                    "review_type": "all", "purchase_type": "all",
                },
            )
            reviews = data.get("reviews", [])
            if not reviews:
                return
            for r in reviews:
                yield {
                    "text": r.get("review", ""),
                    "timestamp": r.get("timestamp_created", 0),
                    "voted_up": r.get("voted_up", False),
                    "language": r.get("language", ""),
                }
            if until_ts is not None and reviews[-1].get("timestamp_created", 0) < until_ts:
                return
            cursor = data.get("cursor", "")
            if not cursor or cursor in seen_cursors:
                return
            seen_cursors.add(cursor)
            await asyncio.sleep(0.3)  # be polite to Steam between pages


async def get_review_histogram(
    appid: int, *, rollup: str = "month", language: str = "all"
) -> dict[str, Any]:
    """Steam's own up/down rollups across the full review history (one call).

    Returns the `results` block: `rollups` (monthly/weekly aggregates, each
    {date, recommendations_up, recommendations_down}), `recent` (daily), plus
    start/end dates. This is the free, full-coverage approval-through-time spine.
    """
    params: dict[str, Any] = {"review_score_preference": 0}
    if language != "all":
        params["l"] = language  # Steam expects a language code (e.g. "english")
    async with make_client() as client:
        resp = await client.get(f"{STOREFRONT}/appreviewhistogram/{appid}", params=params)
        resp.raise_for_status()
        return resp.json().get("results", {})


async def get_review_summary(appid: int) -> dict[str, Any]:
    """Just the aggregate sentiment block (cheap: num_per_page=0)."""
    data = await get_reviews(appid, num_per_page=0)
    return data.get("query_summary", {})


async def get_news(appid: int, *, count: int = 20) -> list[dict[str, Any]]:
    """Official dev posts (patch notes, announcements). Keyless v2 endpoint."""
    async with make_client() as client:
        resp = await client.get(
            f"{WEB_API}/ISteamNews/GetNewsForApp/v2/",
            params={"appid": appid, "count": count},
        )
        resp.raise_for_status()
        return resp.json().get("appnews", {}).get("newsitems", [])
