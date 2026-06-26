"""Smoke test: make one real call against every configured API and report.

Run from the project root:
    python -m scripts.smoke_test

Each check prints  OK / FAIL / SKIP (missing key).  Uses a well-known appid
(Hades = 1145360) so the cross-source resolution is exercised end to end.
"""
from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.clients import steam, steamspy, itad, rawg, igdb, youtube, reddit, deepseek

APPID = 1145360          # Hades
GAME_NAME = "Hades"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def line(status: str, name: str, detail: str) -> None:
    color = {"OK": GREEN, "FAIL": RED, "SKIP": YELLOW}[status]
    print(f"  {color}{status:<4}{RESET} {name:<12} {DIM}{detail}{RESET}")


async def check(name: str, required_keys: list[str], coro_factory) -> bool:
    missing = [k for k in required_keys if not getattr(settings, k)]
    if missing:
        line("SKIP", name, f"missing: {', '.join(missing)}")
        return True  # not a failure — just unconfigured
    try:
        detail = await coro_factory()
        line("OK", name, detail)
        return True
    except Exception as e:  # noqa: BLE001 — smoke test wants the message, not a trace
        line("FAIL", name, f"{type(e).__name__}: {str(e)[:120]}")
        return False


async def steam_check() -> str:
    summary = await steam.get_review_summary(APPID)
    news = await steam.get_news(APPID, count=1)
    return f"{summary.get('review_score_desc', '?')}, {summary.get('total_reviews', 0)} reviews; news ok={bool(news)}"


async def steamspy_check() -> str:
    tags = await steamspy.get_tags(APPID)
    top = sorted(tags.items(), key=lambda x: x[1], reverse=True)[:3]
    return "top tags: " + ", ".join(t for t, _ in top)


async def itad_check() -> str:
    price = await itad.price_for_appid(APPID)
    return f"price={price}" if price else "resolved appid, no current price"


async def rawg_check() -> str:
    results = await rawg.search(GAME_NAME, page_size=1)
    if not results:
        return "no results"
    g = results[0]
    stores = await rawg.get_stores(g["id"])
    appid = rawg.extract_steam_appid(stores)
    return f"{g['name']} (rawg id {g['id']}); steam appid={appid}"


async def igdb_check() -> str:
    games = await igdb.search_game(GAME_NAME, limit=1)
    return f"{games[0]['name']}" if games else "token ok, no game match"


async def youtube_check() -> str:
    vids = await youtube.search_videos(f"{GAME_NAME} review", max_results=1)
    return f"found video: {vids[0]['snippet']['title'][:50]}" if vids else "no videos"


async def reddit_check() -> str:
    posts = await reddit.search_subreddit("Games", GAME_NAME, limit=1)
    return f"found {len(posts)} post(s)"


async def deepseek_check() -> str:
    return f"replied: {await deepseek.ping()!r}"


async def main() -> int:
    print(f"\nSenti-Minted API smoke test  {DIM}(appid {APPID} / {GAME_NAME}){RESET}\n")

    checks = [
        check("steam", [], steam_check),
        check("steamspy", [], steamspy_check),
        check("itad", ["itad_api_key"], itad_check),
        check("rawg", ["rawg_api_key"], rawg_check),
        check("igdb", ["twitch_client_id", "twitch_client_secret"], igdb_check),
        check("youtube", ["youtube_api_key"], youtube_check),
        check("reddit", ["reddit_client_id", "reddit_client_secret"], reddit_check),
        check("deepseek", ["deepseek_api_key"], deepseek_check),
    ]
    results = await asyncio.gather(*checks)

    print()
    if all(results):
        print(f"{GREEN}All configured APIs responded.{RESET}\n")
        return 0
    print(f"{RED}Some checks failed — see above.{RESET}\n")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
