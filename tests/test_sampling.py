from app.pipeline.sampling import DAY, allocate, is_eligible, make_strata


def test_strata_are_days_for_short_windows():
    s = make_strata(0, 28 * DAY - 1)
    assert len(s) == 28
    assert s[0] == (0, DAY - 1) and s[-1][1] == 28 * DAY - 1


def test_long_windows_use_multi_day_slices_capped():
    s = make_strata(0, 180 * DAY - 1, max_strata=31)
    assert len(s) <= 31
    assert s[0][1] - s[0][0] + 1 == 6 * DAY
    assert s[-1][1] == 180 * DAY - 1


def test_allocation_is_proportional_and_exact():
    q = allocate(200, [8000, 1000, 1000, 0])
    assert sum(q) == 200
    assert q == [160, 20, 20, 0]


def test_allocation_largest_remainder():
    q = allocate(10, [1, 1, 1])
    assert sum(q) == 3        # can't take more than exists
    q = allocate(10, [5, 5, 5])
    assert sum(q) == 10 and max(q) - min(q) <= 1


def test_allocation_empty():
    assert allocate(50, [0, 0]) == [0, 0]


def test_eligibility_rules():
    end = 100 * DAY
    ok = {"text": "x" * 40, "updated": end}
    assert is_eligible(ok, window_end=end, min_chars=20, edit_grace_days=7) is None
    assert is_eligible({**ok, "text": "gg"}, window_end=end, min_chars=20, edit_grace_days=7) == "too_short"
    late = {**ok, "updated": end + 8 * DAY}
    assert is_eligible(late, window_end=end, min_chars=20, edit_grace_days=7) == "edited_later"
    grace = {**ok, "updated": end + 6 * DAY}
    assert is_eligible(grace, window_end=end, min_chars=20, edit_grace_days=7) is None
