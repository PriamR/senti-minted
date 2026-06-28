"""YouTube ingestion from non-official spaces (general name search).

`search_comments` answers the concrete question: take the first N videos that
surface for a game-name search, pull the top X comments from each, and normalize
them into pipeline items. These are *wild* (unverified) — every item carries
confidence=None until the classifier (Problem B in PLANNING.md) scores it.

Quota: search.list = 100 units, commentThreads.list = 1 unit/video. So one call
is ~100 + N units (10 videos -> ~110 of the 10,000/day budget).
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from ..clients import youtube


def _iso_to_ts(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return 0


async def search_comments(
    game: str,
    *,
    n_videos: int = 10,
    per_video: int = 20,
    order: str = "relevance",
) -> dict[str, Any]:
    """First `n_videos` for a `game` search → top `per_video` comments each.

    Returns {game, items, videos, dropped}. `items` are normalized pipeline rows;
    `videos` is per-video provenance (incl. comments-disabled failures). Topicality
    is NOT verified here — `confidence` is None pending the classifier.
    """
    videos = await youtube.search_videos(game, max_results=n_videos)
    items: list[dict[str, Any]] = []
    video_stats: list[dict[str, Any]] = []

    for v in videos:
        vid = v.get("id", {}).get("videoId")
        if not vid:
            continue
        snip = v.get("snippet", {})
        title = html.unescape(snip.get("title", ""))
        channel = html.unescape(snip.get("channelTitle", ""))
        try:
            threads = await youtube.get_comment_threads(vid, max_results=per_video, order=order)
        except Exception as e:  # noqa: BLE001 — comments often disabled (403); skip the video
            video_stats.append({"video_id": vid, "title": title, "channel": channel,
                                "comments": 0, "error": type(e).__name__})
            continue

        n = 0
        for t in threads:
            cs = t.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            text = cs.get("textDisplay", "")
            if not text:
                continue
            items.append({
                "text": text,
                "timestamp": _iso_to_ts(cs.get("publishedAt", "")),
                "source": "youtube",
                "confidence": None,  # set by the topicality classifier later
                "meta": {
                    "video_id": vid,
                    "video_title": title,
                    "channel": channel,
                    "likes": cs.get("likeCount", 0),
                    "author": cs.get("authorDisplayName", ""),
                },
            })
            n += 1
        video_stats.append({"video_id": vid, "title": title, "channel": channel, "comments": n})

    return {
        "game": game,
        "n_videos_returned": len(videos),
        "n_comments": len(items),
        "items": items,
        "videos": video_stats,
    }
