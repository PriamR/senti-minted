"""Build a product's retrospective sentiment timeline.

Two layers (see PLANNING.md):
  1. Baseline curve  — Steam's review histogram, full history, free.
  2. Event windows   — Steam-News events are chained into rolling groups, each
                       analyzed over an asymmetric window (1 week before the
                       burst, 3 weeks after the last event in it). Sampled review
                       text is scored by the approval Measure (LLM), giving each
                       group a before/after delta + drivers.
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


def _now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def group_events(
    events: list[dict[str, Any]],
    *,
    chain_gap_days: int = 7,
    before_days: int = 7,
    after_days: int = 21,
) -> list[dict[str, Any]]:
    """Chain events into rolling analysis groups (asymmetric windows).

    Walk chronologically and keep absorbing the next event while it lands within
    `chain_gap_days` of the previous one — so a same-day post blitz AND a run of
    weekly patches both merge into one group. Each group's analysis window is
    [first_event - before_days, last_event + after_days]; every absorbed event
    pushes the tail out so the latest event still gets its full `after_days`.

    The anchor (for the marker/label) is the most informative member
    (patch/launch > announcement), tie-broken to the earliest — the trigger.
    """
    if not events:
        return []
    asc = sorted(events, key=lambda e: e["ts"])
    gap = chain_gap_days * DAY
    runs: list[list[dict[str, Any]]] = [[asc[0]]]
    for ev in asc[1:]:
        if ev["ts"] - runs[-1][-1]["ts"] <= gap:
            runs[-1].append(ev)
        else:
            runs.append([ev])

    now = _now_ts()
    groups: list[dict[str, Any]] = []
    for members in runs:
        first_ts, last_ts = members[0]["ts"], members[-1]["ts"]
        anchor = max(members, key=lambda e: (_TYPE_RANK.get(e["type"], 0), -e["ts"]))
        win_start = first_ts - before_days * DAY
        win_end = last_ts + after_days * DAY
        title = anchor["title"]
        if len(members) > 1:
            title = f'{anchor["title"]} (+{len(members) - 1} more)'
        groups.append({
            "ts": anchor["ts"],
            "date": _iso(anchor["ts"]),
            "title": title,
            "type": anchor["type"],
            "n_events": len(members),
            "first_ts": first_ts,
            "last_ts": last_ts,
            "members": [
                {"date": m["date"], "title": m["title"], "type": m["type"]} for m in members
            ],
            "window": {
                "start_ts": win_start, "end_ts": win_end,
                "start_date": _iso(win_start), "end_date": _iso(win_end),
                "before_days": before_days, "after_days": after_days,
                "truncated": win_end > now,
            },
        })
    groups.sort(key=lambda g: g["ts"], reverse=True)  # newest first
    return groups


def _ratio(reviews: list[dict[str, Any]]) -> float | None:
    if not reviews:
        return None
    return sum(1 for r in reviews if r.get("voted_up")) / len(reviews)


async def analyze_events(
    appid: int,
    groups: list[dict[str, Any]],
    *,
    measure: Measure,
    max_events: int = 6,
    max_pages: int = 60,
    min_reviews: int = 5,
) -> dict[str, Any]:
    """Walk reviews backward once, score each recent event group's window.

    `max_events` here counts event *groups*. Delta compares the week before the
    burst (window_start .. first_event) against the tail after it
    (last_event .. window_end); the burst interior feeds the LLM but not the
    delta halves.
    """
    target = groups[:max_events] if max_events else groups
    until_ts = min((g["window"]["start_ts"] for g in target), default=None)

    walked: list[dict[str, Any]] = []
    async for rev in steam.iter_reviews(appid, max_pages=max_pages, until_ts=until_ts):
        walked.append(rev)

    coverage = {
        "reviews_walked": len(walked),
        "oldest_review_ts": min((r["timestamp"] for r in walked), default=None),
        "newest_review_ts": max((r["timestamp"] for r in walked), default=None),
    }
    oldest = coverage["oldest_review_ts"]
    now = _now_ts()
    analyzed = 0

    for g in groups:  # newest first
        g["analysis"] = None
        if analyzed >= max_events or oldest is None:
            continue
        w = g["window"]
        if w["end_ts"] < oldest:  # whole window predates what we crawled
            continue
        window_reviews = [r for r in walked if w["start_ts"] <= r["timestamp"] <= w["end_ts"]]
        if len(window_reviews) < min_reviews:
            continue

        before = [r for r in window_reviews if r["timestamp"] < g["first_ts"]]
        after = [r for r in window_reviews if r["timestamp"] > g["last_ts"]]
        b, a = _ratio(before), _ratio(after)
        scored = await measure.compute(window_reviews)
        after_observed = max(0, min(w["end_ts"], now) - g["last_ts"]) / DAY
        g["analysis"] = {
            **scored,
            "approval_steam_before": b,
            "approval_steam_after": a,
            "n_before": len(before),
            "n_after": len(after),
            "delta": (a - b) if (a is not None and b is not None) else None,
            "after_days_observed": round(after_observed, 1),
            "window_truncated": w["truncated"],
        }
        analyzed += 1

    return {"events": groups, "coverage": coverage, "events_analyzed": analyzed}


async def build_timeline(
    appid: int,
    *,
    name: str | None = None,
    use_llm: bool = True,
    before_days: int = 7,
    after_days: int = 21,
    chain_gap_days: int = 7,
    max_events: int = 6,
    max_pages: int = 60,
) -> dict[str, Any]:
    baseline = await build_baseline(appid)
    events = await get_events(appid, launch_ts=baseline.get("start_ts"))
    groups = group_events(
        events, chain_gap_days=chain_gap_days,
        before_days=before_days, after_days=after_days,
    )
    measure = ApprovalMeasure(use_llm=use_llm)
    analysis = await analyze_events(
        appid, groups, measure=measure, max_events=max_events, max_pages=max_pages,
    )
    return {
        "appid": appid,
        "name": name or str(appid),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "measure": measure.name,
        "used_llm": use_llm,
        "window": {
            "before_days": before_days, "after_days": after_days,
            "chain_gap_days": chain_gap_days,
        },
        "baseline": baseline,
        "events": analysis["events"],
        "coverage": analysis["coverage"],
        "events_analyzed": analysis["events_analyzed"],
    }
