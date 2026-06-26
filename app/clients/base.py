"""Shared HTTP helpers for all API clients."""
from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# A polite, identifiable UA. Steam's storefront endpoints behave better with one.
USER_AGENT = "SentiMinted/0.1 (+https://github.com/Priyam/senti-minted)"


def make_client(**kwargs) -> httpx.AsyncClient:
    """An AsyncClient with sane defaults shared across all integrations."""
    headers = {"User-Agent": USER_AGENT}
    headers.update(kwargs.pop("headers", {}))
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=headers, **kwargs)


class APIError(RuntimeError):
    """Raised when an upstream API returns an unrecoverable error."""
