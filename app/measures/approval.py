"""Approval measure — aspect-based sentiment for one window, in two LLM stages.

  Stage 1 (fast tier, deepseek-flash, thinking off): label every sampled review
           in batches of 20 — overall text polarity, whether it is substantive,
           and up to 3 aspects from a closed taxonomy, each with polarity and
           confidence.
  Aggregate (Python, no LLM): approval from text, divisiveness, per-aspect
           shares/net/impact, vote-vs-text mismatch. See aspects.py.
  Stage 2 (pro tier, deepseek-v4-pro, thinking on): one call that receives ONLY
           the aggregate tables plus ~10 short exemplar quotes, and names up to
           4 drivers with a one-line why and a summary. Drivers are validated
           against the numbers; percentages in its prose are checked against
           the input.

`single_model=True` runs stage 1 on the pro tier too, to measure what the
two-tier split saves. If the LLM is unavailable (no key, API error, unparseable
output) the window falls back to the lexicon labeller and a templated summary,
and is marked `degraded`.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..clients import deepseek
from ..clients.deepseek import LLMError, UsageLedger
from ..config import settings
from . import aspects as A
from . import lexicon

BATCH_SIZE = 20
MAX_CHARS = 600
_BBCODE = re.compile(r"\[/?[a-z0-9*]+(?:=[^\]]*)?\]", re.I)

CLASSIFY_SYSTEM = (
    "You label Steam reviews for aspect-based sentiment analysis.\n"
    "For EACH numbered review return an object with:\n"
    '  "id": the review number,\n'
    '  "overall": sentiment of the TEXT toward the game: "positive" | "negative" | "mixed" | "neutral",\n'
    '  "substantive": false if the text is a joke, meme, ASCII art, off-topic, or too vague to name '
    "any aspect; otherwise true,\n"
    '  "aspects": 0-3 objects {"aspect": <taxonomy key>, "detail": <2-5 word English phrase, specific>, '
    '"sentiment": "positive" | "negative" | "neutral", "confidence": <0-1>}.\n'
    "Taxonomy keys:\n"
    + "".join(f"  {k}: {v}\n" for k, v in A.TAXONOMY.items())
    + "Rules: judge the words, not any score. Read sarcasm for its intended meaning. Reviews may be in "
    "any language; write details in English. Only list aspects the review actually discusses.\n"
    'Respond ONLY with JSON: {"results": [ ... ]} with one object per review, in order.'
)

SYNTH_SYSTEM = (
    "You are a games-market analyst. You receive PRE-COMPUTED statistics for one time window of a "
    "game's Steam reviews: population approval before/after the events, a labelled review sample, "
    "a per-aspect table, candidate driver aspects ranked by impact (net positive-minus-negative "
    "mentions per substantive review), and short exemplar quotes.\n"
    "Explain what defined reception in this window. Rules:\n"
    "- Drivers must be chosen from the candidate list; direction must match the sign of its impact.\n"
    "- Use only numbers that appear in the input. Do not compute or invent new figures. Write "
    "proportions as whole percentages (0.83 -> 83%).\n"
    "- Ground each driver's why in the exemplars and the event context; cite exemplar ids.\n"
    'Respond ONLY with JSON: {"drivers": [{"aspect": "<candidate key>", "direction": '
    '"positive|negative", "why": "<one sentence>", "evidence_ids": ["<id>", ...]}], '
    '"summary": "<two sentences>", "tension": "<the main disagreement among players in one '
    'sentence, or null>"}. At most 4 drivers, most important first.'
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _BBCODE.sub(" ", text or "")).strip()[:MAX_CHARS]


def _batches(reviews: list[dict[str, Any]], game: str) -> list[list[dict[str, Any]]]:
    out = []
    for i in range(0, len(reviews), BATCH_SIZE):
        chunk = reviews[i:i + BATCH_SIZE]
        body = "\n".join(f"[{j + 1}] {_clean(r['text'])}" for j, r in enumerate(chunk))
        out.append([
            {"role": "system", "content": CLASSIFY_SYSTEM},  # identical prefix -> provider cache hits
            {"role": "user", "content": f"Game: {game}\nReviews:\n{body}"},
        ])
    return out


async def classify_llm(reviews: list[dict[str, Any]], *, game: str, tier_name: str,
                       ledger: UsageLedger) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Stage 1. Returns (reviews with `label`, {batches, failed_batches, unlabelled})."""
    batches = _batches(reviews, game)
    results = await deepseek.chat_json_batch(
        batches, tier_name=tier_name, stage="classify", ledger=ledger,
        max_tokens=4000 if tier_name == "fast" else 16000,
    )
    failed = sum(1 for r in results if isinstance(r, LLMError))
    if failed == len(batches):
        raise LLMError(f"all {failed} classification batches failed: {results[0]}")
    labelled: list[dict[str, Any]] = []
    for b, res in enumerate(results):
        chunk = reviews[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        by_num: dict[int, dict[str, Any]] = {}
        if not isinstance(res, LLMError):
            for item in (res or {}).get("results", []) if isinstance(res, dict) else []:
                try:
                    by_num[int(item.get("id"))] = item
                except (TypeError, ValueError):
                    continue
        for j, r in enumerate(chunk):
            raw = by_num.get(j + 1)
            labelled.append({**r, "label": A.normalize_label(raw) if raw else None})
    unlabelled = sum(1 for r in labelled if r["label"] is None)
    return labelled, {"batches": len(batches), "failed_batches": failed, "unlabelled": unlabelled}


def classify_lexicon(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "label": A.normalize_label(lexicon.label_review(r["text"], r["voted_up"]))} for r in reviews]


def _pct(x: float | None) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "n/a"


def _synth_input(agg: dict[str, Any], candidates: list[dict[str, Any]], exemplars: list[dict[str, Any]],
                 context: dict[str, Any]) -> dict[str, Any]:
    keep = ("aspect", "mentions", "share_of_reviews", "positive", "negative", "positive_share", "impact",
            "contested", "top_details")
    return {
        "game": context.get("game"),
        "window": {"start": context.get("start"), "end": context.get("end")},
        "events": context.get("events", []),
        "event_notes": context.get("event_blurb", ""),
        "population": context.get("movement") or {},
        "sample": {
            "substantive_reviews": agg["n_substantive"],
            "approval_steam_sample": agg["approval_steam_sample"],
            "approval_text": agg["approval_text"],
            "text_polarity": agg["text_polarity"],
            "divisiveness": agg["divisiveness"],
            "vote_text_mismatch": agg["vote_text_mismatch"],
        },
        "aspects": [{k: a[k] for k in keep} for a in agg["aspects"][:10]],
        "candidates": [{k: c[k] for k in keep} for c in candidates],
        "exemplars": exemplars,
    }


async def synthesize_llm(agg: dict[str, Any], labelled: list[dict[str, Any]], context: dict[str, Any],
                         *, ledger: UsageLedger) -> dict[str, Any]:
    """Stage 2 on the pro tier, then validation against the aggregate."""
    candidates = A.driver_candidates(agg)
    exemplars = A.pick_exemplars([{**r, "text": _clean(r["text"])} for r in labelled], candidates)
    payload = _synth_input(agg, candidates, exemplars, context)
    out = await deepseek.chat_json(
        [{"role": "system", "content": SYNTH_SYSTEM},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        tier_name="pro", stage="synthesize", ledger=ledger, max_tokens=16000,
    )
    drivers, dropped = A.validate_drivers(out.get("drivers"), candidates, {e["id"] for e in exemplars})
    allowed = A.allowed_percentages(payload)
    prose = " ".join([str(out.get("summary") or ""), str(out.get("tension") or "")]
                     + [d["why"] for d in drivers])
    by_id = {e["id"]: e for e in exemplars}
    for d in drivers:
        d["evidence"] = [by_id[i]["text"] for i in d["evidence_ids"]][:2]
    return {
        "drivers": drivers,
        "dropped_drivers": dropped,
        "summary": out.get("summary"),
        "tension": out.get("tension"),
        "ungrounded_numbers": A.ungrounded_numbers(prose, allowed),
    }


def synthesize_template(agg: dict[str, Any], labelled: list[dict[str, Any]]) -> dict[str, Any]:
    """Degraded stage 2: top positive and negative aspects by impact, no prose model."""
    candidates = A.driver_candidates(agg)
    pos = [c for c in candidates if c["impact"] > 0][:2]
    neg = [c for c in candidates if c["impact"] < 0][:2]
    drivers = [{
        "aspect": c["aspect"], "direction": "positive" if c["impact"] > 0 else "negative",
        "weight": round(abs(c["impact"]), 3), "mentions": c["mentions"],
        "positive_share": c["positive_share"],
        "why": f"mentioned in {_pct(c['share_of_reviews'])} of reviews, {_pct(c['positive_share'])} of them positive",
        "evidence_ids": [], "evidence": [],
    } for c in sorted(pos + neg, key=lambda c: abs(c["impact"]), reverse=True)]
    best = pos[0]["aspect"] if pos else None
    worst = neg[0]["aspect"] if neg else None
    summary = (f"Rule-based read (no LLM): reviews' text was {_pct(agg['approval_text'])} positive "
               f"among those taking a side; strongest positive aspect {best or 'none'}, "
               f"strongest negative {worst or 'none'}.")
    return {"drivers": drivers, "dropped_drivers": [], "summary": summary, "tension": None,
            "ungrounded_numbers": []}


class ApprovalMeasure:
    name = "approval"

    def __init__(self, use_llm: bool = True, *, single_model: bool = False):
        self.use_llm = use_llm
        self.single_model = single_model

    @property
    def method(self) -> str:
        if not self.use_llm:
            return "lexicon"
        return "llm_single_model" if self.single_model else "llm_two_tier"

    async def compute(self, reviews: list[dict[str, Any]], context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        ledger = UsageLedger()
        result: dict[str, Any] = {
            "name": self.name,
            "method": self.method,
            "degraded": False,
            "degraded_reason": None,
            "n_reviews": len(reviews),
            "models": None,
        }
        if not reviews:
            return {**result, **A.aggregate([]), "drivers": [], "summary": None, "usage": ledger.summary()}

        labelled: list[dict[str, Any]] | None = None
        classify_info: dict[str, int] = {}
        tier_name = "pro" if self.single_model else "fast"
        if self.use_llm:
            try:
                if not settings.deepseek_api_key:
                    raise LLMError("DEEPSEEK_API_KEY not set")
                labelled, classify_info = await classify_llm(
                    reviews, game=context.get("game") or "this game", tier_name=tier_name, ledger=ledger)
            except LLMError as e:
                result.update(degraded=True, degraded_reason=f"classify: {str(e)[:220]}")
        if labelled is None:
            labelled = classify_lexicon(reviews)
            result["method"] = "lexicon"
        agg = A.aggregate(labelled)

        synth: dict[str, Any] | None = None
        if result["method"] != "lexicon":
            try:
                synth = await synthesize_llm(agg, labelled, context, ledger=ledger)
                result["models"] = {
                    "classify": "{} (thinking {})".format(*_tier_desc(tier_name)),
                    "synthesize": "{} (thinking {})".format(*_tier_desc("pro")),
                }
            except LLMError as e:
                # labels are already paid for; keep them and template the prose
                result.update(degraded=True, degraded_reason=f"synthesize: {str(e)[:220]}")
        if synth is None:
            synth = synthesize_template(agg, labelled)
        return {**result, **classify_info, **agg, **synth, "usage": ledger.summary(), "_calls": ledger.calls}


def _tier_desc(tier_name: str) -> tuple[str, str]:
    model, thinking = deepseek.tier(tier_name)
    return model, "on" if thinking else "off"
