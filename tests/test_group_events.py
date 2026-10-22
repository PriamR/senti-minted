from app.pipeline.timeline import DAY, group_events

T0 = 1_600_000_000


def ev(day: float, title: str = "post", type_: str = "announcement") -> dict:
    ts = int(T0 + day * DAY)
    return {"ts": ts, "date": f"d{int(day)}", "title": title, "type": type_}


def test_events_within_gap_chain_into_one_group():
    groups = group_events([ev(0), ev(5), ev(12)], chain_gap_days=7)
    assert len(groups) == 1
    assert groups[0]["n_events"] == 3


def test_gap_longer_than_chain_splits_groups():
    groups = group_events([ev(0), ev(8)], chain_gap_days=7)
    assert len(groups) == 2


def test_window_is_asymmetric_and_tail_follows_last_member():
    g = group_events([ev(0), ev(6)], before_days=7, after_days=21)[0]
    assert g["window"]["start_ts"] == T0 - 7 * DAY
    assert g["window"]["end_ts"] == T0 + 6 * DAY + 21 * DAY


def test_anchor_prefers_patch_over_announcement_then_earliest():
    g = group_events([ev(0, "teaser"), ev(1, "Patch 1.1", "patch"), ev(2, "Patch 1.2", "patch")])[0]
    assert g["title"].startswith("Patch 1.1")
    assert g["title"].endswith("(+2 more)")


def test_same_day_blitz_is_flagged():
    g = group_events([ev(0, "a"), ev(0.1, "b"), ev(0.2, "c"), ev(3, "d")])[0]
    assert g["peak_day_count"] == 3


def test_groups_are_newest_first():
    groups = group_events([ev(0), ev(30), ev(60)])
    assert [g["ts"] for g in groups] == sorted((g["ts"] for g in groups), reverse=True)


def test_nms_fixture_groups(nms_events):
    groups = group_events(nms_events)
    assert len(groups) == 93
    launch = next(g for g in groups if g["type"] == "launch")
    # pre-launch post (Aug 7), Launch (Aug 12), two support posts (Aug 13), patch notes (Aug 20)
    assert launch["n_events"] == 5
    assert launch["window"]["start_date"] == "2016-07-31"
    assert launch["window"]["end_date"] == "2016-09-10"


def test_empty():
    assert group_events([]) == []
