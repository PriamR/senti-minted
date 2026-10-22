from app.measures.lexicon import label_review


def aspects(label):
    return {a["aspect"]: a["sentiment"] for a in label["aspects"]}


def test_detects_aspect_and_polarity():
    lab = label_review("The graphics are beautiful but the game crashes constantly.", voted_up=True)
    found = aspects(lab)
    assert found["visuals"] == "positive"
    assert found["performance"] == "negative"


def test_negation_flips_polarity():
    lab = label_review("The music is not good at all, honestly.", voted_up=False)
    assert aspects(lab)["audio"] == "negative"


def test_keyword_that_is_itself_an_opinion_word():
    lab = label_review("So buggy that I gave up after an hour.", voted_up=False)
    assert aspects(lab)["performance"] == "negative"


def test_overall_follows_text_not_vote():
    lab = label_review("Terrible, boring, a waste of money. Awful.", voted_up=True)
    assert lab["overall"] == "negative"


def test_balanced_text_falls_back_to_vote():
    assert label_review("It has planets and a story.", voted_up=True)["overall"] == "positive"
    assert label_review("It has planets and a story.", voted_up=False)["overall"] == "negative"


def test_no_aspects_is_not_substantive():
    assert label_review("10/10 would buy again lol", voted_up=True)["substantive"] is False


def test_shape_matches_the_llm_labels():
    lab = label_review("Great multiplayer with friends.", voted_up=True)
    assert set(lab) == {"overall", "substantive", "aspects"}
    assert set(lab["aspects"][0]) == {"aspect", "detail", "sentiment", "confidence"}
