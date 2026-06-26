"""Central config: loads keys from `.env` (preferred) then OS environment.

Why `.env` first: the OS user-scope env on this machine can be stale for a
running shell (vars added after the process started aren't seen). The project
`.env` keeps everything self-contained and reproducible. OS env still works as
a fallback for anything not pinned in `.env`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root, overriding any stale OS-env value.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _get(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass(frozen=True)
class Settings:
    # LLM
    deepseek_api_key: str = field(default_factory=lambda: _get("DEEPSEEK_API_KEY"))
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_chat_model: str = "deepseek-chat"          # cheap: classification
    deepseek_reasoner_model: str = "deepseek-reasoner"  # capable: synthesis

    # Twitch / IGDB
    twitch_client_id: str = field(default_factory=lambda: _get("TWITCH_CLIENT_ID"))
    twitch_client_secret: str = field(default_factory=lambda: _get("TWITCH_CLIENT_SECRET"))

    # YouTube
    youtube_api_key: str = field(default_factory=lambda: _get("YOUTUBE_DATA_API_KEY"))

    # IsThereAnyDeal
    itad_api_key: str = field(default_factory=lambda: _get("ITAD_API_KEY"))

    # RAWG
    rawg_api_key: str = field(default_factory=lambda: _get("RAWG_API_KEY"))

    # Steam (optional — most endpoints we use are keyless)
    steam_web_api_key: str = field(default_factory=lambda: _get("STEAM_WEB_API_KEY"))

    # Reddit
    reddit_client_id: str = field(default_factory=lambda: _get("REDDIT_CLIENT_ID"))
    reddit_client_secret: str = field(default_factory=lambda: _get("REDDIT_CLIENT_SECRET"))
    reddit_user_agent: str = field(
        default_factory=lambda: _get("REDDIT_USER_AGENT") or "SentiMinted/0.1"
    )
    reddit_username: str = field(default_factory=lambda: _get("REDDIT_USERNAME"))
    reddit_password: str = field(default_factory=lambda: _get("REDDIT_PASSWORD"))

    def missing(self) -> list[str]:
        """Names of keys that are required but unset (for diagnostics)."""
        required = {
            "DEEPSEEK_API_KEY": self.deepseek_api_key,
            "TWITCH_CLIENT_ID": self.twitch_client_id,
            "TWITCH_CLIENT_SECRET": self.twitch_client_secret,
            "YOUTUBE_DATA_API_KEY": self.youtube_api_key,
            "ITAD_API_KEY": self.itad_api_key,
            "RAWG_API_KEY": self.rawg_api_key,
        }
        return [name for name, val in required.items() if not val]


settings = Settings()
