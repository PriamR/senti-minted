import asyncio

from app import cache


def test_put_then_get_within_ttl():
    cache.put("k", "steam_news", {"a": 1}, ttl=100, now=1000)
    assert cache.get("k", now=1050) == {"a": 1}


def test_expired_entry_is_a_miss():
    cache.put("k", "steam_news", {"a": 1}, ttl=100, now=1000)
    assert cache.get("k", now=1101) is None


def test_missing_key_is_none():
    assert cache.get("nope") is None


def test_per_source_ttls_match_the_plan():
    assert cache.TTL["steam_histogram"] == 24 * cache.HOUR
    assert cache.TTL["steam_news"] == 6 * cache.HOUR
    assert cache.TTL["steam_reviews"] == 7 * cache.DAY
    assert cache.TTL["rawg"] == cache.TTL["igdb"] == 30 * cache.DAY
    assert cache.TTL["itad"] == 1 * cache.HOUR


def test_cached_fetches_once_and_counts_hits():
    calls = []

    async def fetch():
        calls.append(1)
        return {"v": len(calls)}

    async def run():
        a = await cache.cached("rawg", "search", {"q": "x"}, fetch)
        b = await cache.cached("rawg", "search", {"q": "x"}, fetch)
        return a, b

    a, b = asyncio.run(run())
    assert a == b == {"v": 1}
    assert len(calls) == 1
    s = cache.stats()
    assert (s["hits"], s["misses"], s["hit_rate"]) == (1, 1, 0.5)
    assert s["by_source"]["rawg"] == {"hit": 1, "miss": 1}


def test_different_params_are_different_keys():
    assert cache.make_key("rawg", "search", {"q": "a"}) != cache.make_key("rawg", "search", {"q": "b"})
    # key order doesn't matter
    assert cache.make_key("s", "e", {"a": 1, "b": 2}) == cache.make_key("s", "e", {"b": 2, "a": 1})


def test_bypass_forces_a_fresh_fetch():
    n = {"calls": 0}

    async def fetch():
        n["calls"] += 1
        return n["calls"]

    async def run():
        await cache.cached("llm", "x", {}, fetch)
        cache.bypass("llm")
        return await cache.cached("llm", "x", {}, fetch)

    assert asyncio.run(run()) == 2


def test_decorator_keys_on_arguments():
    seen = []

    @cache.cached_call("igdb")
    async def lookup(x, *, y=0):
        seen.append((x, y))
        return x + y

    async def run():
        return [await lookup(1, y=2), await lookup(1, y=2), await lookup(2)]

    assert asyncio.run(run()) == [3, 3, 2]
    assert seen == [(1, 2), (2, 0)]
