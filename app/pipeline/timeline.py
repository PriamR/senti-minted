"""Build a product's retrospective sentiment timeline.

Three layers (see PLANNING.md and TECHNICAL_BREAKDOWN.md):
  1. Baseline curve  — Steam's review histogram, full history, free.
  2. Movement        — official Steam announcements are chained into rolling event
                       groups, each with an asymmetric window (1 week before the
                       burst, 3 weeks after its last event). Every group gets exact
                       before/after approval from Steam's date-range counts, with a
                       two-proportion z-score. Free.
  3. Analysis        — the LLM is spent only where it matters: the launch group plus
                       the groups where approval moved most (or the most recent K).
                       Each selected window gets a stratified review sample scored
                       by the approval Measure: aspects, divisiveness, drivers.
"""
from __future__ import annotations

import asyncio
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from ..clients import steam
from ..clients.deepseek import UsageLedger
from .. import cache
from ..measures import Measure
from ..measures.approval import ApprovalMeasure
from .sampling import sample_window

DAY = 86400
PATCH_KEYWORDS = (
    "update", "patch", "fix", "hotfix", "changelog",
    "version", "build", "maintenance", "expedition", "release",
)
SELECT_STRATEGIES = ("movers", "recent", "all")

Progress = Callable[[str], None]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _noop(_: str) -> None:
    pass


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


async def get_events(appid: int, *, launch_ts: int | None, count: int = 1000) -> list[dict[str, Any]]:
    """The developer's own Steam announcements (+ a synthetic Launch), newest first.

    Press coverage is excluded at the source (official feed only); cross-posted
    duplicates (same day, same title) are collapsed.
    """
    items = await steam.get_news(appid, count=count, feeds=steam.OFFICIAL_FEED)
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for it in items:
        title = (it.get("title") or "").strip()
        ts = it.get("date", 0)
        key = (_iso(ts), title.lower())
        if key in seen:
            continue
        seen.add(key)
        is_patch = any(kw in title.lower() for kw in PATCH_KEYWORDS)
        events.append({
            "ts": ts,
            "date": _iso(ts),
            "title": title,
            "url": it.get("url", ""),
            "type": "patch" if is_patch else "announcement",
            "blurb": it.get("contents", ""),
        })
    if launch_ts:
        events.append({
            "ts": launch_ts, "date": _iso(launch_ts),
            "title": "Launch", "url": "", "type": "launch", "blurb": "",
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
        # Per-day event frequency — flags marketing blitzes (e.g. 5 posts one day).
        day_counts = Counter(m["date"] for m in members)
        peak_day, peak_day_count = day_counts.most_common(1)[0]
        groups.append({
            "ts": anchor["ts"],
            "date": _iso(anchor["ts"]),
            "title": title,
            "type": anchor["type"],
            "n_events": len(members),
            "day_counts": dict(day_counts),
            "peak_day": peak_day,
            "peak_day_count": peak_day_count,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "blurb": anchor.get("blurb", ""),
            "members": [
                {"date": m["date"], "title": m["title"], "type": m["type"], "url": m.get("url", "")}
                for m in members
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


def two_proportion_z(up_b: int, n_b: int, up_a: int, n_a: int) -> float | None:
    """z for the change in approval between two independent groups of reviews."""
    if n_b == 0 or n_a == 0:
        return None
    p = (up_b + up_a) / (n_b + n_a)
    se = math.sqrt(p * (1 - p) * (1 / n_b + 1 / n_a))
    if se == 0:
        return None
    return round((up_a / n_a - up_b / n_b) / se, 2)


async def measure_movement(appid: int, groups: list[dict[str, Any]], *, min_z: float = 2.0) -> None:
    """Exact before/after approval for every group, in place (two cheap requests each).

    Before = reviews created in [window_start, first_event); after = (last_event,
    window_end]. The burst interior is excluded from both halves. Counts are by
    creation date with each review's current vote (see sampling.py on edits).
    """
    now = _now_ts() // 3600 * 3600  # hour-rounded so an open window's query stays cacheable

    async def one(g: dict[str, Any]) -> None:
        w = g["window"]
        before, after = await asyncio.gather(
            steam.get_window_summary(appid, w["start_ts"], g["first_ts"] - 1),
            steam.get_window_summary(appid, g["last_ts"] + 1, min(w["end_ts"], now)),
        )
        z = two_proportion_z(before["up"], before["n"], after["up"], after["n"])
        delta = None
        if before["approval"] is not None and after["approval"] is not None:
            delta = round(after["approval"] - before["approval"], 4)
        g["movement"] = {
            "n_before": before["n"], "n_after": after["n"],
            "approval_before": round(before["approval"], 4) if before["approval"] is not None else None,
            "approval_after": round(after["approval"], 4) if after["approval"] is not None else None,
            "delta": delta,
            "z": z,
            "significant": z is not None and abs(z) >= min_z,
        }

    await asyncio.gather(*(one(g) for g in groups))


def select_windows(
    groups: list[dict[str, Any]],
    *,
    k: int = 6,
    strategy: str = "movers",
    min_n: int = 30,
    min_z: float = 2.0,
) -> list[dict[str, Any]]:
    """Choose which groups get the paid LLM read, recording why on each.

    movers: the launch group, then the groups with the largest |delta| among those
            with >= min_n reviews on both sides and |z| >= min_z.
    recent: the K newest groups (the original behaviour).
    all:    every group.
    """
    for g in groups:
        g["selected"], g["selection_reason"] = False, None
    if strategy == "all":
        chosen = list(groups)
        for g in chosen:
            g["selection_reason"] = "all"
    elif strategy == "recent":
        chosen = groups[:k]
        for g in chosen:
            g["selection_reason"] = "recent"
    elif strategy == "movers":
        chosen = []
        launch = next((g for g in groups if g["type"] == "launch"), None)
        if launch is not None and k > 0:
            launch["selection_reason"] = "launch"
            chosen.append(launch)
        pool = [
            g for g in groups
            if g is not launch and (m := g.get("movement")) and m["delta"] is not None
            and m["n_before"] >= min_n and m["n_after"] >= min_n and m["significant"]
        ]
        pool.sort(key=lambda g: abs(g["movement"]["delta"]), reverse=True)
        for g in pool[:max(0, k - len(chosen))]:
            m = g["movement"]
            g["selection_reason"] = f"moved {m['delta'] * 100:+.1f} pts (z={m['z']})"
            chosen.append(g)
    else:
        raise ValueError(f"unknown selection strategy {strategy!r}; use one of {SELECT_STRATEGIES}")
    for g in chosen:
        g["selected"] = True
    return chosen


async def analyze_window(
    appid: int,
    start_ts: int,
    end_ts: int,
    *,
    measure: Measure,
    context: dict[str, Any],
    sample_reviews: int = 200,
    language: str = "english",
    min_reviews: int = 5,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Sample a window and score it. Returns (analysis, llm_calls, reason_if_none)."""
    population, sample = await asyncio.gather(
        steam.get_window_summary(appid, start_ts, min(end_ts, _now_ts())),
        sample_window(appid, start_ts, end_ts, n=sample_reviews, language=language),
    )
    if len(sample["reviews"]) < min_reviews:
        return None, [], f"only {len(sample['reviews'])} eligible reviews in window"
    scored = await measure.compute(sample["reviews"], context)
    calls = scored.pop("_calls", [])
    return {
        **scored,
        "approval_population": round(population["approval"], 4) if population["approval"] is not None else None,
        "n_population": population["n"],
        "sample": sample["stats"],
    }, calls, None


async def build_timeline(
    appid: int,
    *,
    name: str | None = None,
    use_llm: bool = True,
    single_model: bool = False,
    before_days: int = 7,
    after_days: int = 21,
    chain_gap_days: int = 7,
    max_events: int = 6,
    select: str = "movers",
    sample_reviews: int = 200,
    language: str = "english",
    window_concurrency: int = 2,
    progress: Progress | None = None,
) -> dict[str, Any]:
    say = progress or _noop
    cache.reset_stats()
    say("Fetching the full review history...")
    baseline = await build_baseline(appid)
    say("Fetching official announcements...")
    events = await get_events(appid, launch_ts=baseline.get("start_ts"))
    groups = group_events(
        events, chain_gap_days=chain_gap_days,
        before_days=before_days, after_days=after_days,
    )
    say(f"Measuring before/after approval for {len(groups)} event groups...")
    await measure_movement(appid, groups)
    chosen = select_windows(groups, k=max_events, strategy=select)

    measure = ApprovalMeasure(use_llm=use_llm, single_model=single_model)
    ledger = UsageLedger()
    sem = asyncio.Semaphore(window_concurrency)
    now = _now_ts()
    done = 0
    for g in groups:
        g["analysis"] = None

    async def run(g: dict[str, Any]) -> None:
        nonlocal done
        w = g["window"]
        context = {
            "game": name or str(appid),
            "start": w["start_date"], "end": w["end_date"],
            "events": [f'{m["date"]}: {m["title"]}' for m in g["members"]][:12],
            "event_blurb": g.get("blurb", "")[:400],
            "movement": g.get("movement"),
        }
        async with sem:
            analysis, calls, reason = await analyze_window(
                appid, w["start_ts"], w["end_ts"], measure=measure, context=context,
                sample_reviews=sample_reviews, language=language,
            )
        ledger.calls.extend(calls)
        done += 1
        say(f"Analyzed {done}/{len(chosen)}: {g['date']} {g['title'][:50]}")
        if analysis is None:
            g["analysis_skipped"] = reason
            return
        m = g.get("movement") or {}
        g["analysis"] = {
            **analysis,
            "approval_steam_before": m.get("approval_before"),
            "approval_steam_after": m.get("approval_after"),
            "n_before": m.get("n_before"),
            "n_after": m.get("n_after"),
            "delta": m.get("delta"),
            "after_days_observed": round(max(0, min(w["end_ts"], now) - g["last_ts"]) / DAY, 1),
            "window_truncated": w["truncated"],
        }

    say(f"Sampling and scoring {len(chosen)} window(s)...")
    await asyncio.gather(*(run(g) for g in chosen))

    analyzed = [g for g in groups if g.get("analysis")]
    return {
        "appid": appid,
        "name": name or str(appid),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "measure": measure.name,
        "method": measure.method if chosen else "curve_only",
        "used_llm": use_llm,
        "degraded": any(g["analysis"].get("degraded") for g in analyzed),
        "window": {
            "before_days": before_days, "after_days": after_days,
            "chain_gap_days": chain_gap_days,
        },
        "selection": {"strategy": select, "k": max_events, "min_n": 30, "min_z": 2.0},
        "sampling": {"per_window": sample_reviews, "language": language},
        "baseline": baseline,
        "events": groups,
        "events_analyzed": len(analyzed),
        "llm_usage": ledger.summary(),
        "cache": cache.stats(),
    }
