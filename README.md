# Senti-Minted — game market + social-sentiment platform

The centerpiece is the **Product Sentiment Journey Tool**: a retrospective
sentiment-through-time engine (see `PLANNING.md`). The API layer below wires every
external source behind a single backend so the browser never holds a key.

## Quick start — sentiment journey

```bash
python -m scripts.build_timeline 275850 --name "No Man's Sky"
# -> data/timeline_275850.json  +  out/timeline_275850.html (open in a browser)
# --no-llm for the free histogram curve only; see PLANNING.md for tuning flags.
```

Produces a full-history approval curve (Steam review histogram) annotated with
event-anchored windows where DeepSeek explains *why* sentiment moved (drivers),
not just that it did.

## API layer (first pass)

Each integration is a small async client; `scripts/smoke_test.py` proves they all
make valid calls together.

## Layout

```
app/
  config.py          # loads keys from .env (override) then OS env
  clients/
    base.py          # shared httpx client + UA
    steam.py         # appreviews (sentiment) + ISteamNews (patch timeline)  [keyless]
    steamspy.py      # player tags (the grouping axis)                        [keyless]
    itad.py          # IsThereAnyDeal v2: lookup + overview (pricing)
    rawg.py          # metadata + Steam appid extraction (entity resolution)
    igdb.py          # IGDB via Twitch OAuth (release-date milestones)
    youtube.py       # YouTube Data API v3 (optional discussion source)
    reddit.py        # Reddit OAuth (pre/post-launch discussion)  [creds pending]
    deepseek.py      # sentiment LLM (OpenAI-compatible, 2-model strategy)
scripts/
  smoke_test.py      # one real call per API -> OK / FAIL / SKIP
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in keys (.env is gitignored)
python -m scripts.smoke_test
```

## Key status (this pass)

| API       | Key source                              | Status                |
|-----------|-----------------------------------------|-----------------------|
| DeepSeek  | OS env / `.env`                         | ✅ working            |
| Twitch/IGDB | OS env / `.env`                       | ✅ working            |
| YouTube   | OS env / `.env`                         | ✅ working            |
| ITAD      | from Game Spending Website tool         | ✅ working            |
| RAWG      | from Game Spending Website tool         | ✅ working            |
| Steam     | keyless (key optional, slot present)    | ✅ working            |
| Reddit    | —                                       | ⏳ creds pending      |

The smoke test confirms cross-source resolution: RAWG search → Steam `appid` →
ITAD price all chain correctly on a sample title (Hades, appid 1145360).

## Notes

- **Config precedence:** `.env` overrides OS env on purpose — the machine's
  user-scope env can be stale for an already-running shell. `.env` keeps the
  project reproducible.
- **Reddit:** drop `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` into `.env`
  (plus username/password for user-context, or leave blank for read-only).
  No code change needed — the smoke test will flip from SKIP to OK.
- **Steam Web API key:** the endpoints used here (`appreviews`, `ISteamNews`)
  need no key; the slot exists for future authenticated endpoints.
- **ITAD endpoints** were verified against the working Game Spending Website
  tool (`/games/lookup/v1`, `POST /games/overview/v2`), not just the idea doc.
```
