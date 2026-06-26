"""Build a product's retrospective sentiment timeline.

Two layers (see PLANNING.md):
  1. Baseline curve  — Steam's review histogram, full history, free.
  2. Event windows   — for recent reachable Steam-News events, a ±N-day window of
                       sampled review text scored by the approval Measure (LLM),
                       giving each event a before/after delta + drivers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..clients import steam
from ..measures import Measure
from ..measures.approval import ApprovalMeasure

DAY = 86400
PATCH_KEYWORDS = (
    "update", "patch", "fix", "hotfix", "changelog",
    "version", "build", "maintenance", "expedition", "release",
)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


async def build_baseline(appid: int) -> dict[str, Any]:
    """Full-history approval curve from Steam's monthly rollups."""
    results = await steam.get_review_histogram(appid)
    points = []
    for r in results.get("rollups", []):
        up = r.get("recommendations_up", 0)
        down = r.get("recommendations_down", 0)
        total = up + down
        points.append({
            "date": _iso(r["date"]),
            "ts": r["date"],
            "up": up,
            "down": down,
            "n": total,
            "approval": (up / total) if total else None,
        })
    return {
        "rollup": results.get("rollup_type", "month"),
        "start_ts": results.get("start_date"),
        "end_ts": results.get("end_date"),
        "points": points,
    }


async def get_events(appid: int, *, launch_ts: int | None, count: int = 50) -> list[dict[str, Any]]:
    """Steam-News items (+ a synthetic Launch), newest first."""
    items = await steam.get_news(appid, count=count)
    events: list[dict[str, Any]] = []
    for it in items:
        title = it.get("title", "")
        is_patch = any(kw in title.lower() for kw in PATCH_KEYWORDS)
        events.append({
            "ts": it.get("date", 0),
            "date": _iso(it.get("date", 0)),
            "title": title,
            "url": it.get("url", ""),
            "type": "patch" if is_patch else "announcement",
        })
    if launch_ts:
        events.append({
            "ts": launch_ts, "date": _iso(launch_ts),
            "title": "Launch", "url": "", "type": "launch",
        })
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events


_TYPE_RANK = {"launch": 3, "patch": 2, "announcement": 1}


def cluster_events(events: list[dict[str, Any]], *, window_days: int) -> list[dict[str, Any]]:
    """Collapse events that fall within one analysis window into one anchor.

    Steam often gets several posts the same day (a marketing blitz); analyzing
    each gives near-identical, overlapping windows. We group runs whose adjacent
    gap <= window_days and keep the most informative post (patch/launch > note)
    as the anchor, recording how many were merged.
    """
    if not events:
        return []
    win = window_days * DAY
    clusters: list[list[dict[str, Any]]] = [[events[0]]]
    for ev in events[1:]:  # newest-first
        if clusters[-1][-1]["ts"] - ev["ts"] <= win:
            clusters[-1].append(ev)
        else:
            clusters.append([ev])

    out: list[dict[str, Any]] = []
    for group in clusters:
        anchor = max(group, key=lambda e: (_TYPE_RANK.get(e["type"], 0), e["ts"]))
        merged = {**anchor, "merged_count": len(group)}
        if len(group) > 1:
            merged["title"] = f'{anchor["title"]} (+{len(group) - 1} related posts)'
        out.append(merged)
    return out


def _ratio(reviews: list[dict[str, Any]]) -> float | None:
    if not reviews:
        return None
    return sum(1 for r in reviews if r.get("voted_up")) / len(reviews)


async def analyze_events(
    appid: int,
    events: list[dict[str, Any]],
    *,
    measure: Measure,
    window_days: int = 5,
    max_events: int = 6,
    max_pages: int = 60,
    min_reviews: int = 5,
) -> dict[str, Any]:
    """Walk reviews backward once, score each recent event's ±window_days slice.

    Crawls just deep enough to cover the most-recent `max_events` events
    (bounded by `max_pages` as a safety cap), so popular titles don't trigger a
    full-history crawl.
    """
    win = window_days * DAY
    # Cutoff = how far back we must reach to cover the targeted recent events.
    target = events[:max_events] if max_events else events
    until_ts = (min(e["ts"] for e in target) - win) if target else None

    walked: list[dict[str, Any]] = []
    async for rev in steam.iter_reviews(appid, max_pages=max_pages, until_ts=until_ts):
        walked.append(rev)

    coverage = {
        "reviews_walked": len(walked),
        "oldest_review_ts": min((r["timestamp"] for r in walked), default=None),
        "newest_review_ts": max((r["timestamp"] for r in walked), default=None),
    }
    oldest = coverage["oldest_review_ts"]
    analyzed = 0

    for ev in events:  # newest first
        ev["analysis"] = None
        if analyzed >= max_events or oldest is None:
            continue
        lo, hi = ev["ts"] - win, ev["ts"] + win
        if hi < oldest:  # event window predates everything we reached
            continue
        window_reviews = [r for r in walked if lo <= r["timestamp"] <= hi]
        if len(window_reviews) < min_reviews:
            continue

        before = [r for r in window_reviews if r["timestamp"] < ev["ts"]]
        after = [r for r in window_reviews if r["timestamp"] >= ev["ts"]]
        b, a = _ratio(before), _ratio(after)
        scored = await measure.compute(window_reviews)
        ev["analysis"] = {
            **scored,
            "approval_steam_before": b,
            "approval_steam_after": a,
            "delta": (a - b) if (a is not None and b is not None) else None,
            "window_days": window_days,
        }
        analyzed += 1

    return {"events": events, "coverage": coverage, "events_analyzed": analyzed}


async def build_timeline(
    appid: int,
    *,
    name: str | None = None,
    use_llm: bool = True,
    window_days: int = 5,
    max_events: int = 6,
    max_pages: int = 20,
) -> dict[str, Any]:
    baseline = await build_baseline(appid)
    events = await get_events(appid, launch_ts=baseline.get("start_ts"))
    events = cluster_events(events, window_days=window_days)
    measure = ApprovalMeasure(use_llm=use_llm)
    analysis = await analyze_events(
        appid, events, measure=measure, window_days=window_days,
        max_events=max_events, max_pages=max_pages,
    )
    return {
        "appid": appid,
        "name": name or str(appid),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "measure": measure.name,
        "used_llm": use_llm,
        "baseline": baseline,
        "events": analysis["events"],
        "coverage": analysis["coverage"],
        "events_analyzed": analysis["events_analyzed"],
    }
