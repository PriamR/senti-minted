"""The degraded path: no key (blanked by conftest) -> lexicon, marked degraded."""
import asyncio

from app.measures.approval import ApprovalMeasure

REVIEWS = [
    {"id": str(i), "text": t, "timestamp": 0, "updated": 0, "voted_up": up, "language": "english",
     "playtime_at_review_min": 120}
    for i, (t, up) in enumerate([
        ("The graphics are beautiful and exploring planets is relaxing.", True),
        ("Constant crashes, so buggy it is unplayable. Refund.", False),
        ("Great updates from the devs, the game is so much better now.", True),
        ("Boring and repetitive, the content is shallow.", False),
        ("Beautiful visuals, great music, love the exploration.", True),
    ] * 4)
]


def test_missing_key_degrades_to_lexicon():
    res = asyncio.run(ApprovalMeasure(use_llm=True).compute(REVIEWS, {"game": "Test"}))
    assert res["degraded"] is True
    assert "DEEPSEEK_API_KEY" in res["degraded_reason"]
    assert res["method"] == "lexicon"
    assert res["n_substantive"] > 0
    assert res["drivers"], "the template still names drivers from the aspect table"
    assert res["usage"]["calls"] == 0
    assert res["summary"].startswith("Rule-based read")


def test_no_llm_is_lexicon_but_not_degraded():
    res = asyncio.run(ApprovalMeasure(use_llm=False).compute(REVIEWS))
    assert res["method"] == "lexicon"
    assert res["degraded"] is False


def test_empty_window():
    res = asyncio.run(ApprovalMeasure(use_llm=False).compute([]))
    assert res["n_reviews"] == 0 and res["drivers"] == []
