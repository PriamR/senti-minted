"""DeepSeek — sentiment LLM via the OpenAI-compatible API.

Two-model strategy (per Idea 1.5):
  * deepseek-chat     -> high-volume aspect/sentiment classification (cheap)
  * deepseek-reasoner -> low-volume narrative synthesis (capable)

The async OpenAI client is used so it composes with the rest of the (async)
stack. The LLM only ever sees pre-computed numbers + text samples.
"""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from ..config import settings

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
    return _client


async def classify_sentiment(texts: list[str], *, subject: str = "this game") -> list[dict[str, Any]]:
    """Aspect-based sentiment over a batch of comments/reviews. Returns a list of
    {feature, sentiment, confidence} objects (best-effort JSON parse)."""
    numbered = "\n".join(f"{i+1}. {t[:600]}" for i, t in enumerate(texts))
    prompt = (
        f"You are analysing player discussion about {subject}. For each numbered "
        "comment, extract the main feature/theme discussed, the sentiment toward it "
        "(positive/negative/neutral), and a confidence 0-1. Respond ONLY with a JSON "
        'object: {"results": [{"n": <num>, "feature": "...", '
        '"sentiment": "...", "confidence": 0.0}]}\n\n' + numbered
    )
    resp = await client().chat.completions.create(
        model=settings.deepseek_chat_model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content).get("results", [])


async def ping() -> str:
    """Minimal liveness check used by the smoke test."""
    resp = await client().chat.completions.create(
        model=settings.deepseek_chat_model,
        messages=[{"role": "user", "content": "Reply with exactly: pong"}],
        max_tokens=5,
        temperature=0.0,
    )
    return (resp.choices[0].message.content or "").strip()
