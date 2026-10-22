"""Measure the two-tier model split: score ONE window both ways on the same sample.

    python -m scripts.compare_models 275850 2018-07-17 2018-09-07 --name "No Man's Sky"

Both runs see the identical stratified sample (the review pages are cached), and
LLM caching is bypassed so every call is paid for and measured. Prints tokens,
cost and latency per stage for each mode, the cost ratio, and how far the two
modes' numbers agree. Writes the full result to data/compare_<appid>_<start>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import cache
from app.clients import steam
from app.measures.approval import ApprovalMeasure
from app.pipeline.sampling import sample_window

ROOT = Path(__file__).resolve().parent.parent


def _ts(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _row(label: str, u: dict) -> str:
    return (f"  {label:<22} {u['calls']:>3} calls  {u['prompt_tokens']:>8,} in  "
            f"{u['completion_tokens']:>7,} out ({u['reasoning_tokens']:>6,} reasoning)  "
            f"${u['cost_usd']:.4f}  {u['latency_s']:>6.1f}s")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Two-tier vs single-model cost on one window.")
    ap.add_argument("appid", type=int)
    ap.add_argument("start", help="YYYY-MM-DD")
    ap.add_argument("end", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--sample-reviews", type=int, default=200)
    args = ap.parse_args()

    cache.bypass("llm")
    start_ts, end_ts = _ts(args.start), _ts(args.end) + 86399
    sample = await sample_window(args.appid, start_ts, end_ts, n=args.sample_reviews)
    pop = await steam.get_window_summary(args.appid, start_ts, end_ts)
    context = {"game": args.name or str(args.appid), "start": args.start, "end": args.end,
               "events": [], "movement": {"approval_window": pop["approval"], "n_window": pop["n"]}}
    print(f"Window {args.start} -> {args.end}: sampled {len(sample['reviews'])} reviews "
          f"(population {pop['n']:,}, approval {pop['approval']:.0%})\n")

    results = {}
    for mode, single in (("two_tier", False), ("single_model", True)):
        res = await ApprovalMeasure(use_llm=True, single_model=single).compute(sample["reviews"], context)
        res.pop("_calls", None)
        results[mode] = res
        u = res["usage"]
        print(f"{mode}  ({res['models']['classify']} -> {res['models']['synthesize']})"
              if res.get("models") else f"{mode}  (degraded: {res['degraded_reason']})")
        for stage, su in u["by_stage"].items():
            print(_row(stage, su))
        print(_row("total", u))
        print(f"  approval_text {res['approval_text']}  divisiveness {res['divisiveness']}  "
              f"drivers {[d['aspect'] + ('+' if d['direction'] == 'positive' else '-') for d in res['drivers']]}\n")

    a, b = results["two_tier"], results["single_model"]
    ratio = b["usage"]["cost_usd"] / a["usage"]["cost_usd"] if a["usage"]["cost_usd"] else None
    cls_ratio = None
    if a["usage"]["by_stage"].get("classify", {}).get("cost_usd"):
        cls_ratio = b["usage"]["by_stage"]["classify"]["cost_usd"] / a["usage"]["by_stage"]["classify"]["cost_usd"]
    top = lambda r: {x["aspect"] for x in r["aspects"][:5]}
    agreement = {
        "approval_text_diff": round(abs((a["approval_text"] or 0) - (b["approval_text"] or 0)), 3),
        "divisiveness_diff": round(abs(a["divisiveness"] - b["divisiveness"]), 3),
        "top5_aspect_overlap": len(top(a) & top(b)),
        "driver_overlap": len({d["aspect"] for d in a["drivers"]} & {d["aspect"] for d in b["drivers"]}),
    }
    print(f"Cost ratio single/two-tier: {ratio:.1f}x overall"
          + (f", {cls_ratio:.1f}x on classification" if cls_ratio else ""))
    print(f"Agreement: {agreement}")

    out = ROOT / "data" / f"compare_{args.appid}_{args.start}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "appid": args.appid, "window": [args.start, args.end], "sample": sample["stats"],
        "measured_at": datetime.now(tz=timezone.utc).isoformat(),
        "cost_ratio": ratio, "classify_cost_ratio": cls_ratio, "agreement": agreement,
        "results": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
