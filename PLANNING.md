# Product Sentiment Journey Tool — Planning

## What this is

A **retrospective** sentiment-through-time engine: given a product, reconstruct how public
approval moved across its whole lifetime and timestamp it against the events (patches, updates,
releases) that moved it. It is *not* predictive. Hero demo = No Man's Sky: ~47% approval at its
2016 launch → ~91% today, the redemption arc visible at a glance.

We start with **one measure (approval)** on a clean, pluggable scaffold, then layer more
dimensions (extremity, divisiveness, language spread) behind the same interface.

## The data-reality insight that shapes everything

Retrospective history has a hard constraint: **only Steam reviews carry clean, complete,
timestamped history.** Reddit search is recency-biased (Pushshift is effectively dead); YouTube
comments can't be cleanly date-windowed. So:

- **Historical spine = Steam.** `appreviewhistogram/{appid}` returns monthly up/down rollups across
  the *entire* history in one call — this *is* approval-through-time, free and full-coverage.
- **Review text** for LLM measures comes from paginating `appreviews?filter=recent`, sampled per
  window — not exhaustively.
- **Events** = `ISteamNews/GetNewsForApp` (patches/updates) + a synthetic Launch.
- **Reddit / YouTube** = recent-window enrichment & cross-validation later — NOT the historical
  backbone.

## Architecture (two layers)

1. **Baseline curve** — Steam histogram → monthly approval points across all of history (free).
2. **Event windows** — cluster Steam-News events, then for the most-recent reachable ones take a
   **±5-day window**, sample the review text in it, and run the approval `Measure` (DeepSeek) →
   `approval_llm` + **drivers** + before/after delta. Drivers (the "why") are the whole point — the
   raw thumbs-up ratio can't tell you *which features* moved sentiment, or whether a 60% game is
   calmly mixed vs violently split (divisiveness).

### Bucketing — event-anchored rolling windows (not fixed calendar)
Review volume swings from thousands/week at launch to a trickle later, so fixed windows are wrong.
The histogram gives the continuous baseline; the LLM runs on **asymmetric, rolling** windows
anchored to flagged events (`group_events`):

- **Window = 1 week before the burst → 3 weeks after the last event in it** (`--before-days 7`,
  `--after-days 21`). Asymmetric because reviews lag playtime — the reaction accrues for weeks.
- **Rolling chain:** walking chronologically, events within `--chain-gap-days 7` of the previous
  one are absorbed into the same group, and each absorbed event pushes the 3-week tail out so the
  newest still gets its full window. So a same-day post blitz *and* a run of weekly patches each
  collapse to one group (you can't disentangle weekly patches' effects anyway).
- **Delta** compares the week before the first event vs the tail after the last; the burst interior
  feeds the LLM's view but not the delta halves. Very recent groups whose +3-week tail runs past
  today are flagged `window_truncated` with `after_days_observed`.

### Cost / reachability (validated)
Steam has no "reviews between A and B" param, so historical text needs a backward `filter=recent`
crawl. Crawl depth scales with the title's review velocity: NMS (~95 reviews/day) needs ~88 pages
(~80s) to reach back ~10 weeks. v0 therefore LLM-analyzes the most-recent reachable events (the
`until_ts` adaptive stop + `--max-pages` safety cap) while the free histogram delivers the full
curve. Deep-history LLM backfill is a one-time cacheable batch job — noted, not built.

## Code map

- `app/clients/steam.py` — `get_review_histogram`, `iter_reviews` (cursor-paged, one reused
  connection, retry/backoff, `until_ts` adaptive stop), `get_news`.
- `app/measures/` — `Measure` protocol + `approval.py` (`approval_steam`, `approval_llm`,
  divisiveness, drivers, summary). The seam for extremity / language-spread / etc.
- `app/pipeline/timeline.py` — `build_baseline`, `get_events`, `cluster_events`, `analyze_events`,
  `build_timeline`.
- `app/viz.py` — self-contained inline-SVG HTML (curve + event markers + driver cards).
- `scripts/build_timeline.py` — CLI; writes `data/timeline_<appid>.json` + `out/timeline_<appid>.html`.

## Run it

```bash
python -m scripts.build_timeline 275850 --name "No Man's Sky"            # full (LLM)
python -m scripts.build_timeline 275850 --name "No Man's Sky" --no-llm   # curve only, no tokens
# tuning: --window-days 5  --max-events N  --max-pages 60(safety cap)
```

## Measures roadmap (same scaffold)

- **extremity** — intensity/share of strong language.
- **divisiveness** — already returned by the approval measure; surface it as its own dimension.
- **language spread** — distinct languages discussing the title (Steam reviews carry a per-review
  `language` field; histogram accepts `l`) = market-breadth analogue.
- **volume/velocity**, **aspect breakdown over time**.

## Known refinements / next steps

- Asymmetric event windows (longer "after").
- Cache crawled reviews per appid so re-runs and deep-history backfill are cheap.
- Cross-validate recent windows with Reddit/YouTube.
- A second demo title with lower review velocity (cheaper deep crawl, more event cards).
