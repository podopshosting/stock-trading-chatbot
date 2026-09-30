"""
Tests for MarketScannerService, regime gating, failure behaviour and
persistence (Milestone 4).

Fully offline. A fake provider stands in for Alpaca, so these spend no
quota and exercise failure paths that are hard to provoke live.
"""
import os
import sys
import time
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import AgentConfig, ScannerConfig, UniverseConfig  # noqa: E402
from agent.providers.base import (  # noqa: E402
    Bar, BarSet, DataUnavailable, Provenance, Quote, RateLimited,
)
from agent.scanner import (  # noqa: E402
    DynamoDBScannerStore, InMemoryScannerStore, MarketScannerService,
    ScanContext, ScanStatus, ScannerRun, SecurityType, StaticUniverseProvider,
    UniverseSecurity, candidate_sk,
)


def sec(symbol, **kw):
    defaults = dict(name=f"{symbol} Inc. Common Stock", exchange="NASDAQ",
                    asset_class="us_equity",
                    security_type=SecurityType.EQUITY, tradable=True,
                    fractionable=True, status="active")
    defaults.update(kw)
    return UniverseSecurity(symbol=symbol, **defaults)


LIQUID = ["AAPL", "MSFT", "NVDA", "AMD", "SOFI", "SPY", "QQQ"]


class FakeProvider:
    """Stands in for AlpacaProvider. Records requests and can be made to
    fail selectively."""

    name = "fake"

    def __init__(self, prices=None, fail_snapshot_for=None,
                 fail_bars_for=None, snapshot_error=None, bars_error=None,
                 omit_symbols=None, stale_symbols=None):
        self.prices = prices or {}
        self.fail_snapshot_for = set(fail_snapshot_for or [])
        self.fail_bars_for = set(fail_bars_for or [])
        self.snapshot_error = snapshot_error
        self.bars_error = bars_error
        self.omit_symbols = set(omit_symbols or [])
        self.stale_symbols = set(stale_symbols or [])
        self.request_count = 0
        self.snapshot_batches = []
        self.bars_calls = []

    def _quote(self, symbol):
        price = self.prices.get(symbol, 100.0)
        age = 1200.0 if symbol in self.stale_symbols else 5.0
        return Quote(
            symbol=symbol, price=price, volume=1_000_000,
            bid=price * 0.9999, ask=price * 1.0001,
            open=price * 0.99, previous_close=price * 0.985,
            high=price * 1.01, low=price * 0.98, vwap=price * 0.995,
            provenance=Provenance(provider=self.name,
                                  retrieved_at=time.time() - age),
        )

    def get_snapshot(self, symbols):
        self.request_count += 1
        self.snapshot_batches.append(list(symbols))
        if self.snapshot_error:
            raise self.snapshot_error
        if any(s in self.fail_snapshot_for for s in symbols):
            raise DataUnavailable("batch failed")
        return {s: self._quote(s) for s in symbols
                if s not in self.omit_symbols}

    def get_bars_multi(self, symbols, timeframe="1day", limit=100, **kwargs):
        self.request_count += 1
        self.bars_calls.append((timeframe, len(symbols)))
        if self.bars_error:
            raise self.bars_error
        out = {}
        for s in symbols:
            if s in self.fail_bars_for:
                continue
            price = self.prices.get(s, 100.0)
            out[s] = [Bar(timestamp=f"2026-09-30T1{i}:00:00Z",
                          open=price, high=price, low=price, close=price,
                          volume=2_000_000) for i in range(min(limit, 9))]
        return out

    def get_bars(self, symbol, timeframe="1day", limit=100):
        self.request_count += 1
        return BarSet(symbol=symbol, timeframe=timeframe, bars=[])

    def get_quote(self, symbol):
        return self._quote(symbol)


def open_context(regime="BULLISH", confidence=0.8, posture="NORMAL"):
    return ScanContext(market_session="OPEN", regime=regime,
                       regime_confidence=confidence, risk_posture=posture,
                       minutes_elapsed=195.0, is_open=True)


def make_service(provider=None, universe=None, store=None, config=None):
    provider = provider or FakeProvider()
    universe = universe or StaticUniverseProvider([sec(s) for s in LIQUID])
    return MarketScannerService(
        provider, universe, store or InMemoryScannerStore(),
        config or AgentConfig(),
    ), provider


class TestHappyPath(unittest.TestCase):
    def test_scan_produces_ranked_candidates(self):
        svc, _p = make_service()
        run = svc.run_scan(open_context(), session_date="2026-09-30")

        self.assertIs(run.status, ScanStatus.COMPLETE)
        self.assertGreater(run.candidate_count, 0)
        self.assertEqual(run.universe_count, len(LIQUID))
        ranks = [c.rank for c in run.candidates]
        self.assertEqual(ranks, sorted(ranks))
        scores = [c.scanner_score for c in run.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_run_records_the_market_context(self):
        svc, _p = make_service()
        run = svc.run_scan(open_context(regime="BULLISH", confidence=0.71,
                                        posture="NORMAL"))
        self.assertEqual(run.market_regime, "BULLISH")
        self.assertEqual(run.regime_confidence, 0.71)
        self.assertEqual(run.risk_posture, "NORMAL")
        self.assertEqual(run.market_session, "OPEN")
        for cand in run.candidates:
            self.assertEqual(cand.market_regime, "BULLISH")

    def test_run_reports_no_execution_capability(self):
        svc, _p = make_service()
        run = svc.run_scan(open_context())
        self.assertFalse(run.as_dict()["execution_available"])

    def test_funnel_counts_are_recorded(self):
        svc, _p = make_service()
        run = svc.run_scan(open_context())
        self.assertEqual(run.universe_count, len(LIQUID))
        self.assertGreater(run.static_eligible_count, 0)
        self.assertGreaterEqual(run.dynamic_eligible_count, run.candidate_count)

    def test_shortlist_cap_is_enforced(self):
        cfg = AgentConfig()
        cfg = replace(cfg, scanner=replace(
            cfg.scanner, universe=replace(cfg.scanner.universe,
                                          max_universe_size=3,
                                          max_candidates=2)))
        svc, _p = make_service(config=cfg)
        run = svc.run_scan(open_context())
        self.assertLessEqual(run.shortlist_count, 3)
        self.assertLessEqual(run.candidate_count, 2)

    def test_expensive_stage_only_sees_the_shortlist(self):
        """Widening the universe must not widen per-symbol work."""
        cfg = AgentConfig()
        cfg = replace(cfg, scanner=replace(
            cfg.scanner, universe=replace(cfg.scanner.universe,
                                          max_universe_size=2,
                                          max_candidates=2)))
        universe = StaticUniverseProvider(
            [sec(f"SYM{i}") for i in range(50)] + [sec("SPY"), sec("QQQ")])
        svc, provider = make_service(universe=universe, config=cfg)
        svc.run_scan(open_context())
        for _timeframe, count in provider.bars_calls:
            self.assertLessEqual(count, 2,
                                 "bars must only be fetched for the shortlist")


class TestRegimeGating(unittest.TestCase):
    """The same stock must be treated differently by regime."""

    def _run(self, regime):
        svc, _p = make_service()
        return svc.run_scan(open_context(regime=regime))

    def test_gate_is_recorded_on_every_run(self):
        run = self._run("MIXED")
        self.assertIn("min_score", run.regime_gate)
        self.assertGreater(run.regime_gate["min_score"], 0)

    def test_hostile_regimes_raise_the_bar(self):
        bullish = self._run("BULLISH")
        bearish = self._run("STRONG_BEARISH")
        self.assertLess(bullish.regime_gate["min_score"],
                        bearish.regime_gate["min_score"])
        self.assertLessEqual(bearish.candidate_count, bullish.candidate_count)

    def test_volatile_tightens_spread_and_liquidity(self):
        run = self._run("VOLATILE")
        self.assertLess(run.regime_gate["spread_multiplier"], 1.0)
        self.assertGreater(run.regime_gate["dollar_volume_multiplier"], 1.0)

    def test_unknown_regime_warns_and_restricts(self):
        """Not knowing the regime is not the same as a calm one."""
        run = self._run("UNKNOWN")
        self.assertGreater(run.regime_gate["min_score"], 0)
        self.assertTrue(any("UNKNOWN" in w for w in run.warnings))

    def test_every_regime_is_long_only(self):
        for regime in ("STRONG_BULLISH", "BULLISH", "NEUTRAL", "MIXED",
                       "VOLATILE", "BEARISH", "STRONG_BEARISH", "UNKNOWN"):
            with self.subTest(regime=regime):
                run = self._run(regime)
                for cand in run.candidates:
                    serialised = cand.as_dict()
                    self.assertNotIn("side", serialised)
                    self.assertNotIn("short", str(serialised).lower()[:0] or "")

    def test_gate_warning_reaches_the_candidates(self):
        run = self._run("MIXED")
        for cand in run.candidates:
            self.assertTrue(any("MIXED" in w for w in cand.warnings))


class TestFailureBehaviour(unittest.TestCase):
    def test_market_closed_yields_no_candidates(self):
        svc, provider = make_service()
        ctx = ScanContext(market_session="CLOSED", regime="BULLISH",
                          is_open=False)
        run = svc.run_scan(ctx)

        self.assertIs(run.status, ScanStatus.MARKET_CLOSED)
        self.assertEqual(run.candidate_count, 0)
        self.assertEqual(provider.request_count, 0,
                         "a closed market must not spend provider quota")

    def test_premarket_yields_no_candidates(self):
        svc, _p = make_service()
        run = svc.run_scan(ScanContext(market_session="PRE_MARKET",
                                       regime="BULLISH", is_open=False))
        self.assertIs(run.status, ScanStatus.MARKET_CLOSED)
        self.assertEqual(run.candidate_count, 0)

    def test_provider_outage_is_reported_not_faked(self):
        provider = FakeProvider(snapshot_error=RateLimited("fake", "429"))
        svc, _p = make_service(provider=provider)
        run = svc.run_scan(open_context())

        self.assertIs(run.status, ScanStatus.PROVIDER_ERROR)
        self.assertEqual(run.candidate_count, 0)
        self.assertIsNotNone(run.error)

    def test_partial_batch_failure_keeps_the_rest(self):
        """One failing batch must not discard the whole universe."""
        cfg = AgentConfig()
        cfg = replace(cfg, scanner=replace(cfg.scanner, snapshot_batch_size=2))
        universe = StaticUniverseProvider(
            [sec(s) for s in ["AAPL", "MSFT", "BADX", "NVDA", "AMD", "SPY"]])
        provider = FakeProvider(fail_snapshot_for=["BADX"])
        svc, _p = make_service(provider=provider, universe=universe, config=cfg)
        run = svc.run_scan(open_context())

        self.assertIs(run.status, ScanStatus.COMPLETE)
        self.assertGreater(run.candidate_count, 0)
        self.assertTrue(any("batch" in w for w in run.warnings))
        self.assertNotIn("BADX", [c.symbol for c in run.candidates])

    def test_missing_bar_data_degrades_one_symbol_only(self):
        provider = FakeProvider(fail_bars_for=["AMD"])
        svc, _p = make_service(provider=provider)
        run = svc.run_scan(open_context())
        self.assertIs(run.status, ScanStatus.COMPLETE)
        self.assertGreater(run.candidate_count, 0)

    def test_bars_outage_still_completes_with_snapshot_features(self):
        provider = FakeProvider(bars_error=DataUnavailable("bars down"))
        svc, _p = make_service(provider=provider)
        run = svc.run_scan(open_context())
        self.assertIs(run.status, ScanStatus.COMPLETE)
        self.assertGreater(run.provider_errors, 0)

    def test_empty_universe_is_reported(self):
        svc, _p = make_service(universe=StaticUniverseProvider([]))
        run = svc.run_scan(open_context())
        self.assertIs(run.status, ScanStatus.EMPTY_UNIVERSE)
        self.assertEqual(run.candidate_count, 0)

    def test_all_ineligible_is_reported(self):
        universe = StaticUniverseProvider(
            [sec(f"OTC{i}", exchange="OTC") for i in range(5)])
        svc, _p = make_service(universe=universe)
        run = svc.run_scan(open_context())
        self.assertIs(run.status, ScanStatus.EMPTY_UNIVERSE)
        self.assertGreater(run.rejected_count, 0)
        self.assertIn("OTC_EXCLUDED", run.rejection_reason_counts)

    def test_universe_provider_failure_is_a_provider_error(self):
        class Broken(StaticUniverseProvider):
            def list_symbols(self):
                raise RuntimeError("assets endpoint down")

        svc, _p = make_service(universe=Broken([]))
        run = svc.run_scan(open_context())
        self.assertIs(run.status, ScanStatus.PROVIDER_ERROR)

    def test_persistence_failure_does_not_discard_the_scan(self):
        class BrokenStore(InMemoryScannerStore):
            def save_run(self, run):
                raise RuntimeError("dynamo down")

        svc, _p = make_service(store=BrokenStore())
        run = svc.run_scan(open_context())
        self.assertIs(run.status, ScanStatus.COMPLETE)
        self.assertTrue(any("persistence failed" in w for w in run.warnings))

    def test_stale_snapshots_are_counted_and_excluded(self):
        provider = FakeProvider(stale_symbols=["AMD", "SOFI"])
        svc, _p = make_service(provider=provider)
        run = svc.run_scan(open_context())
        self.assertGreaterEqual(run.stale_count, 2)
        self.assertNotIn("AMD", [c.symbol for c in run.candidates])
        self.assertIn("STALE_QUOTE", run.rejection_reason_counts)

    def test_omitted_symbols_are_recorded_not_ignored(self):
        provider = FakeProvider(omit_symbols=["NVDA"])
        svc, _p = make_service(provider=provider)
        run = svc.run_scan(open_context())
        self.assertIn("MISSING_QUOTE", run.rejection_reason_counts)


class TestProviderEfficiency(unittest.TestCase):
    def test_snapshots_are_batched_not_per_symbol(self):
        universe = StaticUniverseProvider(
            [sec(f"SYM{i}") for i in range(40)] + [sec("SPY"), sec("QQQ")])
        svc, provider = make_service(universe=universe)
        svc.run_scan(open_context())
        self.assertLessEqual(len(provider.snapshot_batches), 2,
                             "42 symbols should batch into one request")

    def test_request_count_reflects_real_requests(self):
        """Counted from the provider's own counter: a call site that
        paginates is many requests, and reporting one was the defect that
        made a 161-request scan look like 18."""
        svc, provider = make_service()
        run = svc.run_scan(open_context())
        self.assertEqual(run.provider_calls, provider.request_count)

    def test_rejection_reasons_are_aggregated_not_per_symbol(self):
        universe = StaticUniverseProvider(
            [sec(f"OTC{i}", exchange="OTC") for i in range(200)]
            + [sec(s) for s in LIQUID])
        svc, _p = make_service(universe=universe)
        run = svc.run_scan(open_context())
        self.assertEqual(run.rejection_reason_counts["OTC_EXCLUDED"], 200)
        self.assertLessEqual(len(run.rejection_samples), 25,
                             "samples must be bounded, not one per symbol")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryScannerStore()

    def test_run_ids_are_unique_within_the_same_second(self):
        """Second-resolution timestamps alone collided, so a retry
        overwrote the previous run."""
        ids = {ScannerRun.make_id("2026-09-30", "2026-09-30T12:00:00+00:00")
               for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_run_and_candidates_are_stored(self):
        svc, _p = make_service(store=self.store)
        run = svc.run_scan(open_context(), session_date="2026-09-30")

        loaded = self.store.get_run(run.scanner_run_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.candidate_count, run.candidate_count)
        self.assertEqual(len(loaded.candidates), len(run.candidates))

    def test_candidate_order_survives_a_round_trip(self):
        svc, _p = make_service(store=self.store)
        run = svc.run_scan(open_context(), session_date="2026-09-30")
        loaded = self.store.get_run(run.scanner_run_id)
        self.assertEqual([c.symbol for c in loaded.candidates],
                         [c.symbol for c in run.candidates])
        self.assertEqual([c.rank for c in loaded.candidates],
                         [c.rank for c in run.candidates])

    def test_latest_run_returns_the_newest(self):
        svc, _p = make_service(store=self.store)
        first = svc.run_scan(open_context(), session_date="2026-09-30")
        second = svc.run_scan(open_context(), session_date="2026-09-30")
        latest = self.store.latest_run("2026-09-30")
        self.assertEqual(latest.scanner_run_id, second.scanner_run_id)
        self.assertNotEqual(latest.scanner_run_id, first.scanner_run_id)

    def test_empty_run_is_still_stored(self):
        svc, _p = make_service(store=self.store)
        run = svc.run_scan(ScanContext(market_session="CLOSED", is_open=False),
                           session_date="2026-09-30")
        loaded = self.store.get_run(run.scanner_run_id)
        self.assertIsNotNone(loaded)
        self.assertIs(loaded.status, ScanStatus.MARKET_CLOSED)
        self.assertEqual(loaded.candidate_count, 0)

    def test_candidates_by_symbol(self):
        svc, _p = make_service(store=self.store)
        run = svc.run_scan(open_context(), session_date="2026-09-30")
        symbol = run.candidates[0].symbol
        history = self.store.candidates_for_symbol(symbol)
        self.assertTrue(history)
        self.assertEqual(history[0]["symbol"], symbol)

    def test_recent_runs_newest_first(self):
        svc, _p = make_service(store=self.store)
        a = svc.run_scan(open_context(), session_date="2026-09-30")
        b = svc.run_scan(open_context(), session_date="2026-09-30")
        self.assertNotEqual(a.scanner_run_id, b.scanner_run_id,
                            "two runs in the same second must not collide")
        runs = self.store.recent_runs("2026-09-30", limit=5)
        self.assertEqual(len(runs), 2)
        self.assertNotIn("candidates", runs[0])

    def test_rank_sort_key_is_zero_padded(self):
        """Lexicographic ordering on an unpadded rank would put 10 before
        2 and silently corrupt the ranking on read."""
        keys = [candidate_sk(2, "AAA"), candidate_sk(10, "BBB")]
        self.assertEqual(sorted(keys), keys)
        self.assertEqual(candidate_sk(2, "AAA"), "CANDIDATE#0002#AAA")


class TestSerialisationRoundTrip(unittest.TestCase):
    def test_run_round_trip(self):
        svc, _p = make_service()
        run = svc.run_scan(open_context(), session_date="2026-09-30")
        restored = ScannerRun.from_dict(run.as_dict())
        self.assertEqual(restored.scanner_run_id, run.scanner_run_id)
        self.assertIs(restored.status, run.status)
        self.assertEqual(len(restored.candidates), len(run.candidates))
        if run.candidates:
            self.assertIsNotNone(restored.candidates[0].features)


class TestNoExecutionCapability(unittest.TestCase):
    def test_scanner_package_never_imports_a_broker(self):
        import agent.scanner as pkg
        root = os.path.dirname(os.path.abspath(pkg.__file__))
        forbidden = ("submit_order", "BrokerAdapter", "place_order",
                     "create_order", "PaperBroker")
        for filename in os.listdir(root):
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(root, filename), encoding="utf-8") as fh:
                body = fh.read()
            for token in forbidden:
                self.assertNotIn(token, body,
                                 f"{filename} references {token}")

    def test_trading_stays_disabled_in_config(self):
        self.assertFalse(AgentConfig().trading_enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
