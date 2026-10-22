"""Aspect taxonomy and the deterministic maths behind every sentiment number.

The LLM (or the lexicon fallback) only LABELS reviews. Everything quantitative —
approval from text, divisiveness, per-aspect shares, driver impact — is computed
here, in plain Python, from those labels. That is what lets the synthesis step
"narrate over pre-computed signals": it receives these tables and may only
explain them, and `validate_drivers` rejects any driver the numbers don't back.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any

# A closed set, so counts aggregate across reviews and windows. Free-text detail
# ("base building", "frame drops") rides along in `detail` for specificity.
TAXONOMY: dict[str, str] = {
    "gameplay": "core mechanics, combat, moment-to-moment feel",
    "content": "amount and variety of things to do, depth, replayability",
    "exploration": "world, discovery, procedural generation, sense of scale",
    "story": "narrative, lore, quests, writing",
    "progression": "grind, crafting, economy, pacing, rewards",
    "multiplayer": "co-op, online play, social features",
    "visuals": "graphics, art style, visual design",
    "audio": "music, sound design, voice",
    "performance": "bugs, crashes, frame rate, optimisation, stability",
    "ui_controls": "interface, menus, inventory, controls",
    "developer": "updates, patches, roadmap, communication, post-launch support, trust in the studio",
    "expectations": "hype, marketing, promises versus what shipped",
    "value": "price, worth the money, monetisation, DLC",
    "other": "anything not covered above",
}
OVERALL = ("positive", "negative", "mixed", "neutral")
POLARITY = ("positive", "negative", "neutral")

MIN_CONFIDENCE = 0.5     # aspect mentions below this are ignored in aggregation
MIN_MENTIONS = 3         # an aspect needs this many reviews (and 3% of them) to be a driver
MIN_MENTION_SHARE = 0.03


def normalize_label(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce one model/lexicon label into the canonical shape, or None if unusable."""
    overall = str(raw.get("overall", "")).lower().strip()
    if overall not in OVERALL:
        return None
    seen: dict[str, dict[str, Any]] = {}
    for a in raw.get("aspects") or []:
        if not isinstance(a, dict):
            continue
        key = str(a.get("aspect", "")).lower().strip().replace(" ", "_").replace("&", "")
        key = key if key in TAXONOMY else "other"
        sent = str(a.get("sentiment", "")).lower().strip()
        if sent not in POLARITY:
            continue
        try:
            conf = max(0.0, min(1.0, float(a.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        mention = {"aspect": key, "detail": str(a.get("detail", ""))[:60].strip().lower(),
                   "sentiment": sent, "confidence": conf}
        # one mention per aspect per review: keep the most confident
        if key not in seen or conf > seen[key]["confidence"]:
            seen[key] = mention
    aspects = list(seen.values())
    return {"overall": overall, "substantive": bool(raw.get("substantive", True)), "aspects": aspects}


def divisiveness(p_pos: float, p_neg: float) -> float:
    """2 x the smaller camp. 0 = consensus (or everyone calmly mixed);
    1 = half love it, half hate it, nobody in between."""
    return round(2 * min(p_pos, p_neg), 3)


def aggregate(labelled: list[dict[str, Any]]) -> dict[str, Any]:
    """Window-level statistics from per-review labels.

    `labelled` items: review fields (id, text, voted_up, playtime_at_review_min)
    plus `label` (normalized) or label=None if classification failed.
    """
    classified = [r for r in labelled if r.get("label")]
    subst = [r for r in classified if r["label"]["substantive"]]
    n = len(subst)
    overall = Counter(r["label"]["overall"] for r in subst)
    p = {k: (overall[k] / n if n else 0.0) for k in OVERALL}
    sided = overall["positive"] + overall["negative"]

    mismatch_base = [r for r in subst if r["label"]["overall"] in ("positive", "negative")]
    mismatch = sum(
        1 for r in mismatch_base
        if (r["voted_up"] and r["label"]["overall"] == "negative")
        or (not r["voted_up"] and r["label"]["overall"] == "positive")
    )

    per: dict[str, dict[str, Any]] = defaultdict(lambda: {"positive": 0, "negative": 0, "neutral": 0,
                                                         "conf": [], "details": Counter(), "ids": []})
    for r in subst:
        for m in r["label"]["aspects"]:
            if m["confidence"] < MIN_CONFIDENCE:
                continue
            row = per[m["aspect"]]
            row[m["sentiment"]] += 1
            row["conf"].append(m["confidence"])
            if m["detail"]:
                row["details"][(m["detail"], m["sentiment"])] += 1
            row["ids"].append(r["id"])

    aspects = []
    for key, row in per.items():
        mentions = row["positive"] + row["negative"] + row["neutral"]
        sided_a = row["positive"] + row["negative"]
        aspects.append({
            "aspect": key,
            "mentions": mentions,
            "share_of_reviews": round(mentions / n, 3) if n else 0.0,
            "positive": row["positive"],
            "negative": row["negative"],
            "neutral": row["neutral"],
            "positive_share": round(row["positive"] / sided_a, 3) if sided_a else None,
            "net": round((row["positive"] - row["negative"]) / mentions, 3),
            # each review contributes +1/-1 to an aspect; impact is the net per substantive review
            "impact": round((row["positive"] - row["negative"]) / n, 4) if n else 0.0,
            "contested": round(2 * min(row["positive"], row["negative"]) / sided_a, 3) if sided_a else 0.0,
            "mean_confidence": round(sum(row["conf"]) / len(row["conf"]), 3),
            "top_details": [
                {"detail": d, "sentiment": s, "count": c}
                for (d, s), c in row["details"].most_common(4)
            ],
        })
    aspects.sort(key=lambda a: a["mentions"], reverse=True)

    def med_hours(rows: list[dict[str, Any]]) -> float | None:
        vals = [r["playtime_at_review_min"] for r in rows if r.get("playtime_at_review_min") is not None]
        return round(median(vals) / 60, 1) if vals else None

    return {
        "n_labelled": len(labelled),
        "n_classified": len(classified),
        "n_substantive": n,
        "approval_steam_sample": round(sum(r["voted_up"] for r in labelled) / len(labelled), 3) if labelled else None,
        "approval_text": round(overall["positive"] / sided, 3) if sided else None,
        "text_polarity": {k: round(v, 3) for k, v in p.items()},
        "divisiveness": divisiveness(p["positive"], p["negative"]),
        "vote_text_mismatch": round(mismatch / len(mismatch_base), 3) if mismatch_base else None,
        "playtime_median_h": {"voted_up": med_hours([r for r in labelled if r["voted_up"]]),
                              "voted_down": med_hours([r for r in labelled if not r["voted_up"]])},
        "aspects": aspects,
    }


def driver_candidates(agg: dict[str, Any], k: int = 6) -> list[dict[str, Any]]:
    """Aspects with enough mentions to matter, ranked by |impact|."""
    n = agg["n_substantive"]
    floor = max(MIN_MENTIONS, MIN_MENTION_SHARE * n)
    pool = [a for a in agg["aspects"] if a["aspect"] != "other" and a["mentions"] >= floor and a["impact"] != 0]
    return sorted(pool, key=lambda a: abs(a["impact"]), reverse=True)[:k]


def pick_exemplars(labelled: list[dict[str, Any]], candidates: list[dict[str, Any]],
                   *, per_aspect: int = 2, max_chars: int = 280) -> list[dict[str, Any]]:
    """Short quotes for the synthesizer: for each candidate aspect, the most confident
    review on its dominant side, then the most confident on the other side."""
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for c in candidates:
        dominant = "positive" if c["impact"] > 0 else "negative"
        for side in (dominant, "negative" if dominant == "positive" else "positive")[:per_aspect]:
            best = None
            for r in labelled:
                if not r.get("label") or r["id"] in used:
                    continue
                for m in r["label"]["aspects"]:
                    if m["aspect"] == c["aspect"] and m["sentiment"] == side:
                        if best is None or m["confidence"] > best[1]:
                            best = (r, m["confidence"])
            if best:
                r = best[0]
                used.add(r["id"])
                out.append({"id": r["id"], "aspect": c["aspect"], "sentiment": side,
                            "voted_up": r["voted_up"],
                            "text": re.sub(r"\s+", " ", r["text"]).strip()[:max_chars]})
    return out


_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def allowed_percentages(*objs: Any) -> set[int]:
    """Every 0-1 float in the synthesizer's input, as a whole percentage."""
    found: set[int] = set()

    def walk(o: Any) -> None:
        if isinstance(o, bool):
            return
        if isinstance(o, float) and -1.0 <= o <= 1.0:
            found.add(round(abs(o) * 100))
        elif isinstance(o, (int, float)) and 0 <= o <= 100:
            found.add(round(o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    for o in objs:
        walk(o)
    return found


def ungrounded_numbers(text: str, allowed: set[int], tolerance: int = 1) -> list[str]:
    """Percentages in model prose that don't match any input number (±tolerance)."""
    bad = []
    for m in _PCT.finditer(text or ""):
        v = round(float(m.group(1)))
        if not any(abs(v - a) <= tolerance for a in allowed):
            bad.append(m.group(0))
    return bad


def validate_drivers(raw: list[Any], candidates: list[dict[str, Any]], exemplar_ids: set[str],
                     *, max_drivers: int = 4) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only drivers the numbers support. Weight comes from the data, not the model."""
    by_key = {c["aspect"]: c for c in candidates}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        key = str(d.get("aspect", "")).lower().strip()
        direction = str(d.get("direction", "")).lower().strip()
        c = by_key.get(key)
        if c is None:
            dropped.append({"aspect": key, "reason": "not a candidate aspect"})
            continue
        expected = "positive" if c["impact"] > 0 else "negative"
        if direction != expected:
            dropped.append({"aspect": key, "reason": f"direction {direction!r} contradicts net {c['impact']:+}"})
            continue
        if any(k["aspect"] == key for k in kept):
            continue
        ids = [str(i) for i in d.get("evidence_ids") or [] if str(i) in exemplar_ids]
        kept.append({
            "aspect": key,
            "direction": direction,
            "weight": round(abs(c["impact"]), 3),
            "mentions": c["mentions"],
            "positive_share": c["positive_share"],
            "why": str(d.get("why", "")).strip(),
            "evidence_ids": ids,
        })
        if len(kept) >= max_drivers:
            break
    return kept, dropped
