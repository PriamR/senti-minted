"""YouTube Data API v3 — optional discussion source (video search + comments).

Free quota 10,000 units/day. search.list costs 100 units; commentThreads.list
costs 1 unit. Key passed as a query param.
"""
from __future__ import annotations

from typing import Any

from .base import make_client
from ..config import settings

BASE = "https://www.googleapis.com/youtube/v3"


async def search_videos(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    async with make_client() as client:
        resp = await client.get(
            f"{BASE}/search",
            params={
                "key": settings.youtube_api_key,
                "q": query,
                "part": "snippet",
                "type": "video",
                "maxResults": max_results,
            },
        )
        resp.raise_for_status()
        return resp.json().get("items", [])


async def get_comment_threads(video_id: str, *, max_results: int = 20) -> list[dict[str, Any]]:
    async with make_client() as client:
        resp = await client.get(
            f"{BASE}/commentThreads",
            params={
                "key": settings.youtube_api_key,
                "videoId": video_id,
                "part": "snippet",
                "maxResults": max_results,
                "order": "relevance",
                "textFormat": "plainText",
            },
        )
        resp.raise_for_status()
        return resp.json().get("items", [])
