import pytest

from app.measures import aspects as A


def review(i, overall, voted_up=True, mentions=(), substantive=True, playtime=600):
    return {
        "id": str(i), "text": f"review {i}", "voted_up": voted_up, "playtime_at_review_min": playtime,
        "label": {"overall": overall, "substantive": substantive,
                  "aspects": [{"aspect": a, "detail": d, "sentiment": s, "confidence": c}
                              for a, d, s, c in mentions]},
    }


def test_normalize_coerces_and_dedupes():
    lab = A.normalize_label({
        "overall": "Positive",
        "aspects": [
            {"aspect": "Performance", "sentiment": "negative", "confidence": 0.4, "detail": "Lag"},
            {"aspect": "performance", "sentiment": "negative", "confidence": 0.9, "detail": "crashes"},
            {"aspect": "made-up", "sentiment": "positive", "confidence": 2},
            {"aspect": "visuals", "sentiment": "great", "confidence": 1},
        ],
    })
    assert lab["overall"] == "positive"
    by = {a["aspect"]: a for a in lab["aspects"]}
    assert by["performance"]["detail"] == "crashes"       # most confident kept
    assert by["other"]["confidence"] == 1.0               # unknown key -> other, confidence clamped
    assert "visuals" not in by                            # invalid sentiment dropped


def test_normalize_rejects_bad_overall():
    assert A.normalize_label({"overall": "ecstatic"}) is None


@pytest.mark.parametrize("pos,neg,expected", [(0.5, 0.5, 1.0), (0.9, 0.1, 0.2), (0.3, 0.2, 0.4), (1.0, 0.0, 0.0)])
def test_divisiveness(pos, neg, expected):
    assert A.divisiveness(pos, neg) == pytest.approx(expected)


def test_aggregate_numbers():
    rows = [
        review(1, "positive", True, [("content", "lots to do", "positive", 0.9)]),
        review(2, "positive", True, [("content", "lots to do", "positive", 0.8), ("performance", "crash", "negative", 0.9)]),
        review(3, "negative", True, [("performance", "crash", "negative", 0.9)]),   # vote/text mismatch
        review(4, "mixed", False, [("content", "", "negative", 0.3)]),               # below confidence floor
        review(5, "neutral", True, substantive=False),
    ]
    agg = A.aggregate(rows)
    assert agg["n_substantive"] == 4
    assert agg["approval_text"] == pytest.approx(2 / 3, abs=1e-3)
    assert agg["text_polarity"]["mixed"] == 0.25
    assert agg["divisiveness"] == 0.5                      # 2 * min(0.5, 0.25)
    assert agg["vote_text_mismatch"] == pytest.approx(1 / 3, abs=1e-3)
    content = next(a for a in agg["aspects"] if a["aspect"] == "content")
    assert (content["mentions"], content["positive"], content["impact"]) == (2, 2, 0.5)
    perf = next(a for a in agg["aspects"] if a["aspect"] == "performance")
    assert perf["impact"] == -0.5 and perf["positive_share"] == 0.0


def test_candidates_need_enough_mentions():
    rows = [review(i, "positive", True, [("content", "x", "positive", 0.9)]) for i in range(3)]
    rows += [review(10, "negative", False, [("audio", "x", "negative", 0.9)])]
    keys = [c["aspect"] for c in A.driver_candidates(A.aggregate(rows))]
    assert keys == ["content"]


def _cands():
    rows = [review(i, "positive", True, [("content", "x", "positive", 0.9)]) for i in range(5)]
    rows += [review(10 + i, "negative", False, [("performance", "x", "negative", 0.9)]) for i in range(4)]
    return A.driver_candidates(A.aggregate(rows))


def test_validate_drivers_drops_unsupported_claims():
    kept, dropped = A.validate_drivers([
        {"aspect": "performance", "direction": "positive", "why": "wrong sign"},
        {"aspect": "story", "direction": "positive", "why": "not a candidate"},
        {"aspect": "content", "direction": "positive", "why": "ok", "evidence_ids": ["1", "999"]},
    ], _cands(), exemplar_ids={"1", "2"})
    assert [k["aspect"] for k in kept] == ["content"]
    assert kept[0]["evidence_ids"] == ["1"]                # unknown id removed
    assert kept[0]["weight"] == pytest.approx(5 / 9, abs=1e-3)  # from the data, not the model
    assert {d["aspect"] for d in dropped} == {"performance", "story"}


def test_ungrounded_numbers():
    allowed = A.allowed_percentages({"approval": 0.83, "share": 0.4, "n": 1200})
    assert A.ungrounded_numbers("Approval rose to 83% and 40% praised it.", allowed) == []
    assert A.ungrounded_numbers("About 65% complained.", allowed) == ["65%"]


def test_exemplars_cover_both_sides():
    rows = [review(1, "positive", True, [("content", "x", "positive", 0.9)]),
            review(2, "positive", True, [("content", "x", "positive", 0.7)]),
            review(3, "negative", False, [("content", "x", "negative", 0.8)])]
    cands = [{"aspect": "content", "impact": 0.3}]
    ex = A.pick_exemplars(rows, cands)
    assert [(e["id"], e["sentiment"]) for e in ex] == [("1", "positive"), ("3", "negative")]
