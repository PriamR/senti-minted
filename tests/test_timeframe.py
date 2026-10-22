from datetime import datetime, timezone

import pytest

from app.agent.tools import resolve_timeframe

LAUNCH = "2016-08-12"


def days_between(a: str, b: str) -> int:
    f = lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (f(b) - f(a)).days


def test_at_launch_is_first_six_weeks():
    r = resolve_timeframe(LAUNCH, "at launch")
    assert r["start"] == LAUNCH
    assert days_between(r["start"], r["end"]) == 45


@pytest.mark.parametrize("phrase", ["2 years in", "two years after launch", "the 2-year mark"])
def test_n_years_in_is_anniversary_window(phrase):
    r = resolve_timeframe(LAUNCH, phrase)
    assert r["start"] == "2018-07-28"   # 2018-08-12 (730 days after launch) minus 15
    assert days_between(r["start"], r["end"]) == 60


def test_first_n_months():
    r = resolve_timeframe(LAUNCH, "first 6 months")
    assert r["start"] == LAUNCH
    assert days_between(r["start"], r["end"]) == 180


def test_recently_needs_no_launch_date():
    r = resolve_timeframe("", "how is it doing recently")
    assert days_between(r["start"], r["end"]) == 60


def test_last_n_months():
    r = resolve_timeframe(LAUNCH, "last 3 months")
    assert days_between(r["start"], r["end"]) == 90


def test_unparseable_phrase_returns_error():
    assert "error" in resolve_timeframe(LAUNCH, "when the moon is full")


def test_relative_phrase_without_launch_date_errors():
    assert "error" in resolve_timeframe("", "2 years in")
