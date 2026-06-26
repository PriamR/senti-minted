"""Build a product's sentiment journey and write JSON + a self-contained HTML chart.

Usage:
    python -m scripts.build_timeline 275850 --name "No Man's Sky"
    python -m scripts.build_timeline 275850 --no-llm
    python -m scripts.build_timeline 275850 --window-days 5 --max-events 6 --max-pages 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.pipeline.timeline import build_timeline
from app.viz import render_html

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"


async def main() -> int:
    ap = argparse.ArgumentParser(description="Build a retrospective sentiment timeline.")
    ap.add_argument("appid", type=int, help="Steam appid (e.g. 275850 = No Man's Sky)")
    ap.add_argument("--name", default=None, help="Display name for the product")
    ap.add_argument("--no-llm", action="store_true", help="Histogram curve only (no tokens)")
    ap.add_argument("--before-days", type=int, default=7, help="Window: days before the burst")
    ap.add_argument("--after-days", type=int, default=21, help="Window: days after the last event")
    ap.add_argument("--chain-gap-days", type=int, default=7,
                    help="Chain events into one group if within this many days")
    ap.add_argument("--max-events", type=int, default=6, help="Most recent event groups to LLM-analyze")
    ap.add_argument("--max-pages", type=int, default=60, help="Safety cap on review pages (100/page)")
    args = ap.parse_args()

    print(f"Building timeline for appid {args.appid} ({args.name or 'unnamed'})...")
    timeline = await build_timeline(
        args.appid,
        name=args.name,
        use_llm=not args.no_llm,
        before_days=args.before_days,
        after_days=args.after_days,
        chain_gap_days=args.chain_gap_days,
        max_events=args.max_events,
        max_pages=args.max_pages,
    )

    DATA_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)
    json_path = DATA_DIR / f"timeline_{args.appid}.json"
    html_path = OUT_DIR / f"timeline_{args.appid}.html"
    json_path.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    html_path.write_text(render_html(timeline), encoding="utf-8")

    base_pts = [p for p in timeline["baseline"]["points"] if p["approval"] is not None]
    if base_pts:
        first, last = base_pts[0], base_pts[-1]
        print(f"  baseline: {len(base_pts)} months, "
              f"{first['date']} {first['approval']:.0%} -> {last['date']} {last['approval']:.0%}")
    cov = timeline["coverage"]
    print(f"  crawled {cov['reviews_walked']} reviews; analyzed {timeline['events_analyzed']} event window(s)")
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
