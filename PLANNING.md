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
2. **Event windows** — group the developer's official Steam announcements, measure exact
   before/after approval for every group (free), then for the launch and the biggest significant
   movers sample ~200 reviews and run the approval `Measure` (DeepSeek, two tiers) → aspect table,
   `approval_text`, divisiveness and validated **drivers**. Drivers (the "why") are the whole point — the
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

### Cost / reachability (revised 22 Oct 2026)
~~Steam has no "reviews between A and B" param~~ — it does. `appreviews` accepts `start_date`,
`end_date` and `date_range_type=include` (undocumented; the store's histogram uses them), verified on
NMS's August 2016 launch. Any window is now one query away, and the first page's `query_summary`
gives that window's exact up/down counts. The backward crawl (`iter_reviews`) is kept only for
whole-history walks; the historical backfill is no longer needed.

Two consequences found while building on it (details in TECHNICAL_BREAKDOWN.md):
- **Edits contaminate history.** Steam serves a review's *current* text and vote. On NMS launch day
  26% of reviews were edited >30 days later (80% positive vs 60% for unedited). The sampler excludes
  reviews edited more than 7 days after their window.
- **The histogram and date-range counts disagree** (NMS Aug 2016: 47% vs 56%). The gap fits the
  histogram binning reviews by last edit. The curve uses the histogram; window stats use creation
  dates; the edit-filtered sample (45%) sides with the histogram.

## Code map

- `app/clients/steam.py` — `get_review_histogram`, `get_reviews_page` / `get_window_summary`
  (date-range queries), `get_news` (official feed), `iter_reviews` (legacy crawl).
- `app/pipeline/sampling.py` — stratified, edit-filtered review sampler for any window.
- `app/measures/` — `Measure` protocol; `approval.py` (two-stage LLM), `aspects.py` (taxonomy and
  all the maths), `lexicon.py` (fallback). The seam for extremity / language-spread / etc.
- `app/pipeline/timeline.py` — `build_baseline`, `get_events`, `group_events`, `measure_movement`,
  `select_windows`, `analyze_window`, `build_timeline`.
- `app/cache.py` — per-source TTL cache. `app/api.py` + `app/web/` — FastAPI + page.
- `app/viz.py` — self-contained inline-SVG HTML (curve, movement ticks, driver + aspect cards).
- `scripts/build_timeline.py` — CLI; writes `data/timeline_<appid>.json` + `out/timeline_<appid>.html`.

## Run it

```bash
python -m scripts.build_timeline 275850 --name "No Man's Sky"            # full (LLM)
python -m scripts.build_timeline 275850 --name "No Man's Sky" --no-llm   # rule-based, no tokens
python -m scripts.build_timeline 275850 --curve-only                     # curve + movement only
# tuning: --select movers|recent|all  --max-events 6  --sample-reviews 200  --language english
#         --before-days 7  --after-days 21  --chain-gap-days 7  --single-model  --no-llm-cache
```

## Cross-source entity resolution (Reddit / YouTube → Steam appid)

Discussion sources carry no Steam appid — resolving free text to the right product is the
genuinely subjective part of the system. Strategy = **deterministic seed, precision-first
expansion.**

### 1. Resolve the canonical handles (mostly deterministic)
`appid → IGDB id` is deterministic (`external_games`, verified: 275850 ⟷ 3225). IGDB's `websites`
list then hands us the official handles directly (verified on NMS):
- `reddit.com/r/NoMansSkyTheGame` → **official subreddit**
- `youtube.com/@HelloGamesTube` → **official dev/publisher channel**
- plus official site, Twitter, Discord, etc.

Classify those links **by URL domain**, not IGDB's `category` enum (it's mid-deprecation —
`category`→`external_game_source`/`type` — and returns null for websites now). Where IGDB lacks a
link, fall back to heuristic discovery (below) and cache the result.

### 2. Ground truth = authoritative sources (no matching needed)
These are ~100% on-topic by construction, so they need no classifier:
- **Official subreddit**: pull its posts + comments (timestamped via `created_utc`).
- **Dev/publisher YouTube channel**: channel → uploads playlist → videos whose title/description
  match the game → `commentThreads`. (Filter videos because a publisher channel covers many games.)

This seed corpus is the **reference** the wild-expansion classifier scores against.

### 3. Wild expansion = needs classification (precision-first)
Discussion in general subs (r/Games, r/pcgaming) or general YouTube search *does* need a match:
- **String layer:** canonical name + aliases (IGDB `name` + `alternative_names`) + abbreviations,
  with an **exclusion list** to kill collisions ("Hades" vs "Hades II", editions, remasters).
- **Semantic layer:** an LLM (or embedding-similarity-to-seed-centroid) yes/no/confidence call —
  "is this comment about <game>?" — using a few exemplars drawn from the §2 seed corpus.
- Store a **confidence** per item; threshold by measure (a sentiment *trend* tolerates noise that
  averages out; *driver extraction* must lean on §2 ground truth + high-confidence §3 only).

### 4. The resolver module (proposed)
`app/resolver/` produces and persists a `GameEntity`:
`{ appid, igdb_id, canonical_name, aliases[], official_subreddit, official_youtube_channel,
   dev/publisher names, per-link source (igdb|heuristic|manual) + confidence }`,
backed by a SQLite **entity/alias map** — resolve-once, cache, manual override. Discussion clients
then take a `GameEntity`: `fetch_ground_truth(entity)` (§2) and `fetch_wild(entity)` (§3).

### Heuristic fallbacks (when IGDB lacks the link)
- subreddit: Reddit `/subreddits/search` by name → verify via description/subscribers/LLM.
- channel: YouTube search by publisher name → verify via channel metadata.

### Caveat (retrospective angle)
Reddit/YouTube remain recency-biased (no deep historical archive), so this layer is **present-day
cross-validation + recent-event enrichment**, complementing — not replacing — the Steam-review
historical spine. Top-of-all-time subreddit posts can backfill *some* history.

## Measures roadmap (same scaffold)

- **extremity** — intensity/share of strong language.
- **divisiveness** — already returned by the approval measure; surface it as its own dimension.
- **language spread** — distinct languages discussing the title (Steam reviews carry a per-review
  `language` field; histogram accepts `l`) = market-breadth analogue.
- **volume/velocity**, **aspect breakdown over time**.

## Known refinements / next steps

- ~~Asymmetric event windows~~ (done). ~~Cache crawled reviews~~ (done: SQLite TTL cache).
- Anchor "before" on the real release date, not the first Steam post (NEXT shipped 24 Jul 2018;
  its first post is 26 Jul, so its "before" half catches the release spike).
- A small human-labelled set to measure classifier accuracy (today only cross-model agreement is
  measured).
- Cross-validate recent windows with Reddit/YouTube.
- A second demo title with lower review velocity.
