from datetime import datetime, timezone

import pytest

from app.clients.deepseek import LLMError, UsageLedger, cost_usd, is_peak, parse_json, tier


@pytest.mark.parametrize("raw", [
    '{"ok": true}',
    '```json\n{"ok": true}\n```',
    '{"ok":`true`}',                      # seen from deepseek-flash, 22 Oct 2026
    'Sure! {"ok": true} hope that helps',
])
def test_parse_json_repairs(raw):
    assert parse_json(raw) == {"ok": True}


def test_parse_json_gives_up_cleanly():
    with pytest.raises(LLMError):
        parse_json("not json at all")


def at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def test_peak_hours():
    assert is_peak(at("2026-10-22 02:30"))       # Thursday 01-04
    assert is_peak(at("2026-10-22 09:59"))       # Thursday 06-10
    assert not is_peak(at("2026-10-22 05:00"))
    assert not is_peak(at("2026-10-22 14:00"))
    assert not is_peak(at("2026-10-24 02:30"))   # Saturday


def test_cost_peak_and_off_peak():
    usage = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000,
             "completion_tokens": 1_000_000}
    assert cost_usd("deepseek-flash", usage, at("2026-10-22 02:00")) == pytest.approx(0.006 + 0.30 + 1.20)
    assert cost_usd("deepseek-flash", usage, at("2026-10-22 14:00")) == pytest.approx((0.006 + 0.30 + 1.20) / 2)
    assert cost_usd("deepseek-v4-pro", usage, at("2026-10-22 02:00")) == pytest.approx(0.044 + 1.32 + 3.96)
    assert cost_usd("deepseek-chat", usage, at("2026-10-22 02:00")) == cost_usd("deepseek-flash", usage, at("2026-10-22 02:00"))


def test_ledger_excludes_cached_calls_from_cost():
    led = UsageLedger()
    row = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 0, "completion_tokens": 50,
           "reasoning_tokens": 10, "cost_usd": 0.01, "latency_s": 1.0}
    led.record({**row, "stage": "classify", "cached": False})
    led.record({**row, "stage": "classify", "cached": True})
    led.record({**row, "stage": "synthesize", "cached": False, "cost_usd": 0.05})
    s = led.summary()
    assert (s["calls"], s["cached_calls"]) == (3, 1)
    assert s["cost_usd"] == pytest.approx(0.06)
    assert s["by_stage"]["classify"]["prompt_tokens"] == 100


def test_tiers():
    assert tier("fast") == ("deepseek-flash", False)
    assert tier("pro") == ("deepseek-v4-pro", True)


def test_scrub_removes_echoed_key_fragments():
    from app.clients.deepseek import scrub
    msg = "Authentication Fails, Your api key: ****abcd is invalid; also sk-live_12345abc"
    out = scrub(msg)
    assert "abcd" not in out and "sk-live" not in out
    assert scrub("DEEPSEEK_API_KEY not set") == "DEEPSEEK_API_KEY not set"
