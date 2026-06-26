"""Approval measure — the headline dimension, in two layers.

  * approval_steam : voted_up ratio over the window (free, from review metadata)
  * approval_llm   : DeepSeek's read of the review TEXT, returned together with
                     the *drivers* (which aspects moved approval) and a one-line
                     why. The drivers are the whole point — they are what the raw
                     thumbs-up ratio cannot tell you.
"""
from __future__ import annotations

import json
from typing import Any

from ..clients import deepseek
from ..config import settings

# Keep LLM cost bounded per window.
MAX_LLM_REVIEWS = 40
MAX_CHARS = 500

_PROMPT = """You are a games-market analyst reading a sample of Steam reviews for {subject},
all written within a single time window. Judge the OVERALL reception in this window.

Return ONLY JSON of this exact shape:
{{
  "approval": <float 0-1, share of goodwill toward the game in this window>,
  "divisiveness": <float 0-1, 0=consensus, 1=love-it-or-hate-it split>,
  "drivers": [
    {{"aspect": "<short feature/theme>", "direction": "positive|negative",
      "weight": <float 0-1, how much it drove sentiment>}}
  ],
  "summary": "<one sentence on what defines reception in this window and why>"
}}
Use at most 4 drivers, most important first. Base everything only on the reviews below.

REVIEWS:
{reviews}
"""


def _steam_ratio(reviews: list[dict[str, Any]]) -> float | None:
    if not reviews:
        return None
    up = sum(1 for r in reviews if r.get("voted_up"))
    return up / len(reviews)


class ApprovalMeasure:
    name = "approval"

    def __init__(self, use_llm: bool = True):
        self.use_llm = use_llm

    async def compute(self, reviews: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "n_reviews": len(reviews),
            "approval_steam": _steam_ratio(reviews),
            "approval_llm": None,
            "divisiveness": None,
            "drivers": [],
            "summary": None,
        }
        if not self.use_llm or not reviews or not settings.deepseek_api_key:
            return result

        sample = [r["text"] for r in reviews if r.get("text")][:MAX_LLM_REVIEWS]
        if not sample:
            return result
        numbered = "\n".join(f"{i+1}. {t[:MAX_CHARS]}" for i, t in enumerate(sample))
        prompt = _PROMPT.format(subject="this game", reviews=numbered)

        resp = await deepseek.client().chat.completions.create(
            model=settings.deepseek_chat_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        try:
            parsed = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            return result

        result["approval_llm"] = parsed.get("approval")
        result["divisiveness"] = parsed.get("divisiveness")
        result["drivers"] = parsed.get("drivers", [])
        result["summary"] = parsed.get("summary")
        return result
