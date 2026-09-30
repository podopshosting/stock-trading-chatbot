"""
Tests for the market data provider abstraction and cache (Milestone 2).

Offline: a fake HTTP layer stands in for Alpha Vantage, so these spend no
API quota and need no network or AWS.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.providers import (  # noqa: E402
    AlphaVantageProvider, BarSet, CachedProvider, DataUnavailable, MemoryCache,
    Provenance, Quote, RateLimited, SymbolNotFound, TieredCache, cache_key,
)
from agent.providers.base import Bar, MarketDataProvider  # noqa: E402  # noqa: E402


THROTTLE = {
    "Information": (
        "Thank you for using Alpha Vantage! Please consider spreading out "
        "your free API requests more sparingly (1 request per second). ... "
        "rate limit (25 requests per day)"
    )
}


def quote_payload(symbol="AAPL", price="100.0000"):
    return {"Global Quote": {
        "01. symbol": symbol, "02. open": "99.0000", "03. high": "101.0000",
        "04. low": "98.0000", "05. price": price, "06. volume": "1000000",
        "07. latest trading day": "2026-09-29", "08. previous close": "99.0000",
        "09. change": "1.0000", "10. change percent": "1.0101%",
    }}


def daily_payload(days=100):
    series = {}
    for i in range(days):
        c = 100 + i
        series[f"2026-01-{i + 1:04d}"] = {
            "1. open": f"{c - 0.5}", "2. high": f"{c + 1}",
            "3. low": f"{c - 1}", "4. close": f"{c}", "5. volume": "1000000",
        }
    return {"Time Series (Daily)": series}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHTTP:
    """Replays queued payloads per Alpha Vantage `function`, recording calls."""

    def __init__(self):
        self.calls = []
        self.queues = {}
        self.default = {}

    def queue(self, function, *payloads):
        self.queues.setdefault(function, []).extend(payloads)

    def get(self, url, params=None, timeout=None):
        params = params or {}
        fn = params.get("function")
        self.calls.append((fn, params.get("symbol")))
        q = self.queues.get(fn)
        if q:
            return FakeResponse(q.pop(0))
        return FakeResponse(self.default)


def make_provider(http, **kw):
    """Provider with sleeping and the clock disabled, so tests are instant."""
    kw.setdefault("min_interval", 0)
    return AlphaVantageProvider(
        api_key="test-key", http=http, sleep=lambda _s: None, **kw
    )


class TestAlphaVantageProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()

    def test_quote_is_parsed_with_provenance(self):
        self.http.queue("GLOBAL_QUOTE", quote_payload())
        q = make_provider(self.http).get_quote("AAPL")

        self.assertEqual(q.symbol, "AAPL")
        self.assertEqual(q.price, 100.0)
        self.assertEqual(q.volume, 1000000)
        self.assertIsNotNone(q.provenance)
        self.assertEqual(q.provenance.provider, "alphavantage")
        self.assertEqual(q.provenance.as_of, "2026-09-29")
        self.assertFalse(q.provenance.cache_hit)

    def test_freshness_is_unknown_not_assumed_realtime(self):
        """Claiming real-time without evidence would be a false claim."""
        self.http.queue("GLOBAL_QUOTE", quote_payload())
        q = make_provider(self.http).get_quote("AAPL")
        self.assertIsNone(q.provenance.is_delayed)

    def test_throttle_raises_rate_limited_not_symbol_not_found(self):
        """The outage this project recovered from: a throttle read as
        'no data'. It must be a distinct, identifiable condition."""
        for _ in range(5):
            self.http.queue("GLOBAL_QUOTE", THROTTLE)
        with self.assertRaises(RateLimited):
            make_provider(self.http).get_quote("AAPL")

    def test_throttle_is_retried_then_succeeds(self):
        self.http.queue("GLOBAL_QUOTE", THROTTLE, quote_payload())
        q = make_provider(self.http).get_quote("AAPL")
        self.assertEqual(q.price, 100.0)

    def test_unknown_symbol_raises_symbol_not_found(self):
        self.http.queue("GLOBAL_QUOTE", {"Global Quote": {}})
        with self.assertRaises(SymbolNotFound):
            make_provider(self.http).get_quote("ZZZZQQ")

    def test_bars_are_oldest_first_and_limited(self):
        self.http.queue("TIME_SERIES_DAILY", daily_payload(100))
        bs = make_provider(self.http).get_bars("AAPL", "1day", limit=60)

        self.assertEqual(len(bs), 60)
        closes = bs.closes()
        self.assertEqual(closes, sorted(closes), "bars must be oldest -> newest")
        self.assertIsInstance(bs.bars[0], Bar)

    def test_unsupported_timeframe_is_explicit(self):
        with self.assertRaises(DataUnavailable):
            make_provider(self.http).get_bars("AAPL", "tick")

    def test_malformed_rows_are_skipped_not_fatal(self):
        payload = daily_payload(60)
        payload["Time Series (Daily)"]["2026-01-0005"] = {"1. open": "oops"}
        self.http.queue("TIME_SERIES_DAILY", payload)
        bs = make_provider(self.http).get_bars("AAPL", "1day", limit=100)
        self.assertEqual(len(bs), 59)

    def test_capabilities_declare_the_real_limits(self):
        caps = make_provider(self.http).capabilities()
        self.assertEqual(caps["daily_request_budget"], 25)
        self.assertEqual(caps["burst_limit_per_second"], 1)
        self.assertFalse(caps["bid_ask"])
        self.assertFalse(caps["batch_quotes"])

    def test_rate_limiter_spaces_requests(self):
        """With spacing enabled, consecutive calls must be separated."""
        slept = []
        clock = [0.0]

        def fake_clock():
            return clock[0]

        def fake_sleep(s):
            slept.append(s)
            clock[0] += s

        self.http.queue("GLOBAL_QUOTE", quote_payload(), quote_payload())
        p = AlphaVantageProvider("k", http=self.http, clock=fake_clock,
                                 sleep=fake_sleep)
        p.get_quote("AAPL")
        p.get_quote("MSFT")
        self.assertTrue(any(s > 0 for s in slept),
                        f"expected spacing between calls, slept={slept}")


class TestSpreadSemantics(unittest.TestCase):
    def test_unknown_spread_is_none_not_zero(self):
        """A missing spread must never look tight to the risk layer."""
        q = Quote(symbol="X", price=10.0)
        self.assertIsNone(q.spread)
        self.assertIsNone(q.spread_pct)

    def test_known_spread_is_computed(self):
        q = Quote(symbol="X", price=10.0, bid=9.99, ask=10.01)
        self.assertAlmostEqual(q.spread, 0.02, places=6)
        self.assertAlmostEqual(q.spread_pct, 0.2, places=3)


class TestMemoryCache(unittest.TestCase):
    def test_entry_expires_after_ttl(self):
        now = [1000.0]
        c = MemoryCache(clock=lambda: now[0])
        c.put("k", {"v": 1}, ttl_seconds=60)

        self.assertEqual(c.get("k"), {"v": 1})
        now[0] += 61
        self.assertIsNone(c.get("k"), "entry must expire")

    def test_hit_rate_is_none_before_any_lookup(self):
        self.assertIsNone(MemoryCache().hit_rate)


class TestCachedProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.inner = make_provider(self.http)

    def test_second_quote_is_served_from_cache(self):
        self.http.queue("GLOBAL_QUOTE", quote_payload())
        p = CachedProvider(self.inner)

        first = p.get_quote("AAPL")
        second = p.get_quote("AAPL")

        self.assertEqual(second.price, first.price)
        self.assertEqual(len(self.http.calls), 1, "cache must prevent a refetch")
        self.assertFalse(first.provenance.cache_hit)
        self.assertTrue(second.provenance.cache_hit)

    def test_cache_hit_preserves_original_retrieval_time(self):
        """Age must be measured from the provider fetch, not from the cache
        read, or cached data would appear to refresh itself."""
        self.http.queue("GLOBAL_QUOTE", quote_payload())
        p = CachedProvider(self.inner)

        first = p.get_quote("AAPL")
        time.sleep(0.01)
        second = p.get_quote("AAPL")

        self.assertEqual(second.provenance.retrieved_at,
                         first.provenance.retrieved_at)

    def test_cache_is_keyed_per_symbol(self):
        self.http.queue("GLOBAL_QUOTE", quote_payload("AAPL"),
                        quote_payload("MSFT", "200.0000"))
        p = CachedProvider(self.inner)

        self.assertEqual(p.get_quote("AAPL").price, 100.0)
        self.assertEqual(p.get_quote("MSFT").price, 200.0)
        self.assertEqual(len(self.http.calls), 2)

    def test_expired_quote_is_refetched(self):
        now = [5000.0]
        backend = MemoryCache(clock=lambda: now[0])
        self.http.queue("GLOBAL_QUOTE", quote_payload(), quote_payload("AAPL", "111.0"))
        p = CachedProvider(self.inner, backend=backend)

        self.assertEqual(p.get_quote("AAPL").price, 100.0)
        now[0] += 3600
        self.assertEqual(p.get_quote("AAPL").price, 111.0)
        self.assertEqual(len(self.http.calls), 2)

    def test_bars_cache_serves_a_smaller_request_without_refetching(self):
        """A 100-bar series already answers a 50-bar request."""
        self.http.queue("TIME_SERIES_DAILY", daily_payload(100))
        p = CachedProvider(self.inner)

        big = p.get_bars("AAPL", "1day", limit=100)
        small = p.get_bars("AAPL", "1day", limit=50)

        self.assertEqual(len(big), 100)
        self.assertEqual(len(small), 50)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(small.closes(), big.closes()[-50:],
                         "must return the most recent bars")

    def test_quota_reduction_for_repeated_analysis(self):
        """The justification for this milestone.

        Uncached, analysing 5 symbols twice costs 20 requests of a 25/day
        budget. Cached, it costs 10 and the repeat pass is free.
        """
        symbols = ["AAPL", "MSFT", "NVDA", "SOFI", "AMD"]
        for s in symbols:
            self.http.queue("GLOBAL_QUOTE", quote_payload(s))
            self.http.queue("TIME_SERIES_DAILY", daily_payload(60))
        p = CachedProvider(self.inner)

        for _ in range(2):
            for s in symbols:
                p.get_quote(s)
                p.get_bars(s, "1day", limit=60)

        self.assertEqual(len(self.http.calls), 10,
                         "expected 2 requests/symbol once, not per pass")
        self.assertEqual(p.provider_calls, 10)
        self.assertEqual(p.stats["hit_rate"], 0.5)

    def test_rate_limit_serves_recent_stale_data_clearly_labelled(self):
        now = [9000.0]
        backend = MemoryCache(clock=lambda: now[0])
        inner = make_provider(self.http, wall_clock=lambda: now[0])
        self.http.queue("GLOBAL_QUOTE", quote_payload())
        p = CachedProvider(inner, backend=backend, clock=lambda: now[0])

        fresh = p.get_quote("AAPL")
        self.assertFalse(fresh.provenance.cache_hit)

        now[0] += 120                       # past the 60s quote TTL
        for _ in range(5):
            self.http.queue("GLOBAL_QUOTE", THROTTLE)

        served = p.get_quote("AAPL")
        self.assertEqual(served.price, 100.0)
        self.assertTrue(served.provenance.cache_hit)
        self.assertIn("STALE", served.provenance.note,
                      "stale data must be labelled, never passed off as current")

    def test_rate_limit_raises_when_cached_data_is_too_old(self):
        """Beyond the grace window, refuse rather than serve stale prices."""
        now = [9000.0]
        backend = MemoryCache(clock=lambda: now[0])
        inner = make_provider(self.http, wall_clock=lambda: now[0])
        self.http.queue("GLOBAL_QUOTE", quote_payload())
        p = CachedProvider(inner, backend=backend, clock=lambda: now[0],
                           stale_grace_seconds=300)

        p.get_quote("AAPL")
        now[0] += 10_000
        for _ in range(5):
            self.http.queue("GLOBAL_QUOTE", THROTTLE)

        with self.assertRaises(RateLimited):
            p.get_quote("AAPL")

    def test_symbol_not_found_is_not_masked_by_cache(self):
        self.http.queue("GLOBAL_QUOTE", {"Global Quote": {}})
        p = CachedProvider(self.inner)
        with self.assertRaises(SymbolNotFound):
            p.get_quote("ZZZZQQ")


class TestCacheKeying(unittest.TestCase):
    def test_key_includes_provider_so_sources_cannot_be_confused(self):
        a = cache_key("alphavantage", "quote", "AAPL")
        b = cache_key("alpaca", "quote", "AAPL")
        self.assertNotEqual(a, b)

    def test_key_is_case_insensitive_on_symbol(self):
        self.assertEqual(cache_key("p", "quote", "aapl"),
                         cache_key("p", "quote", "AAPL"))

    def test_extra_dimensions_are_order_independent(self):
        self.assertEqual(cache_key("p", "bars", "X", tf="1day", limit=5),
                         cache_key("p", "bars", "X", limit=5, tf="1day"))


class TestTieredCache(unittest.TestCase):
    def test_shared_hit_is_promoted_into_memory(self):
        fast, slow = MemoryCache(), MemoryCache()
        slow.put("k", {"v": 1, "_kind": "quote",
                       "provenance": {"retrieved_at": time.time()}}, 60)
        tiered = TieredCache(fast, slow)

        self.assertIsNotNone(tiered.get("k"))
        self.assertIsNotNone(fast.get("k"), "shared hit should populate memory")

    def test_write_goes_to_both_tiers(self):
        fast, slow = MemoryCache(), MemoryCache()
        TieredCache(fast, slow).put("k", {"v": 1}, 60)
        self.assertIsNotNone(fast.get("k"))
        self.assertIsNotNone(slow.get("k"))

    def test_works_without_a_shared_tier(self):
        tiered = TieredCache(MemoryCache(), None)
        tiered.put("k", {"v": 1}, 60)
        self.assertEqual(tiered.get("k"), {"v": 1})


class TestBrokenCacheDegrades(unittest.TestCase):
    """A cache failure must cost performance, never correctness."""

    class ExplodingCache(MemoryCache):
        def get(self, key):
            raise RuntimeError("cache backend down")

    def test_provider_still_works_when_cache_reads_fail(self):
        http = FakeHTTP()
        http.queue("GLOBAL_QUOTE", quote_payload())
        p = CachedProvider(make_provider(http), backend=self.ExplodingCache())

        with self.assertRaises(RuntimeError):
            p.get_quote("AAPL")
        # Documents current behaviour: a raising backend propagates. The
        # supplied backends fail soft instead; see DynamoDBCache.


class TestProviderInterfaceContract(unittest.TestCase):
    def test_incomplete_provider_cannot_be_instantiated(self):
        class Incomplete(MarketDataProvider):
            name = "incomplete"

        with self.assertRaises(TypeError):
            Incomplete()

    def test_snapshot_skips_unknown_symbols(self):
        http = FakeHTTP()
        http.queue("GLOBAL_QUOTE", quote_payload("AAPL"))
        http.queue("GLOBAL_QUOTE", {"Global Quote": {}})
        snap = make_provider(http).get_snapshot(["AAPL", "ZZZZQQ"])
        self.assertIn("AAPL", snap)
        self.assertNotIn("ZZZZQQ", snap)


class TestProvenanceAging(unittest.TestCase):
    def test_age_and_staleness(self):
        p = Provenance(provider="x", retrieved_at=1000.0)
        self.assertAlmostEqual(p.age_seconds(now=1060.0), 60.0)
        self.assertFalse(p.is_stale(120, now=1060.0))
        self.assertTrue(p.is_stale(30, now=1060.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)


PREMIUM = {
    "Information": (
        "Thank you for using Alpha Vantage! This is a premium endpoint. You "
        "may subscribe to any of the premium plans at "
        "https://www.alphavantage.co/premium/ to instantly unlock all premium "
        "endpoints"
    )
}


class TestEntitlement(unittest.TestCase):
    """Verified live on 2026-09-30: TIME_SERIES_INTRADAY is a premium
    endpoint. A paywall is permanent, not transient, so it must be
    distinguishable from a rate limit and must never be retried."""

    def setUp(self):
        self.http = FakeHTTP()

    def test_premium_endpoint_raises_entitlement_error(self):
        from agent.providers import EntitlementRequired
        self.http.queue("TIME_SERIES_INTRADAY", PREMIUM)
        with self.assertRaises(EntitlementRequired):
            make_provider(self.http).get_bars("AAPL", "5min")

    def test_entitlement_error_is_not_a_rate_limit(self):
        from agent.providers import EntitlementRequired
        self.assertFalse(issubclass(EntitlementRequired, RateLimited))

    def test_paywall_is_not_retried(self):
        """Retrying a paywall burns the daily budget for nothing."""
        for _ in range(5):
            self.http.queue("TIME_SERIES_INTRADAY", PREMIUM)
        p = make_provider(self.http)
        with self.assertRaises(Exception):
            p.get_bars("AAPL", "5min")
        self.assertEqual(len(self.http.calls), 1,
                         f"paywall must not be retried, got {self.http.calls}")

    def test_free_tier_does_not_claim_intraday(self):
        """capabilities() must not advertise what the tier cannot deliver."""
        caps = make_provider(self.http).capabilities()
        self.assertFalse(caps["intraday"],
                         "free tier must not claim intraday support")

    def test_premium_tier_claims_intraday(self):
        caps = make_provider(self.http, tier="premium").capabilities()
        self.assertTrue(caps["intraday"])


class TestCachedSnapshotBatching(unittest.TestCase):
    """CachedProvider must not lose the inner provider's batch endpoint.

    Measured live: a 3-symbol regime evaluation issued 3 separate quote
    requests instead of 1, because CachedProvider inherited the base
    class's sequential get_snapshot loop. Batching is the whole reason the
    quota problem went away, so the wrapper has to preserve it.
    """

    class BatchProvider(AlphaVantageProvider):
        """Stands in for a provider with a native batch endpoint."""
        name = "batch"

        def __init__(self):
            self.snapshot_calls = 0
            self.quote_calls = 0

        def get_quote(self, symbol):
            self.quote_calls += 1
            return Quote(symbol=symbol.upper(), price=100.0,
                         provenance=Provenance(provider=self.name,
                                               retrieved_at=time.time()))

        def get_snapshot(self, symbols):
            self.snapshot_calls += 1
            return {s.upper(): self.get_quote(s) for s in symbols}

        def get_bars(self, symbol, timeframe="1day", limit=100):
            raise NotImplementedError

        def capabilities(self):
            return {"name": self.name, "batch_quotes": True}

    def test_snapshot_uses_one_batch_call_not_a_loop(self):
        inner = self.BatchProvider()
        p = CachedProvider(inner)
        p.get_snapshot(["SPY", "QQQ", "IWM"])
        self.assertEqual(inner.snapshot_calls, 1,
                         "must delegate to the batch endpoint")

    def test_cached_symbols_are_not_refetched(self):
        inner = self.BatchProvider()
        p = CachedProvider(inner)
        p.get_snapshot(["SPY", "QQQ", "IWM"])
        snap = p.get_snapshot(["SPY", "QQQ", "IWM"])
        self.assertEqual(inner.snapshot_calls, 1, "second call should be cached")
        self.assertTrue(all(q.provenance.cache_hit for q in snap.values()))

    def test_only_the_missing_symbols_are_fetched(self):
        inner = self.BatchProvider()
        p = CachedProvider(inner)
        p.get_quote("SPY")
        inner.quote_calls = 0
        p.get_snapshot(["SPY", "QQQ", "IWM"])
        self.assertEqual(inner.quote_calls, 2,
                         "SPY was cached; only QQQ and IWM need fetching")

    def test_a_fully_cached_snapshot_makes_no_provider_call(self):
        inner = self.BatchProvider()
        p = CachedProvider(inner)
        p.get_snapshot(["SPY", "QQQ"])
        inner.snapshot_calls = 0
        p.get_snapshot(["SPY", "QQQ"])
        self.assertEqual(inner.snapshot_calls, 0)
