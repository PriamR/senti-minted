"""Steam — appreviews (storefront) + ISteamNews (Web API).

Both endpoints used here are usable without a key. STEAM_WEB_API_KEY is wired
through for future authenticated endpoints (e.g. player stats) but not required.

`appreviews` takes an undocumented date range (start_date, end_date,
date_range_type=include; the store's own histogram uses it). With it, any window
in a product's history is one query away, and the first page's query_summary
gives exact up/down counts for the window. Verified live on 22 Oct 2026 against
No Man's Sky's August 2016 launch.
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any, AsyncIterator

import httpx

from .. import cache
from .base import make_client

STOREFRONT = "https://store.steampowered.com"
WEB_API = "https://api.steampowered.com"

_TRANSIENT = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError)
_RETRY_STATUS = {429, 500, 502, 503, 504}

# Storefront requests in flight at once, per event loop (Steam rate-limits bursts).
STOREFRONT_CONCURRENCY = 4
_sems: dict[int, asyncio.Semaphore] = {}


def _storefront_slot() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _sems:
        _sems[loop_id] = asyncio.Semaphore(STOREFRONT_CONCURRENCY)
    return _sems[loop_id]


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any], *, retries: int = 4) -> dict[str, Any]:
    """GET + JSON with backoff on transient network errors, 429s and 5xx."""
    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code in _RETRY_STATUS and attempt < retries - 1:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
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


def _slim(r: dict[str, Any]) -> dict[str, Any]:
    """The fields the pipeline uses. Author identity is deliberately dropped."""
    author = r.get("author") or {}
    return {
        "id": r.get("recommendationid", ""),
        "text": r.get("review", ""),
        "timestamp": r.get("timestamp_created", 0),
        "updated": r.get("timestamp_updated", 0) or r.get("timestamp_created", 0),
        "voted_up": r.get("voted_up", False),
        "language": r.get("language", ""),
        "playtime_at_review_min": author.get("playtime_at_review"),
        "received_for_free": r.get("received_for_free", False),
    }


async def get_reviews_page(
    appid: int,
    *,
    start_ts: int,
    end_ts: int,
    cursor: str = "*",
    num_per_page: int = 100,
    language: str = "all",
) -> dict[str, Any]:
    """One page of reviews CREATED in [start_ts, end_ts], newest first.

    Returns {reviews (slimmed), cursor, summary}. `summary` is the window's exact
    population count ({n, up, down}) and only comes back on the first page
    (cursor="*"); num_per_page=0 fetches just that. Cached: 7 days for closed
    windows, 6 hours for windows ending within the last two days.
    """
    params = {
        "json": 1, "filter": "recent", "language": language,
        "review_type": "all", "purchase_type": "all",
        "date_range_type": "include", "start_date": start_ts, "end_date": end_ts,
        "num_per_page": num_per_page, "cursor": cursor,
    }

    async def fetch() -> dict[str, Any]:
        async with _storefront_slot():
            async with make_client() as client:
                data = await _get_json(client, f"{STOREFRONT}/appreviews/{appid}", params)
        qs = data.get("query_summary") or {}
        summary = None
        if cursor == "*" and "total_reviews" in qs:
            summary = {"n": qs.get("total_reviews", 0), "up": qs.get("total_positive", 0),
                       "down": qs.get("total_negative", 0)}
        return {
            "reviews": [_slim(r) for r in data.get("reviews", [])],
            "cursor": data.get("cursor", ""),
            "summary": summary,
        }

    closed = end_ts < time.time() - 2 * 86400
    source = "steam_reviews" if closed else "steam_reviews_open"
    return await cache.cached(source, f"appreviews/{appid}", params, fetch)


async def get_window_summary(appid: int, start_ts: int, end_ts: int, *, language: str = "all") -> dict[str, Any]:
    """Exact population counts for reviews created in [start_ts, end_ts]:
    {n, up, down, approval}. One cheap request (no review bodies)."""
    if end_ts < start_ts:
        return {"n": 0, "up": 0, "down": 0, "approval": None}
    page = await get_reviews_page(appid, start_ts=start_ts, end_ts=end_ts, num_per_page=0, language=language)
    s = page.get("summary") or {"n": 0, "up": 0, "down": 0}
    return {**s, "approval": (s["up"] / s["n"]) if s["n"] else None}


async def iter_reviews(
    appid: int,
    *,
    max_pages: int = 60,
    language: str = "all",
    until_ts: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield reviews newest-first by walking the cursor (filter=recent).

    The original backward crawl, kept for whole-history walks. Windowed analysis
    uses `get_reviews_page` with a date range instead, which reaches any date
    directly. Stops at `max_pages`, an empty page, a repeated/empty cursor, or
    once a page's oldest review predates `until_ts`.
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
                yield _slim(r)
            if until_ts is not None and reviews[-1].get("timestamp_created", 0) < until_ts:
                return
            cursor = data.get("cursor", "")
            if not cursor or cursor in seen_cursors:
                return
            seen_cursors.add(cursor)
            await asyncio.sleep(0.3)  # be polite to Steam between pages


@cache.cached_call("steam_histogram")
async def get_review_histogram(
    appid: int, *, rollup: str = "month", language: str = "all"
) -> dict[str, Any]:
    """Steam's own up/down rollups across the full review history (one call).

    Returns the `results` block: `rollups` (monthly/weekly aggregates, each
    {date, recommendations_up, recommendations_down}), `recent` (daily), plus
    start/end dates. This is the free, full-coverage approval-through-time spine.

    Note: its monthly counts don't match a date-range query on the same month
    (NMS Aug 2016: 46,992 reviews at 47% here vs 49,422 at 56% by creation date).
    The gap fits reviews being binned by last edit: launch reviews edited later
    are mostly positive. The curve uses this endpoint; window stats use creation
    dates. See TECHNICAL_BREAKDOWN.md.
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


OFFICIAL_FEED = "steam_community_announcements"
_TAGS = re.compile(r"<[^>]+>|\[/?[^\]]+\]")


def _blurb(contents: str, limit: int) -> str:
    text = _TAGS.sub(" ", html.unescape(contents or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


@cache.cached_call("steam_news")
async def get_news(
    appid: int,
    *,
    count: int = 20,
    feeds: str | None = None,
    maxlength: int = 400,
) -> list[dict[str, Any]]:
    """News items (patch notes, announcements). Keyless v2 endpoint.

    `feeds=OFFICIAL_FEED` keeps only the developer's own Steam announcements.
    Without it the endpoint mixes in press coverage (for NMS, 37 of the latest
    50 items were PC Gamer, RPS, PCGamesN and similar). `contents` is cut to a
    plain-text blurb of `maxlength` characters.
    """
    params: dict[str, Any] = {"appid": appid, "count": count, "maxlength": maxlength}
    if feeds:
        params["feeds"] = feeds
    async with make_client() as client:
        resp = await client.get(f"{WEB_API}/ISteamNews/GetNewsForApp/v2/", params=params)
        resp.raise_for_status()
        items = resp.json().get("appnews", {}).get("newsitems", [])
    for it in items:
        it["contents"] = _blurb(it.get("contents", ""), maxlength)
    return items
