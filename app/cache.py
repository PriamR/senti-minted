"""Per-source TTL cache, backed by one SQLite file.

Every external read goes through `cached()` (or the `@cached_call` decorator), keyed
by (source, endpoint, params). Each source has its own TTL, because the data ages
at very different speeds: a price is stale in an hour, a closed review window from
2016 is effectively permanent. Hit/miss counts are kept per source so a run can
report its hit rate.

The cache stores JSON only. It is a speed and cost layer, never a source of truth:
if the file is missing or unreadable the call simply goes upstream.
"""
from __future__ import annotations

import functools
import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import settings

HOUR = 3600
DAY = 24 * HOUR

# Suggested in the 22 Oct handoff; review pages split by whether the window is closed.
TTL: dict[str, float] = {
    "steam_histogram": 24 * HOUR,
    "steam_news": 6 * HOUR,
    "steam_reviews": 7 * DAY,        # closed (historical) windows
    "steam_reviews_open": 6 * HOUR,  # windows that end in the last 2 days
    "rawg": 30 * DAY,
    "igdb": 30 * DAY,
    "itad": 1 * HOUR,
    "llm": 30 * DAY,                 # same model + same prompt -> same labels
}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None
_stats: dict[str, Counter] = {"hit": Counter(), "miss": Counter()}
_bypass: set[str] = set()


def _db() -> sqlite3.Connection:
    global _conn, _conn_path
    path = settings.cache_path
    if _conn is None or _conn_path != path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            " key TEXT PRIMARY KEY, source TEXT, value TEXT, created REAL, ttl REAL)"
        )
        _conn_path = path
    return _conn


def make_key(source: str, endpoint: str, params: Any) -> str:
    blob = json.dumps(params, sort_keys=True, default=str)
    return f"{source}:{endpoint}:{hashlib.sha256(blob.encode()).hexdigest()[:32]}"


def get(key: str, *, now: float | None = None) -> Any | None:
    """The stored value if present and younger than its TTL, else None."""
    now = time.time() if now is None else now
    with _lock:
        row = _db().execute("SELECT value, created, ttl FROM cache WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value, created, ttl = row
    if now - created > ttl:
        return None
    return json.loads(value)


def put(key: str, source: str, value: Any, ttl: float, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO cache (key, source, value, created, ttl) VALUES (?, ?, ?, ?, ?)",
            (key, source, json.dumps(value, default=str), now, ttl),
        )
        _db().commit()


def bypass(*sources: str) -> None:
    """Skip cache *reads* for these sources (writes still happen). Used to force
    fresh LLM calls when measuring cost."""
    _bypass.update(sources)


async def cached(
    source: str,
    endpoint: str,
    params: Any,
    fetch: Callable[[], Awaitable[Any]],
    *,
    ttl: float | None = None,
) -> Any:
    """Return the cached value for (source, endpoint, params) or fetch and store it."""
    if not settings.cache_enabled:
        return await fetch()
    key = make_key(source, endpoint, params)
    if source not in _bypass:
        try:
            hit = get(key)
        except sqlite3.Error:
            hit = None
        if hit is not None:
            _stats["hit"][source] += 1
            return hit
    _stats["miss"][source] += 1
    value = await fetch()
    try:
        put(key, source, value, ttl if ttl is not None else TTL[source])
    except sqlite3.Error:
        pass  # a broken cache must never break a run
    return value


def cached_call(source: str, *, ttl: float | None = None):
    """Decorator form for async client functions: the key is the function name plus
    its arguments, so call sites stay unchanged."""
    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            params = {"args": list(args), "kwargs": kwargs}
            return await cached(source, fn.__qualname__, params, lambda: fn(*args, **kwargs), ttl=ttl)
        return inner
    return wrap


def stats() -> dict[str, Any]:
    hits, misses = sum(_stats["hit"].values()), sum(_stats["miss"].values())
    total = hits + misses
    sources = sorted(set(_stats["hit"]) | set(_stats["miss"]))
    return {
        "enabled": settings.cache_enabled,
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / total, 3) if total else None,
        "by_source": {s: {"hit": _stats["hit"][s], "miss": _stats["miss"][s]} for s in sources},
    }


def reset_stats() -> None:
    _stats["hit"].clear()
    _stats["miss"].clear()
