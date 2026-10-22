"""Draw a representative sample of review text from any window in a product's history.

Design (see TECHNICAL_BREAKDOWN.md §4):
  * Strata = days (or multi-day slices on long windows, at most `max_strata`).
    Steam's date-range query returns each stratum's exact review count on its
    first page, so the sample is allocated proportionally to real volume
    (largest-remainder rounding): a launch day with 8,000 reviews gets far more
    of the sample than a quiet Tuesday.
  * Eligibility: text of at least `min_chars`, and not edited more than
    `edit_grace_days` after the window closed. Steam serves a review's CURRENT
    text and vote; a launch review rewritten in 2018 tells you about 2018, not
    launch. On NMS launch day 26% of reviews were edited >30 days later, and the
    edited ones are 80% positive vs 60% for the rest.
  * Within a stratum Steam returns newest first, so the eligible pool is
    subsampled at random (seeded by appid + window, so reruns draw the same set).
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Any

from ..clients import steam

DAY = 86400


def make_strata(start_ts: int, end_ts: int, *, max_strata: int = 31) -> list[tuple[int, int]]:
    """Split [start, end] into day-aligned-length slices, at most `max_strata` of them."""
    if end_ts < start_ts:
        return []
    n_days = max(1, math.ceil((end_ts - start_ts + 1) / DAY))
    slice_days = math.ceil(n_days / max_strata)
    strata = []
    s = start_ts
    while s <= end_ts:
        e = min(s + slice_days * DAY - 1, end_ts)
        strata.append((s, e))
        s = e + 1
    return strata


def allocate(n: int, counts: list[int]) -> list[int]:
    """Largest-remainder proportional allocation of `n` across strata of size
    `counts`, never giving a stratum more than it has."""
    total = sum(counts)
    if total == 0 or n <= 0:
        return [0] * len(counts)
    n = min(n, total)
    raw = [n * c / total for c in counts]
    quotas = [min(int(r), c) for r, c in zip(raw, counts)]
    order = sorted(range(len(counts)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    i = 0
    while sum(quotas) < n and i < 10 * len(order) + n:
        k = order[i % len(order)]
        if quotas[k] < counts[k]:
            quotas[k] += 1
        i += 1
    return quotas


def is_eligible(review: dict[str, Any], *, window_end: int, min_chars: int, edit_grace_days: int) -> str | None:
    """None if the review can be sampled, else the reason it can't."""
    if len((review.get("text") or "").strip()) < min_chars:
        return "too_short"
    if review.get("updated", 0) > window_end + edit_grace_days * DAY:
        return "edited_later"
    return None


async def sample_window(
    appid: int,
    start_ts: int,
    end_ts: int,
    *,
    n: int = 200,
    language: str = "english",
    min_chars: int = 20,
    edit_grace_days: int = 7,
    max_strata: int = 31,
    max_pages_per_stratum: int = 6,
) -> dict[str, Any]:
    """Proportional stratified sample of up to `n` eligible reviews created in the window.

    Returns {"reviews": [...], "stats": {...}}; each review carries its `stratum`.
    """
    end_ts = min(end_ts, int(time.time()))
    strata = make_strata(start_ts, end_ts, max_strata=max_strata)
    if not strata:
        return {"reviews": [], "stats": {"requested": n, "drawn": 0, "strata": 0, "population_n": 0}}
    first_page = max(10, min(100, 2 * math.ceil(n / len(strata))))

    # Pass 1: one page per stratum gives the stratum's size plus a first batch.
    pages = await asyncio.gather(*(
        steam.get_reviews_page(appid, start_ts=s, end_ts=e, num_per_page=first_page, language=language)
        for s, e in strata
    ))
    counts = [(p.get("summary") or {}).get("n", 0) for p in pages]
    quotas = allocate(n, counts)

    excluded = {"too_short": 0, "edited_later": 0}
    fetched = 0

    async def fill(idx: int) -> list[dict[str, Any]]:
        """Pass 2: page further into strata whose first page didn't hold enough
        eligible reviews to meet their quota."""
        nonlocal fetched
        s, e = strata[idx]
        page = pages[idx]
        seen: set[str] = set()
        pool: list[dict[str, Any]] = []
        cursors = {"*"}
        pages_used = 1
        while True:
            for r in page.get("reviews", []):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                fetched += 1
                why = is_eligible(r, window_end=end_ts, min_chars=min_chars, edit_grace_days=edit_grace_days)
                if why:
                    excluded[why] += 1
                else:
                    pool.append({**r, "stratum": idx})
            cursor = page.get("cursor") or ""
            done = (len(pool) >= quotas[idx] or len(seen) >= counts[idx] or not cursor
                    or cursor in cursors or pages_used >= max_pages_per_stratum or not page.get("reviews"))
            if done:
                break
            cursors.add(cursor)
            page = await steam.get_reviews_page(appid, start_ts=s, end_ts=e, cursor=cursor,
                                                num_per_page=100, language=language)
            pages_used += 1
        return pool

    pools = await asyncio.gather(*(fill(idx) for idx in range(len(strata))))

    rng = random.Random(f"{appid}:{start_ts}:{end_ts}:{language}")
    drawn: list[dict[str, Any]] = []
    shortfall = 0
    for idx, pool in enumerate(pools):
        take = min(quotas[idx], len(pool))
        shortfall += quotas[idx] - take
        drawn.extend(rng.sample(pool, take) if len(pool) > take else pool)

    checked = fetched - excluded["too_short"]
    return {
        "reviews": drawn,
        "stats": {
            "requested": n,
            "drawn": len(drawn),
            "language": language,
            "strata": len(strata),
            "stratum_days": round((strata[0][1] - strata[0][0] + 1) / DAY, 1),
            "population_n": sum(counts),
            "fetched": fetched,
            "excluded_too_short": excluded["too_short"],
            "excluded_edited_later": excluded["edited_later"],
            "edited_later_share": round(excluded["edited_later"] / checked, 3) if checked else None,
            "shortfall": shortfall,
            "min_chars": min_chars,
            "edit_grace_days": edit_grace_days,
        },
    }
