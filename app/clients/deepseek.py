"""DeepSeek — the sentiment LLM, via the OpenAI-compatible API.

Two tiers (models and prices checked on api-docs.deepseek.com, 22 Oct 2026):
  * fast tier -> deepseek-flash, thinking OFF: high-volume per-review aspect
                 classification, and the agent's tool-calling loop
  * pro tier  -> deepseek-v4-pro, thinking ON: one synthesis call per window

Every call goes through `chat_json`, which records token usage and cost in a
`UsageLedger`, so a run can say exactly what it spent and on which stage. The LLM
only ever sees review text or pre-computed numbers; it never sees a key or a raw
API response.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from .. import cache
from ..config import settings

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            max_retries=2,
            timeout=300.0,
        )
    return _client


class LLMError(RuntimeError):
    """The LLM call failed or returned something unusable."""


# ── prices ──────────────────────────────────────────────────────────────────
# USD per 1M tokens at PEAK rates, from https://api-docs.deepseek.com/quick_start/pricing
# as published on 22 Oct 2026. Off-peak is half. Peak = 01:00-04:00 and 06:00-10:00
# UTC, Monday to Friday (Chinese public holidays are off-peak; not modelled here).
PRICES_AS_OF = "2026-10-22"
PRICES_PEAK: dict[str, dict[str, float]] = {
    "deepseek-flash": {"input_hit": 0.006, "input_miss": 0.30, "output": 1.20},
    "deepseek-v4-pro": {"input_hit": 0.044, "input_miss": 1.32, "output": 3.96},
}
# The API reports the serving model; legacy names are billed as Flash.
_BILLED_AS = {"deepseek-chat": "deepseek-flash", "deepseek-reasoner": "deepseek-flash",
              "deepseek-v4-flash": "deepseek-flash"}


def is_peak(at: datetime) -> bool:
    at = at.astimezone(timezone.utc)
    if at.weekday() >= 5:
        return False
    return at.hour in (1, 2, 3) or 6 <= at.hour < 10


def cost_usd(model: str, usage: dict[str, int], at: datetime) -> float:
    """Cost of one call from its usage block. Completion tokens include reasoning."""
    prices = PRICES_PEAK.get(_BILLED_AS.get(model, model))
    if prices is None:
        return 0.0
    factor = 1.0 if is_peak(at) else 0.5
    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit)
    out = usage.get("completion_tokens", 0)
    return factor * (hit * prices["input_hit"] + miss * prices["input_miss"] + out * prices["output"]) / 1e6


# ── usage ledger ────────────────────────────────────────────────────────────

@dataclass
class UsageLedger:
    """Every LLM call in a run: stage, model, tokens, cost, latency."""
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, entry: dict[str, Any]) -> None:
        self.calls.append(entry)

    def summary(self) -> dict[str, Any]:
        def total(rows: list[dict[str, Any]]) -> dict[str, Any]:
            live = [r for r in rows if not r["cached"]]
            return {
                "calls": len(rows),
                "cached_calls": len(rows) - len(live),
                "prompt_tokens": sum(r["prompt_tokens"] for r in live),
                "prompt_cache_hit_tokens": sum(r["prompt_cache_hit_tokens"] for r in live),
                "completion_tokens": sum(r["completion_tokens"] for r in live),
                "reasoning_tokens": sum(r["reasoning_tokens"] for r in live),
                "cost_usd": round(sum(r["cost_usd"] for r in live), 6),
                "latency_s": round(sum(r["latency_s"] for r in live), 1),
            }
        stages = sorted({r["stage"] for r in self.calls})
        return {
            **total(self.calls),
            "by_stage": {s: total([r for r in self.calls if r["stage"] == s]) for s in stages},
            "prices_as_of": PRICES_AS_OF,
        }


# ── JSON handling ───────────────────────────────────────────────────────────

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


def parse_json(text: str) -> Any:
    """Parse model JSON, repairing the two faults seen in practice: code fences
    around the object, and stray backticks inside it (e.g. {"ok":`true`})."""
    text = (text or "").strip()
    for candidate in (text, _FENCE.sub("", text), _FENCE.sub("", text).replace("`", "")):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1].replace("`", ""))
        except json.JSONDecodeError:
            pass
    raise LLMError(f"unparseable JSON from model: {text[:120]!r}")


# ── calls ───────────────────────────────────────────────────────────────────

def tier(name: str) -> tuple[str, bool]:
    """(model, thinking) for a tier name."""
    if name == "fast":
        return settings.deepseek_fast_model, False
    if name == "pro":
        return settings.deepseek_pro_model, True
    raise ValueError(f"unknown tier {name!r}")


async def chat_json(
    messages: list[dict[str, Any]],
    *,
    tier_name: str,
    stage: str,
    ledger: UsageLedger | None = None,
    max_tokens: int = 4000,
    retries: int = 1,
) -> Any:
    """One JSON-mode call on a tier, cached by (model, thinking, messages).

    Retries once on unparseable output. Raises LLMError when the key is missing,
    the API errors, or the output never parses; callers decide how to degrade.
    """
    if not settings.deepseek_api_key:
        raise LLMError("DEEPSEEK_API_KEY not set")
    model, thinking = tier(tier_name)
    params = {"model": model, "thinking": thinking, "messages": messages, "max_tokens": max_tokens}
    fresh = False

    async def fetch() -> dict[str, Any]:
        nonlocal fresh
        fresh = True
        last_err: Exception | None = None
        for _ in range(retries + 1):
            t0 = time.perf_counter()
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
                "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
            }
            if not thinking:
                kwargs["temperature"] = 0.0  # ignored in thinking mode
            try:
                resp = await client().chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001 — auth, network, rate limit: all degrade the same way
                raise LLMError(f"{type(e).__name__}: {str(e)[:200]}") from e
            latency = time.perf_counter() - t0
            u = resp.usage.model_dump() if resp.usage else {}
            details = u.get("completion_tokens_details") or {}
            usage = {
                "prompt_tokens": u.get("prompt_tokens", 0),
                "prompt_cache_hit_tokens": u.get("prompt_cache_hit_tokens", 0),
                "prompt_cache_miss_tokens": u.get("prompt_cache_miss_tokens", u.get("prompt_tokens", 0)),
                "completion_tokens": u.get("completion_tokens", 0),
                "reasoning_tokens": details.get("reasoning_tokens") or 0,
            }
            at = datetime.now(tz=timezone.utc)
            entry = {
                **usage,
                "model": resp.model or model,
                "thinking": thinking,
                "cost_usd": cost_usd(resp.model or model, usage, at),
                "peak": is_peak(at),
                "latency_s": round(latency, 2),
            }
            try:
                return {"content": parse_json(resp.choices[0].message.content or ""), "usage": entry}
            except LLMError as e:
                last_err = e
                if ledger is not None:  # a failed parse still cost money
                    ledger.record({**entry, "stage": stage, "cached": False, "parse_failed": True})
        raise last_err or LLMError("no response")

    result = await cache.cached("llm", "chat_json", params, fetch)
    if ledger is not None:
        # A cache hit is logged with the original call's tokens but cost nothing now.
        ledger.record({**result["usage"], "stage": stage, "cached": not fresh})
    return result["content"]


async def chat_json_batch(
    batches: list[list[dict[str, Any]]],
    *,
    tier_name: str,
    stage: str,
    ledger: UsageLedger | None = None,
    max_tokens: int = 4000,
    concurrency: int = 4,
) -> list[Any]:
    """Run several independent `chat_json` calls with bounded concurrency. A batch
    that fails comes back as its LLMError, so one bad batch doesn't sink the rest."""
    sem = asyncio.Semaphore(concurrency)

    async def one(msgs: list[dict[str, Any]]) -> Any:
        async with sem:
            try:
                return await chat_json(msgs, tier_name=tier_name, stage=stage, ledger=ledger,
                                       max_tokens=max_tokens)
            except LLMError as e:
                return e

    return await asyncio.gather(*(one(m) for m in batches))


async def ping() -> str:
    """Minimal liveness check used by the smoke test (fast tier, uncached)."""
    model, _ = tier("fast")
    resp = await client().chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with exactly: pong"}],
        max_tokens=5,
        temperature=0.0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return (resp.choices[0].message.content or "").strip()
