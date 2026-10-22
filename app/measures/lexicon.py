"""Rule-based sentiment: the degraded path when the LLM is down or unaffordable.

It produces labels in exactly the shape the LLM classifier does, so the same
aggregation runs on either. It is deliberately simple and English-only:
  * aspect detection by keyword per taxonomy key;
  * aspect polarity from opinion words within 4 tokens of the keyword and in the
    same clause (punctuation and "but"/"however"/"though"... end a clause), with
    a negator ("not", "never", "n't" ...) in the 3 tokens before an opinion word
    flipping it;
  * overall text polarity from the whole review's opinion-word balance, and the
    Steam thumbs-up when the text is balanced.
Confidence is a flat 0.6 so lexicon mentions pass the 0.5 aggregation floor.
"""
from __future__ import annotations

import re
from typing import Any

ASPECT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gameplay": ("gameplay", "mechanic", "mechanics", "combat", "fighting", "shooting", "gunplay"),
    "content": ("content", "variety", "repetitive", "repetition", "depth", "shallow", "replay", "replayability", "boring"),
    "exploration": ("exploration", "explore", "exploring", "planet", "planets", "universe", "galaxy", "procedural", "discovery", "world"),
    "story": ("story", "lore", "narrative", "quest", "quests", "plot", "writing"),
    "progression": ("grind", "grinding", "grindy", "crafting", "craft", "inventory", "economy", "resources", "progression", "upgrade"),
    "multiplayer": ("multiplayer", "co-op", "coop", "online", "friends", "servers", "server"),
    "visuals": ("graphics", "visuals", "visual", "beautiful", "art", "colors", "colours", "scenery", "gorgeous"),
    "audio": ("music", "soundtrack", "sound", "audio", "sounds"),
    "performance": ("bug", "bugs", "buggy", "crash", "crashes", "crashing", "fps", "framerate", "stutter", "stuttering", "lag", "performance", "optimization", "optimisation", "optimized", "glitch", "glitches"),
    "ui_controls": ("ui", "interface", "menu", "menus", "controls", "controller", "keyboard", "mouse"),
    "developer": ("update", "updates", "patch", "patches", "developer", "developers", "devs", "dev", "support", "roadmap", "redemption"),
    "expectations": ("promise", "promised", "promises", "lied", "lies", "lie", "hype", "hyped", "trailer", "trailers", "marketing", "advertised", "expected", "expectations"),
    "value": ("price", "worth", "money", "refund", "refunded", "$60", "60$", "cost", "dlc", "free"),
}
_KW_TO_ASPECT = {kw: a for a, kws in ASPECT_KEYWORDS.items() for kw in kws}

POSITIVE = frozenset("""
good great amazing awesome excellent love loved loving beautiful fun enjoy enjoyed enjoyable
fantastic incredible best brilliant masterpiece recommend recommended solid smooth polished
improved improvement better impressive gorgeous stunning relaxing addictive worth perfect
wonderful nice cool satisfying immersive fixed redeemed redemption generous free
""".split())
NEGATIVE = frozenset("""
bad terrible awful horrible boring worst hate hated disappointing disappointed disappointment
broken buggy crash crashes crashing lag laggy stutter unplayable repetitive shallow empty
lied lies lie scam overpriced refund refunded grindy tedious annoying poor mediocre
waste unfinished lacking missing clunky frustrating ugly dull meh garbage trash false
""".split())
NEGATORS = frozenset("not no never nothing nobody nor without isnt isn't wasnt wasn't dont don't doesnt doesn't didnt didn't cant can't cannot wont won't".split())

_TOKEN = re.compile(r"[a-z0-9$'\-]+|[.,;:!?]")
BOUNDARY = frozenset(". , ; : ! ? but however although though yet except whereas".split())
WINDOW = 4
NEG_SCOPE = 3
CONFIDENCE = 0.6


def tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _polarity_at(toks: list[str], i: int) -> int:
    """+1 / -1 for an opinion word at position i (negation-aware), else 0."""
    w = toks[i]
    score = 1 if w in POSITIVE else -1 if w in NEGATIVE else 0
    lo, _ = _clause_bounds(toks, i, NEG_SCOPE, 0)
    if score and any(t in NEGATORS or t.endswith("n't") for t in toks[lo:i]):
        score = -score
    return score


def _clause_bounds(toks: list[str], i: int, left: int, right: int) -> tuple[int, int]:
    """Inclusive [lo, hi] reaching up to `left`/`right` tokens from i, never
    crossing a clause boundary."""
    lo = i
    while lo > 0 and i - lo < left and toks[lo - 1] not in BOUNDARY:
        lo -= 1
    hi = i
    while hi < len(toks) - 1 and hi - i < right and toks[hi + 1] not in BOUNDARY:
        hi += 1
    return lo, hi


def label_review(text: str, voted_up: bool) -> dict[str, Any]:
    """One review -> {overall, substantive, aspects} in the classifier's shape."""
    toks = tokens(text)
    total = sum(_polarity_at(toks, i) for i in range(len(toks)))
    found: dict[str, int] = {}
    for i, t in enumerate(toks):
        aspect = _KW_TO_ASPECT.get(t)
        if not aspect:
            continue
        lo, hi = _clause_bounds(toks, i, WINDOW, WINDOW)
        s = sum(_polarity_at(toks, j) for j in range(lo, hi + 1) if j != i)
        # the keyword can itself be an opinion word ("buggy", "boring", "lied")
        s += _polarity_at(toks, i)
        found[aspect] = found.get(aspect, 0) + s
    if total > 0:
        overall = "positive"
    elif total < 0:
        overall = "negative"
    else:
        overall = "positive" if voted_up else "negative"
    aspects = [
        {"aspect": a, "detail": "", "confidence": CONFIDENCE,
         "sentiment": "positive" if s > 0 else "negative" if s < 0 else "neutral"}
        for a, s in found.items()
    ]
    words = sum(1 for t in toks if t not in BOUNDARY)
    return {"overall": overall, "substantive": bool(found) and words >= 4, "aspects": aspects}
