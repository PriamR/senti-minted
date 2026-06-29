"""Minimal product resolver: name → canonical Steam appid + launch date.

This is the v1 of the resolver described in PROJECT_OVERVIEW.md §6 — live
resolution with an in-process cache. The full SQLite entity/alias map and the
probabilistic Reddit/YouTube paths are still future work; this covers the
deterministic appid spine the agent needs today.

Resolution path:
  - numeric query        → treat as a Steam appid directly
  - otherwise            → RAWG search → first hit → Steam appid from its store URL
  - launch_date          → Steam review-histogram start_date (free, reliable proxy)
  - igdb_id              → best-effort reverse lookup via IGDB external_games
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..clients import igdb, rawg, steam

_cache: dict[str, dict[str, Any]] = {}


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


async def _igdb_id_for_appid(appid: int) -> int | None:
    try:
        rows = await igdb._query(
            "external_games", f'fields game; where uid = "{appid}"; limit 1;'
        )
        if rows:
            return rows[0].get("game")
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        return None
    return None


async def resolve_product(query: str) -> dict[str, Any]:
    """Resolve a free-text name (or appid) to a canonical product record."""
    key = query.strip().lower()
    if key in _cache:
        return _cache[key]

    appid: int | None = None
    canonical_name = query.strip()
    confidence = 0.0

    if key.isdigit():
        appid, confidence = int(key), 1.0
    else:
        results = await rawg.search(query, page_size=3)
        if results:
            hit = results[0]
            canonical_name = hit.get("name", canonical_name)
            stores = await rawg.get_stores(hit["id"])
            appid = rawg.extract_steam_appid(stores)
            confidence = 0.9 if appid else 0.3

    record: dict[str, Any] = {
        "query": query,
        "appid": appid,
        "canonical_name": canonical_name,
        "launch_date": None,
        "igdb_id": None,
        "confidence": confidence,
        "resolved": appid is not None,
    }
    if appid:
        hist = await steam.get_review_histogram(appid)
        record["launch_date"] = _iso(hist.get("start_date"))
        record["igdb_id"] = await _igdb_id_for_appid(appid)

    _cache[key] = record
    return record
