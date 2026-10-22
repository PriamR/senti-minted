"""Thin FastAPI layer over the pipeline and the agent. Keys stay server-side.

    uvicorn app.api:app --port 8000

  GET  /health                        liveness, which keys are configured, cache stats
  GET  /api/resolve?q=                name or appid -> canonical product record
  GET  /api/timelines                 saved timelines (summary rows)
  GET  /api/timeline/{appid}          saved timeline, or build it now (?refresh=true forces)
  POST /api/timeline/{appid}/jobs     build in the background; poll the job
  GET  /api/jobs/{job_id}             job status + progress log
  POST /api/ask {question}            agent answer + provenance + cost
  GET  /                              web page: search a game, build, browse, ask
  GET  /view/{appid}                  the timeline page with an ask box
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from . import cache, store
from .agent.controller import run_agent
from .clients import deepseek
from .config import settings
from .pipeline.timeline import SELECT_STRATEGIES, build_timeline
from .resolver import resolve_product
from .viz import render_html

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Senti-Minted", version="0.2.0",
              description="Retrospective product-sentiment journeys from Steam reviews.")

_jobs: dict[str, dict[str, Any]] = {}
_job_by_appid: dict[int, str] = {}


class BuildOptions(BaseModel):
    name: str | None = None
    no_llm: bool = False
    max_events: int = Field(6, ge=0, le=20)
    sample_reviews: int = Field(200, ge=20, le=500)
    select: str = "movers"


class AskBody(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)


def _opts(name: str | None, no_llm: bool, max_events: int, sample_reviews: int, select: str) -> BuildOptions:
    if select not in SELECT_STRATEGIES:
        raise HTTPException(422, f"select must be one of {SELECT_STRATEGIES}")
    return BuildOptions(name=name, no_llm=no_llm, max_events=max_events,
                        sample_reviews=sample_reviews, select=select)


async def _build(appid: int, opts: BuildOptions, progress=None) -> dict[str, Any]:
    name = opts.name
    if not name:
        existing = store.load(appid)
        name = existing["name"] if existing and existing.get("name") != str(appid) else None
    if not name:
        name = (await resolve_product(str(appid))).get("canonical_name") or str(appid)
    timeline = await build_timeline(
        appid, name=name, use_llm=not opts.no_llm, max_events=opts.max_events,
        sample_reviews=opts.sample_reviews, select=opts.select, progress=progress,
    )
    store.save(timeline)
    return timeline


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "time": datetime.now(tz=timezone.utc).isoformat(),
        "keys_configured": {
            "deepseek": bool(settings.deepseek_api_key),
            "rawg": bool(settings.rawg_api_key),
            "igdb": bool(settings.twitch_client_id and settings.twitch_client_secret),
            "itad": bool(settings.itad_api_key),
            "youtube": bool(settings.youtube_api_key),
            "reddit": bool(settings.reddit_client_id and settings.reddit_client_secret),
        },
        "models": {"fast": deepseek.tier("fast")[0], "pro": deepseek.tier("pro")[0],
                   "prices_as_of": deepseek.PRICES_AS_OF},
        "cache": cache.stats(),
        "saved_timelines": len(store.list_saved()),
    }


@app.get("/api/resolve")
async def api_resolve(q: str = Query(..., min_length=1, max_length=120)) -> dict[str, Any]:
    record = await resolve_product(q)
    saved = store.load(record["appid"]) if record.get("appid") else None
    return {**record, "saved": saved is not None}


@app.get("/api/timelines")
async def api_timelines() -> list[dict[str, Any]]:
    return store.list_saved()


@app.get("/api/timeline/{appid}")
async def api_timeline(
    appid: int,
    refresh: bool = False,
    name: str | None = None,
    no_llm: bool = False,
    max_events: int = Query(6, ge=0, le=20),
    sample_reviews: int = Query(200, ge=20, le=500),
    select: str = "movers",
) -> dict[str, Any]:
    """The saved timeline if there is one (unless refresh), else build it now.
    A full LLM build takes minutes; use the jobs endpoint from a browser."""
    if not refresh:
        saved = store.load(appid)
        if saved is not None:
            return saved
    return await _build(appid, _opts(name, no_llm, max_events, sample_reviews, select))


@app.post("/api/timeline/{appid}/jobs")
async def api_start_job(
    appid: int,
    name: str | None = None,
    no_llm: bool = False,
    max_events: int = Query(6, ge=0, le=20),
    sample_reviews: int = Query(200, ge=20, le=500),
    select: str = "movers",
) -> dict[str, Any]:
    running = _job_by_appid.get(appid)
    if running and _jobs[running]["status"] == "running":
        return {"job_id": running, "already_running": True}
    opts = _opts(name, no_llm, max_events, sample_reviews, select)
    job_id = uuid.uuid4().hex[:12]
    job: dict[str, Any] = {"job_id": job_id, "appid": appid, "status": "running", "log": [],
                           "started_at": datetime.now(tz=timezone.utc).isoformat(), "error": None}
    _jobs[job_id] = job
    _job_by_appid[appid] = job_id

    async def run() -> None:
        try:
            t = await _build(appid, opts, progress=job["log"].append)
            job["status"] = "done"
            job["summary"] = {"events_analyzed": t["events_analyzed"], "degraded": t["degraded"],
                              "cost_usd": t["llm_usage"]["cost_usd"]}
        except Exception as e:  # noqa: BLE001 — report any failure to the poller
            job["status"] = "error"
            job["error"] = deepseek.scrub(f"{type(e).__name__}: {str(e)[:300]}")

    job["_task"] = asyncio.create_task(run())  # keep a reference so it isn't collected
    return {"job_id": job_id, "already_running": False}


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {k: v for k, v in job.items() if not k.startswith("_")}


@app.post("/api/ask")
async def api_ask(body: AskBody) -> dict[str, Any]:
    if not settings.deepseek_api_key:
        raise HTTPException(503, "The agent needs DEEPSEEK_API_KEY; the timeline endpoints work without it.")
    try:
        return await run_agent(body.question)
    except Exception as e:  # noqa: BLE001 — upstream LLM failure
        raise HTTPException(502, deepseek.scrub(f"agent failed: {type(e).__name__}: {str(e)[:200]}")) from e


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@app.get("/static/md.js")
async def md_js() -> FileResponse:
    return FileResponse(WEB_DIR / "md.js", media_type="text/javascript")


@app.get("/view/{appid}", response_class=HTMLResponse)
async def view(appid: int) -> HTMLResponse:
    t = store.load(appid)
    if t is None:
        raise HTTPException(404, f"No saved timeline for {appid}. Build it from the home page.")
    return HTMLResponse(render_html(t, ask=True))
