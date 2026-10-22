"""Build a product's sentiment journey and write JSON + a self-contained HTML chart.

Usage:
    python -m scripts.build_timeline 275850 --name "No Man's Sky"
    python -m scripts.build_timeline 275850 --no-llm            # rule-based, no tokens
    python -m scripts.build_timeline 275850 --curve-only        # histogram + movement only
    python -m scripts.build_timeline 275850 --select recent --max-events 4 --sample-reviews 100
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app import cache, store
from app.pipeline.timeline import SELECT_STRATEGIES, build_timeline


async def main() -> int:
    ap = argparse.ArgumentParser(description="Build a retrospective sentiment timeline.")
    ap.add_argument("appid", type=int, help="Steam appid (e.g. 275850 = No Man's Sky)")
    ap.add_argument("--name", default=None, help="Display name for the product")
    ap.add_argument("--no-llm", action="store_true", help="Rule-based (lexicon) analysis: no tokens spent")
    ap.add_argument("--curve-only", action="store_true", help="Histogram + per-event movement, no window analysis")
    ap.add_argument("--single-model", action="store_true",
                    help="Run classification on the pro model too (to compare against two-tier)")
    ap.add_argument("--no-llm-cache", action="store_true", help="Force fresh LLM calls (for cost measurement)")
    ap.add_argument("--select", choices=SELECT_STRATEGIES, default="movers",
                    help="Which event windows get the LLM read (default: launch + biggest movers)")
    ap.add_argument("--max-events", type=int, default=6, help="How many event windows to analyze")
    ap.add_argument("--sample-reviews", type=int, default=200, help="Reviews sampled per window")
    ap.add_argument("--language", default="english", help="Steam language for the text sample")
    ap.add_argument("--before-days", type=int, default=7, help="Window: days before the burst")
    ap.add_argument("--after-days", type=int, default=21, help="Window: days after the last event")
    ap.add_argument("--chain-gap-days", type=int, default=7,
                    help="Chain events into one group if within this many days")
    args = ap.parse_args()

    if args.no_llm_cache:
        cache.bypass("llm")
    print(f"Building timeline for appid {args.appid} ({args.name or 'unnamed'})...")
    timeline = await build_timeline(
        args.appid,
        name=args.name,
        use_llm=not args.no_llm,
        single_model=args.single_model,
        before_days=args.before_days,
        after_days=args.after_days,
        chain_gap_days=args.chain_gap_days,
        max_events=0 if args.curve_only else args.max_events,
        select="recent" if args.curve_only else args.select,
        sample_reviews=args.sample_reviews,
        language=args.language,
        progress=lambda msg: print(f"  {msg}", flush=True),
    )
    json_path, html_path = store.save(timeline)

    base_pts = [p for p in timeline["baseline"]["points"] if p["approval"] is not None]
    if base_pts:
        first, last = base_pts[0], base_pts[-1]
        print(f"  baseline: {len(base_pts)} months, "
              f"{first['date']} {first['approval']:.0%} -> {last['date']} {last['approval']:.0%}")
    sig = sum(1 for g in timeline["events"] if (g.get("movement") or {}).get("significant"))
    print(f"  {len(timeline['events'])} event groups, {sig} with a significant before/after move; "
          f"analyzed {timeline['events_analyzed']} ({timeline['method']}"
          f"{', DEGRADED' if timeline['degraded'] else ''})")
    u = timeline["llm_usage"]
    print(f"  LLM: {u['calls']} calls ({u['cached_calls']} cached), {u['prompt_tokens']:,} in / "
          f"{u['completion_tokens']:,} out tokens, ${u['cost_usd']:.4f}")
    c = timeline["cache"]
    print(f"  cache: {c['hits']} hits / {c['misses']} misses (hit rate {c['hit_rate']})")
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
