"""
Tests for signal orchestration and persistence.

No AWS, no network: the provider and store are injected fakes. What is
under test is the orchestration contract - that one failing symbol
cannot abort a run, that provider cost is attributed honestly, that
freshness is read rather than assumed, and that nothing here acquires
execution semantics.
"""
import os
import random
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.signals import (                                   # noqa: E402
    InMemorySignalStore, SignalDirection, SignalService,
)
from agent.signals.models import DataFreshness, SignalRun      # noqa: E402


def series(n=260, start=100.0, drift=0.002, vol=0.010, seed=3):
    rng = random.Random(seed)
    prices = [start]
    for _ in range(n - 1):
        prices.append(max(0.01, prices[-1] * (1 + drift + rng.gauss(0, vol))))
    return prices


class FakeProvenance:
    def __init__(self, retrieved_at, provider="fake", is_delayed=False):
        self.retrieved_at = retrieved_at
        self.provider = provider
        self.as_of = "2026-09-30T19:00:00Z"
        self.is_delayed = is_delayed
        self.note = "test feed"


_UNSET = object()


class FakeQuote:
    def __init__(self, price, retrieved_at=_UNSET):
        # Sentinel, not `if retrieved_at is not None`: that default
        # swallowed an explicit None and the "undated quote" tests were
        # silently running against a fresh timestamp.
        import time
        self.price = price
        if retrieved_at is _UNSET:
            retrieved_at = time.time()
        self.provenance = FakeProvenance(retrieved_at)


class FakeBars:
    def __init__(self, prices):
        self._prices = prices

    def closes(self):
        return list(self._prices)


class FakeProvider:
    """Counts calls so provider cost can be asserted, not assumed."""

    def __init__(self, prices_by_symbol=None, fail=()):
        self.prices = prices_by_symbol or {}
        self.fail = set(fail)
        self.quote_calls = 0
        self.bars_calls = 0

    def get_quote(self, symbol):
        self.quote_calls += 1
        if symbol in self.fail:
            raise RuntimeError("provider exploded")
        prices = self.prices.get(symbol, series())
        return FakeQuote(prices[-1])

    def get_bars(self, symbol, timeframe, limit=None):
        self.bars_calls += 1
        if symbol in self.fail:
            raise RuntimeError("provider exploded")
        return FakeBars(self.prices.get(symbol, series()))


class FakeCandidate:
    def __init__(self, symbol, rank):
        self.symbol = symbol
        self.rank = rank


class FakeScannerRun:
    def __init__(self, symbols):
        self.scanner_run_id = "scan_abc123"
        self.session_date = "2026-09-30"
        self.market_regime = "MIXED"
        self.regime_confidence = 0.42
        self.market_session = "OPEN"
        self.candidates = [FakeCandidate(s, i + 1)
                           for i, s in enumerate(symbols)]


class TestEvaluateSymbol(unittest.TestCase):

    def test_evaluates_a_symbol_end_to_end(self):
        svc = SignalService(FakeProvider())
        r = svc.evaluate_symbol("AAPL", "BULLISH", 0.6)
        self.assertTrue(r.analysis_available)
        self.assertEqual(r.symbol, "AAPL")
        self.assertEqual(r.market_regime, "BULLISH")
        self.assertEqual(len(r.indicator_results), 6)

    def test_provider_failure_becomes_a_stated_absence_not_an_exception(self):
        svc = SignalService(FakeProvider(fail=["BAD"]))
        r = svc.evaluate_symbol("BAD")
        self.assertFalse(r.analysis_available)
        self.assertFalse(r.direction.is_directional)
        self.assertTrue(any("unavailable" in w for w in r.warnings))

    def test_freshness_is_read_from_provenance_not_assumed(self):
        import time
        provider = FakeProvider()
        provider.get_quote = lambda s: FakeQuote(100.0,
                                                 retrieved_at=time.time() - 900)
        svc = SignalService(provider)
        r = svc.evaluate_symbol("OLD")
        self.assertEqual(r.data_quality["freshness"], "STALE")
        self.assertTrue(any("stale" in w.lower() for w in r.warnings))

    def test_an_undated_quote_is_unknown_not_fresh(self):
        """Treating a quote with no timestamp as current is how stale
        data gets presented as a live reading."""
        provider = FakeProvider()
        provider.get_quote = lambda s: FakeQuote(100.0, retrieved_at=None)
        svc = SignalService(provider)
        r = svc.evaluate_symbol("NODATE")
        self.assertEqual(r.data_quality["freshness"], "UNKNOWN")

    def test_undated_quote_still_has_none_age_rather_than_zero(self):
        provider = FakeProvider()
        provider.get_quote = lambda s: FakeQuote(100.0, retrieved_at=None)
        r = SignalService(provider).evaluate_symbol("NODATE")
        self.assertIsNone(r.data_quality["age_seconds"])


class TestRun(unittest.TestCase):

    def test_run_evaluates_every_requested_symbol(self):
        svc = SignalService(FakeProvider())
        run = svc.run(["AAPL", "MSFT", "NVDA"], "NEUTRAL", 0.5)
        self.assertEqual(run.requested_count, 3)
        self.assertEqual(run.evaluated_count, 3)
        self.assertEqual(len(run.results), 3)

    def test_one_failing_symbol_does_not_abort_the_run(self):
        svc = SignalService(FakeProvider(fail=["BAD"]))
        run = svc.run(["AAPL", "BAD", "NVDA"])
        self.assertEqual(run.evaluated_count, 2)
        self.assertEqual(run.error_count, 1)
        self.assertEqual(len(run.results), 3)

    def test_top_n_bounds_the_work(self):
        """Deep analysis costs a request per symbol. The scanner exists
        to decide what is worth that."""
        provider = FakeProvider()
        svc = SignalService(provider, top_n=2)
        run = svc.run(["A", "B", "C", "D", "E"])
        self.assertEqual(run.requested_count, 2)
        self.assertEqual(provider.bars_calls, 2)

    def test_provider_calls_are_counted_per_request_not_per_symbol(self):
        """
        A count that under-reports makes a quota look safe when it is
        not - this project has already shipped a scanner that reported
        18 calls against 161 actual.
        """
        provider = FakeProvider()
        svc = SignalService(provider)
        run = svc.run(["A", "B", "C"])
        self.assertEqual(run.provider_calls,
                         provider.quote_calls + provider.bars_calls)
        self.assertEqual(run.provider_calls, 6)

    def test_direction_counts_are_recorded(self):
        svc = SignalService(FakeProvider())
        run = svc.run(["A", "B"])
        self.assertEqual(sum(run.direction_counts.values()),
                         run.evaluated_count)

    def test_run_ids_do_not_collide_within_one_second(self):
        """Second-resolution timestamps collided in the scanner and the
        second run silently overwrote the first."""
        ids = {SignalRun.make_id("2026-09-30", "2026-09-30T19:00:00")
               for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_run_carries_no_execution_capability(self):
        svc = SignalService(FakeProvider())
        run = svc.run(["A"])
        self.assertFalse(run.execution_available)
        self.assertFalse(run.as_dict()["execution_available"])


class TestScannerIntegration(unittest.TestCase):

    def test_enriches_scanner_candidates_in_rank_order(self):
        svc = SignalService(FakeProvider(), top_n=3)
        run = svc.run_for_scanner(FakeScannerRun(["AAA", "BBB", "CCC", "DDD"]))
        self.assertEqual([r.symbol for r in run.results],
                         ["AAA", "BBB", "CCC"])

    def test_carries_the_scanner_context_forward(self):
        svc = SignalService(FakeProvider())
        run = svc.run_for_scanner(FakeScannerRun(["AAA"]))
        self.assertEqual(run.scanner_run_id, "scan_abc123")
        self.assertEqual(run.market_regime, "MIXED")
        self.assertEqual(run.market_session, "OPEN")
        self.assertEqual(run.session_date, "2026-09-30")

    def test_candidates_are_taken_by_rank_not_by_list_order(self):
        scan = FakeScannerRun(["ZZZ", "YYY", "XXX"])
        scan.candidates = [FakeCandidate("ZZZ", 3), FakeCandidate("YYY", 1),
                           FakeCandidate("XXX", 2)]
        svc = SignalService(FakeProvider(), top_n=2)
        run = svc.run_for_scanner(scan)
        self.assertEqual([r.symbol for r in run.results], ["YYY", "XXX"])


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.store = InMemorySignalStore()
        self.svc = SignalService(FakeProvider(), store=self.store)

    def test_run_is_persisted_and_retrievable(self):
        run = self.svc.run(["AAPL", "MSFT"])
        stored = self.store.get_run(run.signal_run_id)
        self.assertIsNotNone(stored)
        self.assertEqual(len(stored["results"]), 2)

    def test_latest_result_by_symbol(self):
        self.svc.run(["AAPL"])
        latest = self.store.latest_for_symbol("AAPL")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["symbol"], "AAPL")

    def test_symbol_lookup_is_case_insensitive(self):
        self.svc.run(["AAPL"])
        self.assertIsNotNone(self.store.latest_for_symbol("aapl"))

    def test_history_returns_newest_first(self):
        self.svc.run(["AAPL"])
        self.svc.run(["AAPL"])
        history = self.store.history_for_symbol("AAPL", limit=5)
        self.assertEqual(len(history), 2)
        self.assertGreaterEqual(history[0]["timestamp"],
                                history[1]["timestamp"])

    def test_latest_run_by_session_date(self):
        run = self.svc.run(["AAPL"], session_date="2026-09-30")
        found = self.store.latest_run("2026-09-30")
        self.assertEqual(found["signal_run_id"], run.signal_run_id)

    def test_store_does_not_hand_back_a_mutable_live_object(self):
        run = self.svc.run(["AAPL"])
        stored = self.store.get_run(run.signal_run_id)
        stored["results"][0]["direction"] = "TAMPERED"
        again = self.store.get_run(run.signal_run_id)
        self.assertNotEqual(again["results"][0]["direction"], "TAMPERED")

    def test_raw_bars_are_not_persisted(self):
        """
        Large, reproducible from the provider, and they go stale while
        looking authoritative. Only the reading and its inputs are kept.
        """
        import json
        run = self.svc.run(["AAPL"])
        blob = json.dumps(self.store.get_run(run.signal_run_id))
        self.assertNotIn("closes", blob)
        self.assertLess(len(blob), 200_000)


if __name__ == "__main__":
    unittest.main()
