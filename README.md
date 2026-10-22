# Senti-Minted — game market + social-sentiment platform

The centerpiece is the **Product Sentiment Journey Tool**: a retrospective
sentiment-through-time engine. Give it a game; it rebuilds how approval moved
across the game's whole life, finds the updates where it moved most, and reads
a stratified sample of reviews from those windows to say *why* — per aspect,
with evidence. See `TECHNICAL_BREAKDOWN.md` for how every number is made.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env                      # fill in keys (.env is gitignored)
python -m scripts.smoke_test              # one real call per API: OK / FAIL / SKIP

# Web app: search a game, build its journey, browse, ask questions
uvicorn app.api:app --port 8000           # then open http://localhost:8000

# CLI
python -m scripts.build_timeline 275850 --name "No Man's Sky"            # full ($0.14 measured)
python -m scripts.build_timeline 275850 --name "No Man's Sky" --no-llm   # rule-based, free
python -m scripts.build_timeline 275850 --curve-only                     # curve + movement only
python -m scripts.ask "What did No Man's Sky's reception look like 2 years in, and why?" -v
python -m scripts.compare_models 275850 2018-07-19 2018-08-28            # two-tier vs single-model cost

python -m pytest                          # 75 offline unit tests
```

Outputs: `data/timeline_<appid>.json` (the contract) and `out/timeline_<appid>.html`
(self-contained page). On Windows, set `PYTHONIOENCODING=utf-8` for the CLI.

## Build flags

| Flag | Default | Meaning |
|---|---|---|
| `--select movers\|recent\|all` | `movers` | Which event windows get the LLM read: launch + biggest significant moves, newest K, or all |
| `--max-events` | 6 | K windows to analyze |
| `--sample-reviews` | 200 | Reviews sampled per window (stratified by day) |
| `--language` | `english` | Steam language of the text sample (population counts use all languages) |
| `--single-model` | off | Classify on the pro model too (cost comparison) |
| `--no-llm` | off | Lexicon labels + templated drivers, no tokens |
| `--no-llm-cache` | off | Force fresh LLM calls |
| `--before-days / --after-days / --chain-gap-days` | 7 / 21 / 7 | Event window shape |

## HTTP API (`uvicorn app.api:app`)

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | status, which keys are configured (names only), models, cache stats |
| GET | `/api/resolve?q=` | name or appid → `{appid, canonical_name, launch_date, confidence, saved}` |
| GET | `/api/timelines` | saved timelines |
| GET | `/api/timeline/{appid}` | saved timeline, or builds it (`?refresh=true&no_llm=&max_events=&sample_reviews=&select=`) |
| POST | `/api/timeline/{appid}/jobs` | starts a background build → `{job_id}` |
| GET | `/api/jobs/{job_id}` | `{status, log[], summary, error}` |
| POST | `/api/ask` `{question}` | agent answer + provenance (every tool call) + cost |
| GET | `/`, `/view/{appid}` | web page; timeline page with an ask box |

## Layout

```
app/
  config.py          keys from .env (override) then OS env; model tiers; cache path
  cache.py           SQLite per-source TTL cache + hit-rate stats
  clients/           steam (reviews by date range, histogram, official news), steamspy,
                     itad, rawg, igdb, youtube, reddit [creds pending], deepseek (tiers, cost ledger)
  pipeline/
    timeline.py      baseline → events → groups → movement → selection → analysis
    sampling.py      stratified, edit-filtered review sampler for any window
  measures/
    approval.py      stage 1 classify (flash) → aggregate → stage 2 synthesize (v4-pro)
    aspects.py       taxonomy + all sentiment maths + driver validation
    lexicon.py       rule-based fallback labeller
  agent/             tool registry + bounded tool-calling controller
  resolver/          name → appid → IGDB id, launch date
  ingest/youtube.py  wild YouTube comment ingestion (not yet in the timeline)
  api.py, web/       FastAPI layer + single-page UI
  store.py, viz.py   saved timelines; JSON → HTML
scripts/             build_timeline, ask, compare_models, smoke_test
tests/               offline unit tests + NMS events fixture
```

## Key status

| API | Status |
|---|---|
| DeepSeek (`deepseek-flash`, `deepseek-v4-pro`) | ✅ |
| Steam (keyless) | ✅ |
| Twitch/IGDB, YouTube, ITAD, RAWG | ✅ |
| Reddit | ⏳ creds pending |

## Notes

- **Config precedence:** `.env` overrides OS env on purpose — the machine's
  user-scope env can be stale for an already-running shell.
- **Models:** override with `DEEPSEEK_FAST_MODEL` / `DEEPSEEK_PRO_MODEL`. Prices
  live in `app/clients/deepseek.py` with the date they were read; update both
  when DeepSeek changes them.
- **Cache:** `.cache/senti.sqlite`; `SENTI_CACHE=0` disables it.
- **Cost exposure:** `/api/ask` and LLM builds spend money and have no auth. Fine
  on localhost; put auth and a rate limit in front before exposing it.
