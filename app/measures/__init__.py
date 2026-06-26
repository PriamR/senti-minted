"""Pluggable sentiment measures.

Each measure takes the reviews sampled in one time window and returns a small
result dict. Approval is the first; extremity / divisiveness / language-spread
slot in behind the same `Measure` protocol without touching the pipeline.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Measure(Protocol):
    name: str

    async def compute(self, reviews: list[dict[str, Any]]) -> dict[str, Any]:
        """Score one window of reviews. `reviews` items carry at least
        {text, timestamp, voted_up, language}. Returns a JSON-able dict."""
        ...
