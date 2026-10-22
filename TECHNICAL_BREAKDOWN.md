# Senti-Minted: Technical Breakdown

*What the code does, step by step, and exactly how the sentiment analysis works. Written 22 Oct
2026 against the code at this commit. Every figure quoted here comes from a recorded run (§11) or
a live check made while building it; nothing is projected.*

Companion docs: `README.md` (how to run), `PLANNING.md` (design notes), `PROJECT_OVERVIEW.md`
(product framing, interview narrative).

---

## 1. What it does, in one paragraph

Given a Steam game, Senti-Minted rebuilds how its public approval moved across its whole life and
ties each shift to the developer's own announcements. It draws the monthly approval curve from
Steam's review histogram. It groups the developer's announcements into event windows and measures
exact before/after approval for every window. It then spends LLM money only on the launch and the
windows where approval moved most. For those, it draws a stratified sample of ~200 reviews and runs
**aspect-based sentiment analysis** in two stages. A cheap model labels every review. Plain Python
turns the labels into the numbers. A stronger model writes the explanation, and its claims are
checked against those numbers. A tool-calling agent answers questions in plain English over the
same functions, and a FastAPI layer with a small web page puts it all behind one URL.

**Headline numbers (No Man's Sky, appid 275850):**
- Approval curve: 123 months, **47% → 90%**. 154 official posts → **93 event groups**, **28** with a
  significant before/after move.
- 6 windows read (launch + the 5 biggest movers) × **200 reviews each**. The whole build cost
  **$0.137**; an immediate re-run cost **$0** at a **99.8%** cache hit rate.
- The **two-tier** model split is **10.3× cheaper** than running both stages on the capable model,
  with the same answers (text approval 80.8% vs 80.6%, same 4 drivers).
- What the aspect data shows: from launch to late 2018, trust in the developer goes from neutral
  (impact −0.02) to the dominant positive driver (+0.62). The broken-promises complaint fades
  (−0.32 at its worst → −0.05). Divisiveness falls from **0.78 to 0.18**.

---

## 2. System map

```
 Browser (app/web/index.html, /view/{appid})
     │  fetch: /api/resolve, /api/timeline/*/jobs, /api/jobs/*, /api/ask
     ▼
 app/api.py (FastAPI) ───────────────► app/agent/controller.py ──► app/agent/tools.py
     │ background job                     (DeepSeek tool-calling,       │
     ▼                                     ≤8 steps, provenance)         │
 app/pipeline/timeline.py  build_timeline ◄────────────────────────────────┘
   1 build_baseline ─────────► steam.get_review_histogram           (1 request, full history)
   2 get_events ─────────────► steam.get_news(feeds=official)       (1 request, all posts)
   3 group_events                                                   (pure Python)
   4 measure_movement ───────► steam.get_window_summary ×2 / group  (exact counts, no bodies)
   5 select_windows                                                 (pure Python)
   6 analyze_window ─┬───────► pipeline/sampling.sample_window ───► steam.get_reviews_page (date range)
                     └───────► measures/approval.ApprovalMeasure
                                  stage 1  deepseek-flash  (classify, batches of 20)
                                  aggregate measures/aspects.py   (all the numbers)
                                  stage 2  deepseek-v4-pro (synthesize + validate)
                                  fallback measures/lexicon.py
     ▼
 app/store.py → data/timeline_<appid>.json (the contract) + out/timeline_<appid>.html (app/viz.py)

 app/cache.py: every external read above goes through a SQLite cache with per-source TTLs.
```

The **timeline JSON is the contract**. The pipeline produces it; the CLI, the API, the page and the
agent only read it or call the same functions.

---

## 3. Data sources

| Source | Endpoint | What we take | Cache TTL |
|---|---|---|---|
| Steam histogram | `store.steampowered.com/appreviewhistogram/{appid}` | monthly up/down counts, whole history, 1 request | 24 h |
| Steam reviews | `store.steampowered.com/appreviews/{appid}` with `start_date`, `end_date`, `date_range_type=include`, `filter=recent` | review text + vote + edit time for any date range; `query_summary` = exact window counts | 7 d (closed windows), 6 h (windows ending in the last 2 days) |
| Steam news | `api.steampowered.com/ISteamNews/GetNewsForApp/v2/?feeds=steam_community_announcements&count=1000` | the developer's own posts, with a 400-char plain-text blurb | 6 h |
| RAWG | `/games?search=`, `/games/{id}/stores` | name → Steam appid (from the store URL) | 30 d |
| IGDB (Twitch OAuth) | `external_games`, `games` | appid → IGDB id, display name for a bare appid | 30 d |
| DeepSeek | `chat/completions`, JSON mode | labels and synthesis (§5) | 30 d (keyed by model + prompt) |
| ITAD, SteamSpy, YouTube, Reddit | — | clients built and smoke-tested; **not used by the timeline yet** (Reddit credentials pending) | ITAD 1 h |

### 3.1 Three things found about Steam while building this

1. **Steam's review endpoint takes a date range.** It isn't in the public docs, but the store's own
   histogram uses it: `start_date`, `end_date` (Unix seconds), `date_range_type=include`. Checked
   on NMS: a query for 9–16 Aug 2016 returns reviews from that week and a `query_summary` covering
   that week only (27,388 reviews). Paging with the cursor stays inside the range (300 reviews,
   all from 13 Aug 2016). This removed the old constraint that historical text needed a slow
   backward crawl, so any window since launch can now be analysed.

2. **Old reviews have been edited, so their text isn't from the time they were written.** Steam
   serves each review's *current* text and vote. Of 500 NMS reviews written on launch day (13 Aug
   2016), **131 (26%) were edited more than 30 days later**. **80%** of those edited reviews are now
   positive, against **60%** of the unedited ones. People who came back after the updates rewrote
   their launch reviews. Reading those as launch-day opinion would make launch look kinder than it
   was. The sampler therefore excludes any review edited more than 7 days after its window closed
   (§4.6).

3. **The histogram and date-range counts disagree.** For August 2016 the histogram shows 46,992
   reviews at 47% approval. A date-range query for the same month (by creation date) returns 49,422
   at 56%. The likeliest explanation is that the histogram files each review under its *last edit*
   date: edited launch reviews (mostly positive) move out of August 2016, which lowers that month's
   approval. The edit-filtered sample of the launch window comes out at **44%** approval (English,
   200 reviews, §11), close to the histogram. The app therefore uses the histogram for the long-run curve and creation-date
   counts for window statistics, and states which is which.

---

## 4. The pipeline, step by step (`app/pipeline/timeline.py`)

### 4.1 Baseline curve: `build_baseline`
One histogram request gives a point per month: `{date, up, down, n, approval = up/n}`. For NMS that's
123 months, from 47% (Aug 2016) to 90% (Oct 2026). It costs nothing and covers the whole history.

### 4.2 Events: `get_events`
- Only the **official announcements feed**. Without the filter, 37 of NMS's latest 50 "news" items
  were press coverage (PC Gamer, Rock Paper Shotgun, PCGamesN, Russian aggregators). Those are other
  people writing about the game, not things the developer did, so they don't belong on an event
  timeline. With the filter and `count=1000`, all 154 official posts back to 7 Aug 2016 come back.
- **De-duplication** on (date, lower-cased title): the 2016 posts were cross-posted twice, so 154
  becomes 130.
- **Type** by keyword in the title (`update`, `patch`, `fix`, `version`, `release`, `expedition` …) →
  `patch`, else `announcement`; plus a synthetic **`launch`** event at the histogram's start date
  (12 Aug 2016 for NMS).

### 4.3 Grouping: `group_events`
- **Rolling chain:** walk the events in time order. Each event joins the current group if it lands
  within `chain_gap_days` (7) of the group's latest event; otherwise it starts a new group. A
  same-day blitz and a run of weekly patches both collapse into one group. Patches a week apart
  can't be told apart in review data anyway, so one group per cluster is the honest unit.
- **Asymmetric window:** `[first event − 7 days, last event + 21 days]`. The after side is longer
  because reviews come in after people have played. Each event that joins pushes the tail out, so
  the latest event still gets its full 21 days.
- **Anchor** (label and marker) is the most informative member (launch > patch > announcement),
  earliest first. `peak_day_count` flags blitzes such as five posts in one day.
- NMS: 131 events → **93 groups**.

### 4.4 Movement: `measure_movement` (free, every group)
Two count-only requests per group (`num_per_page=0`, all languages):
- **before** = reviews created in `[window start, first event)`;
  **after** = `(last event, window end]`. The burst in between is left out of both.
- `delta = approval_after − approval_before`.
- **Two-proportion z-test:** pooled `p = (up_b + up_a)/(n_b + n_a)`,
  `z = (p_a − p_b) / sqrt(p(1−p)(1/n_b + 1/n_a))`. `significant` = |z| ≥ 2.
- NMS: **28 of 93** groups show a significant move.

### 4.5 Selection: `select_windows`
- `movers` (default): always the **launch** group, then the groups with the largest |delta| among
  those with ≥30 reviews on each side and |z| ≥ 2, up to K (6).
- `recent`: the K newest (the old behaviour). `all`: every group.
- Each group records `selection_reason`, e.g. `moved +24.3 pts (z=16.17)`.
- This puts the LLM spend where the curve actually bends, not on whatever happened most recently.

### 4.6 Sampling: `pipeline/sampling.py`
Goal: ~200 reviews that represent the window, read as they were written at the time.
1. **Strata** = days (multi-day slices when the window is long, at most 31 strata).
2. **Pass 1:** one request per stratum. It returns the stratum's exact review count (in the sample
   language) and a first page of reviews.
3. **Proportional allocation** of the 200 slots by stratum size, using largest-remainder rounding
   so the slots add up exactly: a launch day with 8,000 reviews gets many slots, a quiet Tuesday
   few.
4. **Eligibility:** text ≥ 20 characters, and **not edited more than 7 days after the window
   closed** (§3.1).
5. **Pass 2:** strata whose first page didn't hold enough eligible reviews page further (≤6 pages
   per stratum in total).
6. **Seeded random subsample** inside each stratum (Steam returns newest first, so taking the first
   N would favour late-day reviews). The seed is `appid:start:end:language`, so a re-run draws the
   same reviews.
7. **Stats recorded per window:** strata, population, fetched, excluded as too short, excluded as
   edited later, `edited_later_share`, shortfall.

The text sample is **English** by default (`--language`); the population counts use all languages.
English keeps the lexicon fallback and the exemplar quotes readable. The classifier itself can
read any language.

### 4.7 Scoring
`ApprovalMeasure.compute(reviews, context)`, covered in §5. The context passed to it is the game
name, window dates, member event titles, the anchor post's blurb, and the movement numbers.

### 4.8 Output
`build_timeline` returns (and `store.save` writes) one JSON document: `baseline`, `events` (every
group with `movement`, `selected`, `selection_reason`, and `analysis` if read), `llm_usage` (the run's
ledger), `cache` (hit/miss by source), `method`, `degraded`, and the selection and sampling settings.

---

## 5. Sentiment analysis, in detail

### 5.1 What "sentiment" means here: five separate signals
| Signal | Source | Field |
|---|---|---|
| **Vote approval** | Steam thumbs up/down, exact counts | `approval_population`, `movement.approval_before/after` |
| **Text approval** | Stage-1 label of the review text | `approval_text` |
| **Divisiveness** | Spread of text polarity | `divisiveness` |
| **Aspect sentiment** | Stage-1 aspect labels, aggregated | `aspects[]` |
| **Drivers** | Highest-impact aspects, explained by stage 2 | `drivers[]` |

The thumbs-up ratio says *that* approval moved. The other four say *what* people were reacting to
and *how split* they were, which the ratio can't.

### 5.2 Method: aspect-based sentiment analysis (ABSA)
Plain sentiment analysis gives one polarity per text. ABSA gives polarity per *aspect* the text
discusses. "Gorgeous planets, but it crashes every hour" is positive on visuals and negative on
performance. For a product team the second form is the useful one.

### 5.3 The taxonomy: a closed set of 14 aspects (`measures/aspects.py`)
`gameplay, content, exploration, story, progression, multiplayer, visuals, audio, performance,
ui_controls, developer, expectations, value, other`, each with a one-line definition sent to the
model.

**Why closed:** if the model invents labels freely ("exploration", "Exploring planets",
"discovery"), they can't be counted across reviews or compared between windows. The set is
general to games, not specific to NMS. `developer` (updates, support, trust) and `expectations`
(hype, promises vs delivery) are there for this product's story, where reception is driven by
the post-launch relationship as much as by the game. Specific wording is kept separately, as a
free-text `detail` per mention ("base building", "frame drops"), and shown alongside the counts.

### 5.4 Stage 1: per-review classification
- **Model:** `deepseek-flash`, **thinking off**, temperature 0, JSON output mode.
- **Batching:** 20 reviews per call, each cleaned (BBCode stripped, whitespace collapsed) and cut to
  600 characters. Up to 4 calls run at once.
- **Per review, the model returns:**
  - `overall`: positive / negative / **mixed** / neutral, judged on the **text**;
  - `substantive`: false for jokes, memes, ASCII art, off-topic or too-vague text;
  - `aspects`: 0–3 of `{aspect (taxonomy key), detail (2–5 English words), sentiment
    (positive/negative/neutral), confidence 0–1}`.
- **Prompt rules:** judge the words, not any score; read sarcasm for what it means; reviews can be
  in any language, but details are written in English; list only aspects actually discussed.
- **The thumbs-up is left out of the prompt.** This keeps the text label independent of the vote,
  so `vote_text_mismatch` (§5.5) measures something real: a "Recommended" review whose text is
  negative is a grudging recommendation, and the app can now count those.
- **Prompt caching:** the system prompt (instructions + taxonomy) is identical for every batch and
  goes first, so DeepSeek's automatic prefix cache can bill repeated prefixes at the cache-hit rate.
- **Validation (`normalize_label`):** unknown aspect keys become `other`; invalid sentiments are
  dropped; confidence is clamped to [0, 1]; one mention per aspect per review (the most confident);
  an invalid `overall` makes the review unlabelled. Mapping is by review number, so a missing item
  leaves only that review unlabelled. A failed batch (an API error, or JSON that can't be repaired
  after one retry) comes back as an error object; the window degrades only if *every* batch fails.
- **JSON repair (`parse_json`):** handles code fences, stray backticks inside the JSON (the flash
  model returned `{"ok":` `` `true` `` `}` during testing) and prose around the object.

### 5.5 Aggregation: where every number comes from (`aggregate`, plain Python)
Let *S* be the substantive, labelled reviews in the window, n = |S|.

| Metric | Formula | Reading |
|---|---|---|
| `text_polarity` | share of S whose `overall` is positive / negative / mixed / neutral | — |
| `approval_text` | pos / (pos + neg) | approval among reviews that take a side in their text |
| **`divisiveness`** | **2 × min(p_pos, p_neg)** | 0 = consensus, or everyone calmly mixed; 1 = half love it, half hate it, nobody in between |
| `vote_text_mismatch` | share of S with a positive/negative text whose vote points the other way | grudging recommendations and "thumbs down, but…" |
| aspect `mentions` | reviews in S mentioning the aspect with confidence ≥ 0.5 | — |
| `share_of_reviews` | mentions / n | how much people talked about it |
| `positive_share` | pos / (pos + neg) for the aspect | how they felt about it |
| `net` | (pos − neg) / mentions | — |
| **`impact`** | **(pos − neg) / n** | net push on the window per substantive review; driver ranking uses this |
| `contested` | 2 × min(pos, neg) / (pos + neg) | how split opinion is on this one aspect |
| `playtime_median_h` | median hours played at review time, by vote | e.g. whether negatives came from people who quit early |

**Why divisiveness is defined this way.** A 60%-approval window can be two very different things,
and the formula separates them:
- calmly mixed (pos 30%, neg 20%, mixed 50%) → **0.40**;
- sharply split (pos 50%, neg 45%, mixed 5%) → **0.90**;
- consensus (pos 90%, neg 10%) → **0.20**.

It is the size of the smaller camp, doubled, so it only reaches 1 when the two camps are equal and
nobody is in the middle. The older version had the LLM estimate a "divisiveness" number directly,
which nobody could check. This one can be recomputed by hand from the labels.

**Why impact ranks the drivers.** An aspect that 40% of reviews praise outranks one that 5% hate
intensely. `impact` combines how often an aspect is raised with which way it leans, so the
ranking reflects what actually moved the window.

### 5.6 Driver candidates and exemplars
- **Candidates:** aspects other than `other` with mentions ≥ max(3, 3% of n) and non-zero impact,
  ranked by |impact|; top 6.
- **Exemplars:** for each candidate, the most confident review on its dominant side, then the most
  confident on the other side. That's up to 12 quotes of ≤280 characters. The synthesizer sees
  real voices on both sides, not only the majority.

### 5.7 Stage 2: synthesis
- **Model:** `deepseek-v4-pro`, **thinking on**, JSON output mode, one call per window.
- **Input:** only computed data: window dates, the event titles and the anchor post's blurb,
  before/after population approval, the sample statistics, the top 10 aspects table, the
  candidates and the exemplars. It never sees the raw sample.
- **Rules:** drivers must come from the candidate list; a driver's direction must match the sign
  of its impact; use only numbers that appear in the input, written as whole percentages; ground
  each driver's "why" in exemplars and cite their ids.
- **Output:** up to 4 `{aspect, direction, why, evidence_ids}`, a 2-sentence `summary`, and a
  one-sentence `tension` naming the main disagreement.

### 5.8 Guardrails: checks applied after the model answers
1. **Driver validation (`validate_drivers`):** a driver is **dropped** if its aspect isn't a
   candidate, or if its direction contradicts the computed impact. Dropped drivers are listed in
   `dropped_drivers` with the reason. Evidence ids that weren't in the exemplar set are removed.
2. **Weights come from the data.** A driver's `weight` is |impact| from §5.5, and its `mentions` and
   `positive_share` come from the table. The model only supplies the wording.
3. **Number grounding (`ungrounded_numbers`):** every percentage in the prose (summary, tension,
   whys) is checked against every 0–1 share and 0–100 number in the input, with ±1 point
   tolerance. Anything that doesn't match is listed in `ungrounded_numbers` and flagged on the
   card.

This is the concrete meaning of "the LLM narrates over pre-computed signals". Any number on a card
can be traced to a count, and any driver the counts don't support is removed.

**What isn't checked:** non-percentage figures in prose, and whether the "why" sentence matches
the quotes it cites. Those are reviewed by a person, not enforced by code.

### 5.9 Degraded path: the rule-based fallback (`measures/lexicon.py`)
Used when the key is missing, when every stage-1 batch fails, or by choice with `--no-llm`. It
returns labels in exactly the stage-1 shape, so the same aggregation code runs either way.
- **Aspect detection:** keyword lists per taxonomy key (e.g. performance: bug, crash, fps, stutter,
  optimisation…).
- **Aspect polarity:** opinion words (≈80 positive, ≈80 negative, chosen for games) within 4 tokens
  of the keyword **and in the same clause**. Clause boundaries are punctuation and contrast words
  (but, however, though, although, yet, except, whereas). Without them, "the graphics are beautiful
  but the game crashes" cancelled itself out (found by a unit test). A keyword that is itself an
  opinion word ("buggy", "lied") counts toward its own aspect.
- **Negation:** "not", "never", "no", "n't"… within the 3 tokens before an opinion word (same clause)
  flips it.
- **Overall:** the sign of the whole review's opinion-word total; if it is zero, the Steam vote.
- Confidence is a flat 0.6. Drivers are the top two positive and top two negative aspects by
  impact, and the summary is a fixed template ("Rule-based read (no LLM): …").
- **Partial failure:** if stage 1 succeeds but stage 2 fails, the paid-for LLM labels are kept and
  only the prose is templated.
- The window is marked `degraded: true` with `degraded_reason`. The page shows a banner, and the
  timeline's top-level `degraded` is true if any window degraded.
- **Verified both ways on NMS (2 windows, 60 reviews each):** with the key blanked, and with an
  invalid key (a real 401 from DeepSeek), both runs completed with `degraded: true`,
  `method: lexicon`, $0 spent, and drivers taken from the aspect table. Provider error text is
  scrubbed of key fragments (the 401 message echoes `****` plus the key's last characters) before it
  is stored or returned.
- **Not comparable with LLM numbers:** the lexicon has no "mixed" class, so its divisiveness runs
  high (0.96 on the launch window in the 60-review degraded test, against 0.78 from LLM labels on
  the 200-review run). Use degraded windows for
  direction and aspect ranking, not for absolute divisiveness.

### 5.10 Two tiers vs one model: measured
`scripts/compare_models.py`, NEXT window (19 Jul – 28 Aug 2018), the **same** 200-review sample for
both modes (review pages cached), LLM cache bypassed so every call was paid for, off-peak prices,
22 Oct 2026:

| | Two-tier | Single model | Ratio |
|---|---|---|---|
| Classify (10 calls) | flash, thinking off: 18,090 in / 13,947 out, **$0.0107** | v4-pro, thinking on: 18,850 in / 88,338 out (68,692 reasoning), **$0.1861** | **17.4×** |
| Synthesize (1 call) | v4-pro: 3,973 in / 3,026 out, $0.0086 | v4-pro: 4,117 in / 4,648 out, $0.0119 | 1.4× |
| **Total per window** | **$0.0193** | **$0.1980** | **10.3×** |
| Summed LLM latency | 91 s | 957 s | |
| Text approval | 80.8% | 80.6% | Δ 0.2 pts |
| Divisiveness | 0.337 | 0.343 | Δ 0.006 |
| Drivers | developer+, performance−, exploration+, content+ | same four (different order) | 4/4 |
| Top-5 aspects overlap | | | 4/5 |

**Where the 10× comes from.** Two factors multiply. Price: v4-pro costs 4.4× flash per input token
and 3.3× per output token. Volume: with thinking on, the pro model wrote 6.3× more output tokens
to classify the same reviews, mostly reasoning. Per-review labelling doesn't need step-by-step
reasoning; writing the summary does, which is why only stage 2 has thinking on.

**What this shows and what it doesn't.** It shows the cheap path gives the same window-level
numbers as the expensive one on this window. It doesn't show either is *correct*: two models can
agree and both be wrong. That needs a hand-labelled set (§12). The baseline here is "everything on
the capable model with thinking on", as the handoff specified. Against v4-pro with thinking *off*
for classification, the gap would be roughly the price ratio (3–4×); that wasn't measured.

---

## 6. Cost accounting (`clients/deepseek.py`)
- Prices are stored in code with the date they were read: **DeepSeek, 22 Oct 2026, USD per 1M
  tokens at peak**:

  | Model | Input (cache hit) | Input (cache miss) | Output |
  |---|---|---|---|
  | `deepseek-flash` | $0.006 | $0.30 | $1.20 |
  | `deepseek-v4-pro` | $0.044 | $1.32 | $3.96 |

  Off-peak is half. Peak is 01:00–04:00 and 06:00–10:00 UTC on weekdays. `cost_usd` works out which
  applies from the time of each call; Chinese public holidays (always off-peak) aren't modelled.
- Model names: `deepseek-chat` and `deepseek-reasoner` are now legacy names, **both served by
  `deepseek-flash`** (without and with thinking). Checked with live calls on 22 Oct. So the old
  config's "two-tier" split was really one model with two modes. The tiers are now named models
  (`DEEPSEEK_FAST_MODEL`, `DEEPSEEK_PRO_MODEL` override them).
- Every call is recorded in a `UsageLedger`: stage, model, thinking on/off, prompt tokens
  (cache hit/miss), completion and reasoning tokens, cost, latency, and whether it came from our own
  cache. Cached calls count as calls but cost $0. The ledger is summarised per window
  (`analysis.usage`), per run (`llm_usage`) and per question (`/api/ask` → `usage`).

---

## 7. The agent (`app/agent/`)
- **Controller:** DeepSeek tool calling on `deepseek-flash` with **thinking off**. In thinking mode
  with tools, DeepSeek requires the reasoning content to be sent back each turn; non-thinking
  avoids that. Max 8 steps. Every tool call and result is kept as `provenance`, and the cost of
  the question (including any `analyze_window` spend) comes back as `usage`. A context variable
  keeps concurrent API questions from mixing their ledgers.
- **Tools (its entire surface):** `resolve_product`, `resolve_timeframe` (deterministic date maths:
  "at launch", "first N months", "N years in", "recently", "last N months"), `get_approval_curve`,
  `get_events`, `analyze_window`.
- **Changes in this build:** `analyze_window` now works for **any window since launch** (up to 366
  days), sampling 120 reviews through the same sampler and measure. It returns a compact view:
  population and text approval, divisiveness, top aspects, drivers and summary. A rule-ordering bug
  in `resolve_timeframe` is fixed: "two years after launch" contains the word "launch" and used to
  match the launch-window rule first.
- **Grounding rules in the system prompt:** call `resolve_product` first; never compute dates; state
  only numbers tools returned; say plainly when a tool returns nothing.

## 8. API and web page (`app/api.py`, `app/web/index.html`)
- Endpoints are listed in the README. Builds that take minutes run as **background jobs**
  (`asyncio.create_task`) with a progress log the page polls every 1.5 s; one job per appid at a
  time. `GET /api/timeline/{appid}` serves the saved copy unless `refresh=true`.
- Keys never leave the server. `/health` reports which keys are configured, as booleans by name.
- The page: search a game (resolved via RAWG; appid, launch date and confidence shown, with a warning
  when confidence is low) → build or open the saved one → `/view/{appid}`. That page is the chart
  with an ask box that sends questions about this game to `/api/ask` and shows the answer with a
  collapsible tool trace and its cost.
- The chart: monthly approval line (hover a month for n), event ticks coloured by significant
  rise/fall (hover for delta, z, n), gold dashed lines on analysed windows linking to their cards.
  Each card shows population approval, before→after delta with z, text approval, divisiveness,
  vote≠text rate, summary, tension, drivers with weight and one quote, aspect bars (green positive,
  red negative, grey neutral; hover for top details) and a footer with sample stats and the
  window's LLM cost.

## 9. Caching (`app/cache.py`)
- One SQLite file (`.cache/senti.sqlite`, gitignored). Key = `source:endpoint:sha256(params)`; value =
  JSON; per-row TTL. Wired in with a decorator on client functions (`@cached_call("rawg")`) or
  `cached(...)` for review pages, so call sites didn't change.
- Review pages for windows that ended more than 2 days ago are cached 7 days; open windows 6 hours.
  The histogram, news and metadata use the TTLs in §3.
- LLM calls are cached by (model, thinking, exact messages) for 30 days, so a re-run with the same
  sample costs nothing; `--no-llm-cache` bypasses this for measurement runs.
- A broken or missing cache file never breaks a run: reads fall back to the network, and write
  errors are ignored.
- Hit and miss counts are kept per source and saved in each timeline as `cache`.

**Measured on NMS:** the first full build hit 187 of 432 lookups (43%), because the comparison run
had already cached some review pages. An immediate re-run hit **431 of 432 (99.8%)**, took **1.3 s**
and cost **$0** (all 66 LLM calls served from cache). The one miss was an open window's count query
whose end was "now"; that time is now rounded to the hour so it caches too.

**DeepSeek's own prefix cache** is separate from ours: in the full build, 15,360 of 104,435
classification prompt tokens (15%) were billed at the cache-hit rate, because the system prompt
goes first and is identical for every batch.

## 10. Tests (`tests/`, `python -m pytest`)
75 offline tests, no key or network needed (an autouse fixture blanks the key and points the cache
at a temp file):
grouping (chaining, asymmetric windows, anchors, blitz flag, the NMS fixture's 93 groups and launch
window), timeframes (every phrase form, including the fixed "after launch" case), cache (TTL
expiry, hit/miss stats, bypass, decorator keys, the planned TTLs), lexicon (aspects, negation,
clause boundaries, vote fallback, output shape), aggregation (label normalisation, every formula in
§5.5, candidate floor, driver validation, number grounding, exemplars), sampling (strata,
largest-remainder allocation, eligibility), selection (z-test value, movers ordering and filters),
DeepSeek client (JSON repair cases, peak hours, cost maths, ledger, key scrubbing), the degraded measure, and the
API (health hides key values, saved list, view, 404, 503 without a key, input validation, index).

---

## 11. Results on No Man's Sky
Run of 22 Oct 2026, `data/timeline_275850.json`: 6 windows, 66 LLM calls, 129,893 input / 112,652
output tokens (26,655 reasoning), **$0.1368** (classification $0.064, synthesis $0.073).

### 11.1 The six windows
| Window (selection) | Before → after, all reviews (z) | Sample vote | Text approval | Divisiveness | Vote≠text | Edited-later excluded |
|---|---|---|---|---|---|---|
| Launch, 31 Jul – 10 Sep 2016 (launch) | — → 39.9% | 44% | 43% | 0.78 | 3.2% | 29% |
| Foundation, 18 Nov – 18 Dec 2016 (+24.3) | 50.1% → 74.5% (16.2) | 58% | 60% | 0.59 | 3.6% | 36% |
| Path Finder, 28 Feb – 7 Apr 2017 (+21.1) | 56.5% → 77.6% (4.4) | 68% | 63% | 0.60 | 2.7% | 32% |
| NEXT, 19 Jul – 28 Aug 2018 (−12.5) | 90.9% → 78.4% (−14.9) | 89% | 88% | 0.21 | 1.4% | 19% |
| Abyss + Dev Update 6, 5 Oct – 23 Nov 2018 (+17.2) | 78.6% → 95.8% (8.3) | 91% | 87% | 0.23 | 2.1% | 24% |
| Visions / Dev Update 9, 22 Nov – 20 Dec 2018 (−12.7) | 96.9% → 84.2% (−14.0) | 91% | 90% | 0.18 | 1.5% | 20% |

Every window drew its full 200 reviews (shortfall 0), across 19–30 strata.

### 11.2 Drivers (validated; weight = |impact|)
- **Launch:** expectations − (55 mentions, 12% positive, w 0.23) · value − (43, 12%, 0.19) ·
  performance − (60, 27%, 0.15) · content − (41, 22%, 0.13). Strongest positive aspect:
  exploration (65 mentions, 65% positive). Tension: "some praised the calm, endless discovery…
  others complained all planets look the same."
- **Foundation:** expectations − (67, 12%, 0.32) still the largest force, but developer + (53,
  77%, 0.18) appears, with exploration + and visuals +.
- **Path Finder:** developer + becomes the top driver (92 mentions, 85% positive, 0.35);
  expectations − (77, 19%, 0.25); exploration +; value −.
- **NEXT:** developer + (79, 96%, 0.45) · expectations − (50, 22%) · exploration + (33, 91%) ·
  performance − (37, 19%).
- **Abyss:** developer + (111 mentions, 96%, 0.62) · exploration + · content + · gameplay +. The
  negative candidates (performance −0.08, expectations −0.05) ranked below all four by impact.
- **Visions:** developer + (103, 96%, 0.60) · exploration + · value + (84% positive; it had been
  12% at launch) · content +.

Across all six windows, **0 drivers were dropped** by validation and **0 ungrounded percentages**
were flagged. Some prose quotes counts ("75 of 79 developer mentions"); those aren't checked
automatically (§5.8), and these were correct against the table.

### 11.3 Other signals in the data
- **Edit contamination is large and varies.** 19–36% of fetched reviews were excluded as edited
  after their window; it's highest for the late-2016 and early-2017 windows (29–36%), when there
  was the most reason to come back and rewrite. In the launch window the sample's vote approval
  (44%, unedited, English) sits 11 points under the creation-date population figure (55.5%, all
  languages). Most of that gap fits the edit effect, but language mix contributes an unmeasured
  part.
- **Negative reviewers had played less.** Median playtime at review, voted up vs down: launch
  20.3 h vs 13.6 h; Visions 63.6 h vs 12.2 h.
- **Vote and text rarely disagree.** They point opposite ways in 1.4–3.6% of reviews.

### 11.4 Agent over historical windows
`POST /api/ask` "What did No Man's Sky's reception look like at launch, and why?" → 5 tool calls
(`resolve_product`, `resolve_timeframe("at launch")`, `get_approval_curve`, `get_events`,
`analyze_window(2016-08-12 … 2016-09-26)`), 1 min 53 s, **$0.0225**. The answer cites the curve
(46.9% Aug, 30.8% Sep 2016), text approval 41.4%, divisiveness 0.70, drivers expectations −,
value −, performance −, content −, and exploration as the bright spot, and states how it read
"at launch". Before this build the same question could only answer "needs the historical backfill".

---

## 12. Limitations to state plainly
- **Steam stores only each review's current vote.** Creation-date counts include later changes of
  heart. The edit filter fixes the text *sample*, but the population before/after counts can't be
  filtered by edit date. Both halves of one window are about the same age, so the bias should
  largely cancel in the *delta*, less so in the *level*.
- **Before/after is anchored on Steam post dates, not release dates.** NEXT shipped on 24 Jul 2018,
  but its group's first Steam post is 26 Jul. So that window's "before" half contains the release
  spike, and the measured −12.5 pts is the fade after the honeymoon, not NEXT's effect. A
  release-date anchor (IGDB, or the largest daily review spike) is the fix.
- **No accuracy figure for the labels yet.** What's measured is agreement *between* model setups
  (§5.10), not agreement with human labels. A 100–200 review hand-labelled set would give real
  precision and recall per aspect.
- **The sample is English and substantive-only**, which leaves out short "good game" style reviews
  (more positive), so `approval_text` is approval among reviews with something to say.
- **The z-test treats reviews as independent draws.** Review bombs and Steam sale spikes break that,
  so read z as a ranking signal, not a p-value.
- **The date-range parameters are undocumented** and could change. `iter_reviews` (the old backward
  crawl) is still there as a fallback.
- **The taxonomy is generic to games.** Other verticals would need their own key set (a small
  change: the taxonomy is one dict).
- **Grounding checks cover percentages only** (§5.8).
- **Operational:** jobs live in process memory (lost on restart); `/api/ask` and builds spend money
  and have no auth. Fine on localhost, not for a public deploy.
- **YouTube and Reddit are not in the timeline yet.** Discussion-thread analysis is still Phase 2.

## 13. CV claims, now checked against the code
How the 22 Oct handoff's claim table stands after this build:

| Claim | Status now | Wording the evidence supports |
|---|---|---|
| "FastAPI backend integrating 8 external APIs" | FastAPI built. 8 async clients exist, 7 live (Reddit pending). The FastAPI flows use Steam, RAWG, IGDB and DeepSeek; ITAD/SteamSpy/YouTube are smoke-tested but not yet behind an endpoint | "FastAPI backend over async clients for 8 external APIs (7 live)" |
| "two-tier model strategy… cutting inference cost roughly 10x" | **Measured: 10.3×** per window (17.4× on classification), same outputs | true as stated; cite one measured window |
| "feature-level sentiment across 200+ reviews and 50 discussion threads per title" | 200 reviews **per window** × 6 windows = 1,200 per title, aspect level. **Discussion threads: still not built** | "aspect-level sentiment over 1,200 sampled reviews per title"; drop "50 discussion threads" until Phase 2 |
| "TTL-based caching (1 hour to 30 days per source) and graceful-degradation fallbacks, including a rule-based sentiment path" | **True:** TTLs 1 h (ITAD) to 30 d (RAWG/IGDB/LLM); 99.8% re-run hit rate; lexicon fallback verified with missing and invalid keys | true as stated |
| "the LLM narrates over pre-computed signals… eliminate hallucinated metrics" | Stronger than before: the LLM-estimated approval and divisiveness are gone; drivers are validated; percentages are checked. "Eliminate" overstates it (counts in prose aren't checked) | "…with code-level checks that reject unsupported drivers and flag ungrounded figures" |
| "quantifies the expectation gap between pre-launch hype and post-launch reception at the feature level" | Still not built (no pre-launch data). What exists: the post-launch `expectations` aspect tracked over time | "tracks broken-promises sentiment over a product's life (−0.32 → −0.05 for NMS)"; keep pre-launch for Phase 3 |

## 14. File map
```
app/config.py            keys (.env first), model tiers, cache path
app/cache.py             SQLite per-source TTL cache, hit-rate stats
app/clients/steam.py     histogram, reviews by date range, window counts, official news
app/clients/deepseek.py  tiers, prices, chat_json (repair, retry, cache), UsageLedger
app/pipeline/sampling.py stratified edit-filtered sampler
app/pipeline/timeline.py baseline, events, grouping, movement, selection, analysis, build
app/measures/aspects.py  taxonomy, aggregation maths, candidates, exemplars, validation
app/measures/approval.py stage 1 / stage 2 / fallback orchestration
app/measures/lexicon.py  rule-based labeller
app/agent/               tools + controller
app/api.py, app/web/     FastAPI + page
app/store.py, app/viz.py saved timelines, HTML rendering
scripts/                 build_timeline, compare_models, ask, smoke_test
tests/                   75 offline tests + NMS events fixture
```
