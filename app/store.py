"""Saved timelines on disk: data/timeline_<appid>.json + out/timeline_<appid>.html.

The JSON is the contract between the pipeline and every consumer (CLI, API, page).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .viz import render_html

DATA_DIR = PROJECT_ROOT / "data"
OUT_DIR = PROJECT_ROOT / "out"
_NAME = re.compile(r"^timeline_(\d+)\.json$")


def json_path(appid: int) -> Path:
    return DATA_DIR / f"timeline_{appid}.json"


def save(timeline: dict[str, Any]) -> tuple[Path, Path]:
    DATA_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    jp = json_path(timeline["appid"])
    hp = OUT_DIR / f"timeline_{timeline['appid']}.html"
    jp.write_text(json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8")
    hp.write_text(render_html(timeline), encoding="utf-8")
    return jp, hp


def load(appid: int) -> dict[str, Any] | None:
    p = json_path(appid)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_saved() -> list[dict[str, Any]]:
    """One summary row per saved timeline, newest first."""
    rows = []
    if not DATA_DIR.exists():
        return rows
    for p in DATA_DIR.iterdir():
        m = _NAME.match(p.name)
        if not m:
            continue
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pts = [x for x in t.get("baseline", {}).get("points", []) if x.get("approval") is not None]
        rows.append({
            "appid": t["appid"],
            "name": t.get("name"),
            "generated_at": t.get("generated_at"),
            "method": t.get("method"),
            "events_analyzed": t.get("events_analyzed", 0),
            "degraded": t.get("degraded", False),
            "approval_first": pts[0]["approval"] if pts else None,
            "approval_last": pts[-1]["approval"] if pts else None,
            "cost_usd": (t.get("llm_usage") or {}).get("cost_usd"),
        })
    rows.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return rows
