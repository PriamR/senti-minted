"""The agent's tool surface — its *entire* API.

Each tool is a thin, deterministic wrapper over existing backend functions; the
LLM can call nothing else. Every number the agent reports therefore traces back
to one of these results (the grounding guarantee). Tools are exposed to DeepSeek
as JSON-schema functions; `dispatch` runs them by name.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..measures.approval import ApprovalMeasure
from ..pipeline import timeline
from ..resolver import resolve_product as _resolve_product

DAY = 86400
_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# How recent a window must be for analyze_window to attempt a live crawl.
RECENT_REACHABLE_DAYS = 120


def _now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def _ts(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _num(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _NUM_WORDS.get(token)


# ── tool implementations ────────────────────────────────────────────────────

async def resolve_product(name: str) -> dict[str, Any]:
    return await _resolve_product(name)


def resolve_timeframe(launch_date: str, phrase: str) -> dict[str, Any]:
    """Deterministic date math: a relative phrase → an absolute [start, end].

    Keeps date arithmetic out of the LLM. Handles 'at launch', 'first N months/
    years', 'N years/months in|after launch', and 'recently/now/last N months'.
    """
    p = phrase.lower().strip()
    launch = _ts(launch_date) if launch_date else None

    def span(start: int, end: int, interp: str) -> dict[str, Any]:
        return {"start": _iso(start), "end": _iso(end), "interpretation": interp}

    # recency phrases (don't need launch_date)
    if re.search(r"\b(now|today|currently|recently|lately|these days|present)\b", p):
        return span(_now() - 60 * DAY, _now(), "most recent ~60 days")
    m = re.search(r"last (\w+) (month|year)s?", p)
    if m and _num(m.group(1)):
        units = _num(m.group(1)) * (365 if m.group(2) == "year" else 30)
        return span(_now() - units * DAY, _now(), f"last {m.group(1)} {m.group(2)}(s)")

    if launch is None:
        return {"error": "launch_date required for this phrase"}

    if re.search(r"\b(at launch|launch|release|day one|debut)\b", p):
        return span(launch, launch + 45 * DAY, "launch window (first ~6 weeks)")
    m = re.search(r"first (\w+) (month|year)s?", p)
    if m and _num(m.group(1)):
        units = _num(m.group(1)) * (365 if m.group(2) == "year" else 30)
        return span(launch, launch + units * DAY, f"first {m.group(1)} {m.group(2)}(s)")
    # "N years in", "N years after launch", "N-year mark", "after N months"
    m = re.search(r"(\w+)[ -](year|month)s?\s*(in|after|mark|on|later|post)?", p)
    if m and _num(m.group(1)):
        n = _num(m.group(1))
        units = n * (365 if m.group(2) == "year" else 30)
        anniv = launch + units * DAY
        # anniversary window, widened forward to catch a proximate major event
        return span(anniv - 15 * DAY, anniv + 45 * DAY, f"~{n} {m.group(2)}(s) after launch")

    return {"error": f"could not parse timeframe '{phrase}'"}


async def get_approval_curve(appid: int, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    base = await timeline.build_baseline(appid)
    pts = [p for p in base["points"] if p["approval"] is not None]
    # Histogram points are monthly (dated to the 1st), so slice by year-month, not
    # exact day — otherwise the boundary months (launch month, an event month) drop.
    if start:
        pts = [p for p in pts if p["date"][:7] >= start[:7]]
    if end:
        pts = [p for p in pts if p["date"][:7] <= end[:7]]
    rows = [{"month": p["date"][:7], "approval": round(p["approval"], 3), "n": p["n"]} for p in pts]
    if len(rows) > 36:  # keep payload small for the LLM
        step = len(rows) // 36 + 1
        rows = rows[::step]
    summary = None
    if rows:
        summary = {
            "first": rows[0], "last": rows[-1],
            "min": min(rows, key=lambda r: r["approval"]),
            "max": max(rows, key=lambda r: r["approval"]),
        }
    return {"appid": appid, "points": rows, "summary": summary}


async def get_events(appid: int, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    base = await timeline.build_baseline(appid)
    evs = await timeline.get_events(appid, launch_ts=base.get("start_ts"))
    groups = timeline.group_events(evs)
    if start:
        s = _ts(start); groups = [g for g in groups if g["window"]["end_ts"] >= s]
    if end:
        e = _ts(end); groups = [g for g in groups if g["ts"] <= e]
    out = [
        {"date": g["date"], "title": g["title"], "type": g["type"],
         "n_posts": g["n_events"], "peak_day_count": g["peak_day_count"]}
        for g in groups[:12]
    ]
    note = None
    if not out:
        note = ("No catalogued events in range. Steam News only returns recent items, "
                "so historical events may be absent without the news backfill.")
    return {"appid": appid, "events": out, "note": note}


async def analyze_window(appid: int, start: str, end: str) -> dict[str, Any]:
    """The expensive tool: LLM drivers + divisiveness for a window. Gated to
    recent (reachable) windows; older windows need the historical backfill."""
    end_ts = _ts(end)
    if end_ts < _now() - RECENT_REACHABLE_DAYS * DAY:
        return {
            "available": False,
            "reason": (f"window ends {end} — older than ~{RECENT_REACHABLE_DAYS}d, so review text "
                       "isn't reachable without the historical backfill. Use approval-curve numbers."),
        }
    return await timeline.analyze_explicit_window(
        appid, _ts(start), end_ts, measure=ApprovalMeasure(use_llm=True)
    )


# ── schemas + dispatch ──────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "resolve_product",
        "description": "Resolve a product name (or Steam appid) to its canonical appid, launch_date, and confidence. Call this FIRST.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Product name, e.g. 'No Man's Sky', or a numeric Steam appid"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "resolve_timeframe",
        "description": "Convert a relative time phrase into an absolute {start,end} using the product's launch_date. Use for any temporal phrase; never compute dates yourself.",
        "parameters": {"type": "object", "properties": {
            "launch_date": {"type": "string", "description": "ISO launch date from resolve_product (YYYY-MM-DD)"},
            "phrase": {"type": "string", "description": "e.g. '2 years in', 'at launch', 'first 6 months', 'recently'"}},
            "required": ["launch_date", "phrase"]}}},
    {"type": "function", "function": {
        "name": "get_approval_curve",
        "description": "Monthly approval (0-1) over time from Steam's review histogram. Free; use for level and trend. Optional start/end ISO dates to slice.",
        "parameters": {"type": "object", "properties": {
            "appid": {"type": "integer"},
            "start": {"type": "string", "description": "ISO date (optional)"},
            "end": {"type": "string", "description": "ISO date (optional)"}},
            "required": ["appid"]}}},
    {"type": "function", "function": {
        "name": "get_events",
        "description": "Catalogued events (Steam News patches/updates) in a date range. Free. Historical events may be absent.",
        "parameters": {"type": "object", "properties": {
            "appid": {"type": "integer"},
            "start": {"type": "string"}, "end": {"type": "string"}},
            "required": ["appid"]}}},
    {"type": "function", "function": {
        "name": "analyze_window",
        "description": "EXPENSIVE. LLM read of review TEXT for a window: drivers (why sentiment moved) + divisiveness. Only call when the user needs the WHY. Works only for recent windows; returns available:false for old ones.",
        "parameters": {"type": "object", "properties": {
            "appid": {"type": "integer"},
            "start": {"type": "string", "description": "ISO date"},
            "end": {"type": "string", "description": "ISO date"}},
            "required": ["appid", "start", "end"]}}},
]

_DISPATCH = {
    "resolve_product": resolve_product,
    "get_approval_curve": get_approval_curve,
    "get_events": get_events,
    "analyze_window": analyze_window,
}


async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "resolve_timeframe":  # sync
        return resolve_timeframe(**args)
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool '{name}'"}
    return await fn(**args)
