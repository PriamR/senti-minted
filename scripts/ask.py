"""Ask the sentiment-journey agent a natural-language question.

    python -m scripts.ask "What did No Man's Sky's reception look like 2 years in?"
    python -m scripts.ask "How is No Man's Sky received recently?" --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.agent.controller import run_agent

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


async def main() -> int:
    ap = argparse.ArgumentParser(description="Natural-language sentiment-journey agent.")
    ap.add_argument("question", help="e.g. \"What did No Man's Sky's reception look like 2 years in?\"")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print each tool call (provenance trace)")
    args = ap.parse_args()

    print(f"\n{BOLD}Q:{RESET} {args.question}\n")
    if args.verbose:
        print(f"{DIM}tool trace:{RESET}")
    result = await run_agent(args.question, verbose=args.verbose)

    print(f"\n{BOLD}A:{RESET} {result['answer']}\n")
    print(f"{DIM}({result['steps']} step(s), {len(result['provenance'])} tool call(s)){RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
