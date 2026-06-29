# Product Sentiment Journey Tool — Project Overview

*A reference for explaining this project end-to-end: what it is, how it's built, the decisions
behind it, and where it's going. Companion to `PLANNING.md` (working notes) and `README.md` (quick
start).*

---

## 1. One-line pitch

A **retrospective sentiment-through-time engine** for consumer products (games first): given a
product, it reconstructs how public approval moved across its entire lifetime and timestamps each
shift against the events — patches, updates, releases — that caused it. It answers *"did this
product win people back, when, and over what?"* — not "what will happen next."

**Hero example — No Man's Sky:** infamous 2016 launch at ~47% approval → ~91% today, the full
redemption arc reconstructed from public data in one view, with the specific updates that moved it.

---

## 2. The problem and how this is positioned

Social-listening / market-research tools (Brandwatch, Talkwalker, Sprinklr, Meltwater) are **broad
and shallow** by economic necessity: many platforms, generic sentiment, enterprise pricing. LLMs
have since commoditized sentiment classification — "we do sentiment, but with AI" is no longer a
moat.

This tool is deliberately the opposite shape — **narrow and deep**:

| Generic social listening | This tool |
|---|---|
| Sentiment score over a mention firehose | Feature-level **expectation gap** on a product's event timeline |
| Twitter/news/blogs | Structured, domain-specific sources (Steam reviews, SteamSpy, ITAD, IGDB) |
| "Brand health" number | *Why* sentiment moved + how divided the audience is |
| Built for enterprise marketing | Built for the question an indie dev / PM actually asks post-launch |

**The differentiator is the analytical frame, not the sentiment tech.** Anyone can produce an
approval number. The value is reconstructing the *journey* — anchored to real events, explained by
real drivers, in a vertical deep enough to have signals the horizontal tools ignore.

---

## 3. System architecture

**Golden rule (from the design docs):** the backend proxies every external call; the browser never
holds an API key. Today there is no browser at all — the tool is a batch CLI that emits a
self-contained artifact — but the key-handling discipline is already in place.

```
                       scripts/build_timeline.py  (CLI entry)
                                   │
                                   ▼
        app/pipeline/timeline.py   ── orchestration ──┐
            │                                          │
   ┌────────┼───────────────┬──────────────┐          ▼
   ▼        ▼               ▼              ▼     app/measures/  (pluggable
 Steam   SteamSpy   IGDB/Twitch  YouTube/Reddit   Measure protocol; approval first)
 (clients are thin async wrappers; keys live in app/config.py, never in code)
            │                                          │
            ▼                                          ▼
   data/timeline_<appid>.json  ◄── the contract ──►  app/viz.py → out/<appid>.html
```

**Layered, single-responsibility modules:**

- `app/clients/` — one thin async client per source (Steam, SteamSpy, ITAD, RAWG, IGDB/Twitch,
  YouTube, Reddit, DeepSeek). Retry/backoff, one reused connection, key redaction.
- `app/ingest/` — discussion-source ingestion (YouTube wild search today), normalizing everything
  to one item shape: `{text, timestamp, source, confidence, meta}`.
- `app/measures/` — a `Measure` protocol; `approval` is the first implementation. New dimensions
  (extremity, divisiveness, language spread) slot in behind the same interface.
- `app/pipeline/timeline.py` — builds the baseline curve + event-anchored analysis windows.
- `app/viz.py` — renders the timeline JSON to self-contained inline-SVG HTML.
- `app/config.py` — loads keys from `.env` (override) then OS env; reports missing keys.

**The seam that matters:** the **timeline JSON is the contract.** Logic produces it; rendering
consumes only it. That's what makes the future frontend cheap (see §9).

---

## 4. The insight that shapes everything: the historical-data constraint

Retrospective analysis has a hard constraint most people miss: **only Steam reviews carry clean,
complete, timestamped history.** Reddit search is recency-biased (Pushshift, the old historical
archive, is effectively dead); YouTube comments cluster near upload dates and can't be cleanly
date-windowed. Consequences:

- **Steam is the historical spine.** The `appreviewhistogram` endpoint returns up/down review
  rollups across a product's *entire* history in **a single call** — that *is* approval-over-time,
  for free, with full coverage.
- **Reddit / YouTube are present-day enrichment**, not historical reconstruction. Used for recent
  cross-validation and the "why," never asked to rebuild 2016.

Recognizing this up front avoided promising history those APIs can't deliver — an honest scoping
decision that shaped the whole pipeline.

---

## 5. How it works — the pipeline

### 5.1 Baseline curve (free, full history)
`steam.get_review_histogram(appid)` → monthly up/down rollups → an approval point per month across
the product's life. No LLM, no deep crawl. This alone renders the No Man's Sky redemption arc.

### 5.2 Event-anchored analysis windows (the differentiated layer)
1. **Events** come from Steam News (patches, updates) plus a synthetic Launch.
2. **Rolling groups** (`group_events`): walking chronologically, events within `chain_gap_days`
   (default 7) of the previous one are merged into one group; a same-day post blitz *and* a run of
   weekly patches each collapse to a single analyzed window. Per-day frequency is recorded
   (`peak_day_count`) so a 5-posts-in-one-day marketing push is flagged.
3. **Asymmetric window** per group: **1 week before** the burst → **3 weeks after** the last event
   in it. Asymmetric because reviews lag playtime — the reaction accrues for weeks.
4. **Sampling + scoring:** the tool crawls reviews backward (`filter=recent`, adaptive depth) just
   far enough to cover the targeted recent groups, samples the text in each window, and runs the
   approval `Measure`.
5. **Delta:** approval in the week *before* the first event vs the tail *after* the last; the burst
   interior feeds the LLM's view but not the delta halves. Very recent groups whose 3-week tail
   hasn't elapsed are flagged `window_truncated` + `after_days_observed`.

### 5.3 The approval measure — two layers, and why the LLM earns its place
- `approval_steam` — `voted_up` ratio over the window (free, from review metadata).
- `approval_llm` — DeepSeek's read of the review **text**, returned *with*:
  - **drivers** — which features moved approval (e.g. "free updates ↑, technical issues ↓"),
  - **divisiveness** — is a 60% game *calmly mixed* or *violently split*? Same ratio, opposite
    product reality; only the text reveals it.

**This is the answer to "why not just read the Steam score":** the ratio tells you *that* approval
moved; the LLM tells you *why* and *how divided* — which the binary thumbs-up cannot.

### 5.4 LLM-safety architecture
The model **narrates over pre-computed numbers**; it never sees raw API responses or holds a key,
and it can't invent a metric — only describe one the backend already computed. Two-tier model
strategy keeps cost down: a cheap model for high-volume classification, a capable model reserved
for synthesis.

---

## 6. The hard problem: cross-source entity resolution

Every source must reconcile to one canonical key — the **Steam appid**. This is the classic
record-linkage problem, and the key realization is that it splits into two tiers of very different
difficulty:

**Tier 1 — deterministic (keyed).** Resolved by maintained ID cross-references, not guessing:
- ITAD: `lookup?appid=` → UUID.
- RAWG: Steam store URL in its data → regex the appid.
- **IGDB is the resolution hub:** `external_games` maps Steam appid ⟷ IGDB id *bidirectionally*
  (verified: appid 275850 ⟷ IGDB 3225). And **Twitch owns IGDB**, so a Twitch category → IGDB id →
  appid is also deterministic.
- IGDB `websites` even hands us the official handles: verified that NMS resolves to
  `r/NoMansSkyTheGame` and `@HelloGamesTube` directly. (We classify those links by URL domain —
  IGDB's `category` enum is mid-deprecation and unreliable.)

**Tier 2 — probabilistic (the genuinely subjective part).** Free-text sources with no native game
ID — **Reddit and YouTube**. Strategy = *deterministic seed, precision-first expansion*:
1. **Ground truth** (no matching needed): the official subreddit's comments and the dev/publisher
   channel's video comments are ~100% on-topic by construction.
2. **Wild expansion** (needs classification): general subs / general search → a **string layer**
   (canonical name + IGDB `alternative_names` + abbreviations, minus an exclusion list to kill
   collisions like "Hades" vs "Hades II") → an **LLM/embedding classifier** that scores each
   candidate against the ground-truth seed → a stored **confidence**.
3. **Confidence is a per-measure knob:** a sentiment *trend* tolerates noise that averages out;
   *driver extraction* must lean on ground truth + high-confidence items only.

A future `resolver` module persists this as a `GameEntity` in a SQLite **entity/alias map** —
resolve once, cache, allow manual override.

---

## 7. Key engineering decisions and trade-offs (the "why" talking points)

- **Steam appid as the canonical anchor, IGDB as the bridge.** Turns an N×N source-matching problem
  into N one-to-one lookups against maintained cross-references.
- **Event-anchored rolling windows over fixed calendar buckets.** Review volume swings from
  thousands/week at launch to a trickle later, so fixed windows are wrong; bursts of closely-spaced
  patches can't be disentangled anyway, so merging them is more honest than faking per-patch deltas.
- **`.env` overrides OS env on purpose.** A running shell's user-scope environment can be stale;
  pinning keys in a gitignored `.env` keeps the project reproducible. (A real lesson learned, not a
  default.)
- **Cost is treated as a first-class constraint.** Steam has no "reviews between A and B" endpoint,
  so historical text needs a backward crawl whose depth scales with review velocity (NMS ≈ 95/day →
  ~90s to reach ~10 weeks). The free histogram carries full history; the LLM runs only on the
  reachable recent windows; deep-history backfill is a cacheable batch job, scoped for later.
- **Graceful degradation by design.** Each source has a fallback path; the dashboard degrades (e.g.
  "price unavailable") rather than hard-failing on one API.
- **Honest scoping as a feature.** Twitter dropped (API priced out), sales numbers reframed as
  hype-vs-reception, Reddit/YouTube positioned as present-day enrichment — engineering maturity over
  feature greed.

---

## 8. What is built and verified today

| Capability | Status |
|---|---|
| 8-source async API layer (key-safe, smoke-tested) | ✅ 7 live; Reddit pending creds |
| Full-history approval curve (Steam histogram) | ✅ NMS: 119 months, 47% → 91% |
| Rolling event groups + asymmetric (7d/21d) windows | ✅ verified on NMS |
| Per-day event-frequency flagging | ✅ e.g. "5 posts on 2026-05-27" |
| Approval measure (Steam ratio + LLM drivers + divisiveness) | ✅ distinct drivers per window |
| Self-contained HTML visualization | ✅ curve + window bands + driver cards |
| Entity resolution: appid ⟷ IGDB ⟷ official handles | ✅ verified (275850 ⟷ 3225 → handles) |
| YouTube wild ingestion (top comments, name search) | ✅ 10 videos → 150 comments |
| **Agentic query layer** (NL question → grounded answer) | ✅ v1 — tool-calling loop, provenance, verified on NMS |
| Version control | ✅ clean repo, secrets gitignored |

**Concrete verified run (No Man's Sky):**
- 2026-05-27 update (5 same-day posts): window 05-20→06-17, +1.6pts, drivers = exploration↑,
  content↑, repetitiveness↓, technical↓.
- 2026-04-08 group (04-08 + 04-14 chained): window 04-01→05-05, drivers = game evolution↑, depth↑,
  VR optimization↓.

---

## 9. Roadmap

**Near term**
- **Topicality classifier** so YouTube/Reddit wild ingestion feeds the timeline (string prefilter →
  LLM/embedding scored against the ground-truth seed).
- **`resolver` module + SQLite entity map** — `GameEntity` with cached, manually-overridable links.
- **Reddit ingestion** once credentials land (official subreddit ground truth first).
- **More measures on the same scaffold:** extremity, divisiveness (already computed — surface it),
  language spread (Steam reviews carry a per-review `language` field — a market-breadth analogue),
  volume/velocity.

**Medium term**
- **Historical LLM backfill** — a one-time cacheable backward crawl to analyze the dramatic early
  windows (2016 launch, 2018 NEXT recovery) where the big deltas actually live.
- **Thin FastAPI layer** — wrap `build_timeline` as `GET /api/timeline/{appid}`; point a minimal
  frontend at the existing JSON. Introduced when interactivity (type-any-game demo) justifies it;
  presentation moves out of `app/` at that point.
- **Cross-source cross-validation** — compare Steam-derived approval against Reddit/YouTube reads
  for the same window as a confidence signal.
- **Agentic query layer (§10)** — a natural-language agent that orchestrates the backend tools and
  returns grounded KPI summaries, removing the need for a dashboard. The flagship interface goal.

**The generalizable capability underneath** — multi-source entity resolution + aspect-based analysis
+ LLM-safe synthesis — ports to any consumer-feedback vertical (apps, hardware, CPG). The strategy
is to go deep on one vertical first.

---

## 10. The agentic layer (v1 built)

**Vision:** replace the dashboard with a question. The user asks *"What did No Man's Sky's
reception look like 2 years in?"* and an agent resolves the product, figures out the relevant
window, drives the backend, reads the output, and returns a grounded KPI summary plus the chart —
no filters, no clicking, no knowing what an "appid" is.

> **Status — v1 is built and verified** (`app/agent/`, `app/resolver/`, `scripts/ask.py`). The full
> tool surface, the bounded controller (DeepSeek function-calling), and provenance all work. Live
> verification on No Man's Sky: the "2 years in" query returns the real curve (84%→75%→68%); a
> "recently, and why" query drives `analyze_window` to a live LLM read of ~3,000 reviews with
> drivers + divisiveness. Still minimal: the resolver is live-resolution (no SQLite map yet), and
> `analyze_window` is gated to recent windows pending the historical backfill.

### 10.1 Why an agent fits *this* tool specifically
It's the natural extension of the existing safety principle (§5.4): the LLM **narrates over
deterministic, pre-computed numbers**. The agent is a *planner* that can only call a small, fixed
set of key-safe backend tools; every number it reports is a tool result. It never touches raw APIs,
keys, or invents a metric. That makes the agent's reach auditable by construction.

### 10.2 The agent's tool surface (its entire API)
A constrained registry — each entry a thin wrapper over functions we already have, exposed to
DeepSeek as JSON-schema tools for function-calling:

| Tool | Returns | Backs onto |
|---|---|---|
| `resolve_product(name)` | `{appid, igdb_id, canonical_name, launch_date, confidence}` | resolver (§6) |
| `resolve_timeframe(launch_date, phrase)` | `{start, end}` — deterministic date math | helper |
| `get_approval_curve(appid, start?, end?)` | monthly approval points (free) | `build_baseline` |
| `get_events(appid, start?, end?)` | event groups in range | `get_events`/`group_events` |
| `analyze_window(appid, start, end)` | `{approval_steam, approval_llm, drivers, divisiveness, summary, n}` | `analyze_events` + measure |
| `get_discussion(appid, window, source)` *(later)* | classified Reddit/YouTube items | `ingest/` + classifier |

`resolve_timeframe` exists so the **LLM never does brittle date arithmetic** — it passes the phrase
("2 years in", "at launch", "first 6 months"), the backend computes the window.

### 10.3 The agent loop (bounded, cheap-first ReAct)
1. **Parse intent** (LLM): product · timeframe phrase · what's asked (level? trend? *why*? compare?).
2. `resolve_product` → appid + launch_date (carry the confidence; flag if low).
3. `resolve_timeframe` → the absolute window.
4. **Cheap first:** `get_approval_curve` (level + trend) and `get_events` (what happened) — both
   free.
5. **Spend only if needed:** if the question wants the *why*/texture, `analyze_window` for
   drivers + divisiveness (gated, because it costs a crawl + tokens; cached by `(appid, window)`).
6. **Synthesize** a structured answer object and stop. Hard caps: max tool calls (~6) and a token/
   crawl budget per question.

### 10.4 Worked trace — *"What did No Man's Sky's reception look like 2 years in?"*
(Numbers below from `get_approval_curve` are **real**; the `analyze_window` drivers are what that
tool would add once the historical LLM backfill (§9) is run — the free histogram already carries the
hard numbers.)

```
1. resolve_product("No Man's Sky")
   → { appid: 275850, launch_date: "2016-08-09", confidence: 1.0 }   (IGDB-deterministic)
2. resolve_timeframe("2016-08-09", "2 years in")
   → { start: "2018-07-01", end: "2018-09-30" }   (anniversary month, widened to catch the
                                                    proximate major event)
3. get_approval_curve(275850, "2018-06", "2018-10")
   → 2018-07: 84% (n=8,390) · 2018-08: 75% (n=5,517) · 2018-09: 68% (n=615)
     [context vs launch 2016-08: 47% (n=46,992)]
4. get_events(275850, "2018-07-01", "2018-09-30")
   → NEXT update (2018-07-24) — the turnaround event
5. analyze_window(275850, "2018-07-01", "2018-09-30")          (pending backfill)
   → drivers: multiplayer↑, base-building↑, "free post-launch support / redemption"↑,
     lingering launch-promise resentment↓ ; divisiveness ≈ moderate
```

**Synthesized answer:**
> Two years in (Aug 2018), No Man's Sky's reception had transformed. Monthly approval sat at **~75%**,
> up from **47%** at the 2016 launch. The turn came weeks earlier: the **NEXT update (24 Jul 2018)**
> drove a spike to **84%** approval on **~8,400 reviews** — the biggest monthly wave since launch — as
> lapsed players returned. Reviews credited NEXT's multiplayer and base-building and a "they actually
> fixed it" narrative, against residual bitterness about the original launch promises. Net: the
> product was *mid-comeback* — no longer the cautionary tale of 2016, not yet the ~91% it holds today.

### 10.5 The answer object (auditable, not a black box)
`{ headline, metrics[], chart_ref, events_cited[], provenance: tool_calls[], assumptions[],
confidence }`. Provenance lists every tool call and window used, so any claim traces back to a
deterministic result — and `assumptions` records choices the agent made (e.g. "interpreted '2 years
in' as the Aug-2018 month, widened to include NEXT").

### 10.6 Guardrails & cost controls
- **Grounding:** report only tool-returned numbers; if a tool returns nothing, say so — never
  interpolate. *(Observed nuance: when `get_events` returns nothing for an old window, the model may
  still name a known event (e.g. the NEXT update) from world knowledge — it hedges it, but a strict
  "tools-only" mode is a planned config so the agent can be locked to verifiable facts for demos.)*
- **Cheap-first + budgets:** the free histogram answers level/trend; `analyze_window` is invoked
  only when the question needs *why*, and is cached.
- **Clarify-or-default:** ambiguous "reception" defaults to approval + drivers with the assumption
  stated; only genuinely under-specified queries trigger a clarifying question.
- **Confidence propagation:** low entity-resolution confidence is surfaced in the answer rather than
  hidden.
- **Evaluation harness:** golden `question → expected tool calls + expected claims` cases to catch
  regressions in planning and grounding.

### 10.7 How it maps to the codebase
```
app/agent/
  tools.py       the tool registry + JSON schemas (the agent's whole surface)
  controller.py  the bounded tool-calling loop (DeepSeek function-calling)
  answer.py      the answer-object schema + provenance assembly
scripts/ask.py   CLI: python -m scripts.ask "What did No Man's Sky's reception look like 2 years in?"
```
Reuses the resolver (§6), `pipeline/timeline.py`, and `measures/`. The same controller becomes
`POST /api/ask {query}` when the FastAPI layer lands (§9) — at which point **the agent *is* the
interface**, and the "complicated dashboard" never needs to exist.

### 10.8 Open questions / risks
- **Temporal semantics:** is "2 years in" a point or a span? Default = the anniversary month,
  widened to capture a proximate major event; configurable.
- **When to spend:** the planner's call on histogram-only vs invoking `analyze_window` is the main
  cost lever — needs tuning and guardrails.
- **Comparative / multi-product queries** ("vs Elite Dangerous") — a second resolve + parallel
  windows; straightforward but expands the tool contract.
- **Bad tool arguments / hallucinated appids** — mitigated by schema validation and routing all
  identity through `resolve_product` (never letting the LLM supply a raw appid).

---

## 11. What this project demonstrates (interview framing)

**Product thinking**
- Found a defensible wedge (vertical depth + analytical frame) against commoditized horizontal tools.
- Scoped honestly around hard constraints rather than overpromising.
- Designed the output around the *question a user actually asks* ("did the launch land, and why").

**Technical depth**
- Multi-source integration with real entity resolution (deterministic + probabilistic tiers).
- LLM-as-narrator-over-computed-numbers — a safe, production-shaped AI architecture.
- Cost-, rate-, and failure-aware data pipeline (adaptive crawl, caching strategy, graceful
  degradation).

**Engineering maturity**
- Clean layering with a stable JSON contract that makes future frontend/API work cheap.
- Empirical de-risking (verified the histogram endpoint, the IGDB mappings, the ingestion live
  before committing to them).
- Disciplined version control and secret handling from day one.

---

## 12. Tech stack & repo map

**Stack:** Python 3.11 · `httpx` (async) · `python-dotenv` · DeepSeek via the OpenAI-compatible SDK
· inline-SVG HTML (no frontend framework yet).

```
app/
  config.py            keys from .env → OS env; missing-key diagnostics
  clients/             steam, steamspy, itad, rawg, igdb, youtube, reddit, deepseek
  ingest/              youtube (wild name-search ingestion); reddit (planned)
  measures/            Measure protocol + approval (Steam ratio + LLM drivers/divisiveness)
  pipeline/timeline.py baseline curve + rolling event groups + windowed analysis
  resolver/            appid ⟷ IGDB ⟷ official handles + entity map        (planned, §6)
  agent/               tool registry + bounded controller + answer object  (planned, §10)
  viz.py               timeline JSON → self-contained HTML
scripts/
  build_timeline.py    CLI: appid → data/timeline_<appid>.json + out/<appid>.html
  ask.py               agent CLI: natural-language question → grounded answer (planned, §10)
  smoke_test.py        one live call per API
PLANNING.md            working design notes
README.md              quick start
PROJECT_OVERVIEW.md    this document
```

**Run the hero demo:**
```bash
python -m scripts.build_timeline 275850 --name "No Man's Sky"
# → open out/timeline_275850.html
```
