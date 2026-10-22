"""Shared fixtures. Every test runs offline: no API key, a throwaway cache file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import cache
from app.config import settings

FIXTURES = Path(__file__).parent / "fixtures"


def _set(name: str, value) -> None:
    object.__setattr__(settings, name, value)  # Settings is a frozen dataclass


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    """Point the cache at a temp file, blank the LLM key, and reset cache state."""
    saved = {k: getattr(settings, k) for k in ("cache_path", "deepseek_api_key", "cache_enabled")}
    _set("cache_path", tmp_path / "cache.sqlite")
    _set("deepseek_api_key", "")
    _set("cache_enabled", True)
    cache.reset_stats()
    cache._bypass.clear()
    yield
    for k, v in saved.items():
        _set(k, v)
    cache._bypass.clear()


@pytest.fixture
def nms_events() -> list[dict]:
    return json.loads((FIXTURES / "nms_events.json").read_text(encoding="utf-8"))
