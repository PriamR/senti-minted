"""Pluggable sentiment measures.

Each measure takes the reviews sampled in one time window and returns a small
result dict. Approval is the first; extremity / language-spread slot in behind
the same `Measure` protocol without touching the pipeline.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Measure(Protocol):
    name: str

    async def compute(self, reviews: list[dict[str, Any]], context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Score one window of reviews. `reviews` items carry at least
        {id, text, timestamp, updated, voted_up, language}. `context` describes the
        window (game, dates, events, population movement) for the synthesis step.
        Returns a JSON-able dict."""
        ...
