import pytest

from app.pipeline.timeline import select_windows, two_proportion_z


def group(ts, type_="patch", delta=None, z=None, n_b=500, n_a=500):
    sig = z is not None and abs(z) >= 2
    return {"ts": ts, "type": type_, "title": f"g{ts}",
            "movement": {"delta": delta, "z": z, "n_before": n_b, "n_after": n_a, "significant": sig}}


def test_two_proportion_z_known_value():
    # 50% of 1000 -> 60% of 1000
    assert two_proportion_z(500, 1000, 600, 1000) == pytest.approx(4.49, abs=0.01)


def test_two_proportion_z_needs_both_sides():
    assert two_proportion_z(0, 0, 10, 20) is None


def test_movers_takes_launch_then_biggest_significant_moves():
    groups = [
        group(5, delta=0.05, z=3.0),
        group(4, delta=-0.20, z=-9.0),
        group(3, delta=0.30, z=1.5),              # big but not significant
        group(2, delta=0.10, z=4.0, n_b=10),     # too few reviews before
        group(1, delta=0.15, z=5.0),
        group(0, type_="launch"),
    ]
    chosen = select_windows(groups, k=3, strategy="movers")
    assert [g["ts"] for g in chosen] == [0, 4, 1]
    assert chosen[0]["selection_reason"] == "launch"
    assert "-20.0 pts" in chosen[1]["selection_reason"]
    assert all(g["selected"] == (g in chosen) for g in groups)


def test_recent_takes_newest_k():
    groups = [group(3), group(2), group(1)]
    assert [g["ts"] for g in select_windows(groups, k=2, strategy="recent")] == [3, 2]


def test_zero_k_selects_nothing():
    assert select_windows([group(0, type_="launch")], k=0, strategy="movers") == []


def test_unknown_strategy():
    with pytest.raises(ValueError):
        select_windows([], strategy="vibes")
