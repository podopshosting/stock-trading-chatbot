"""
Tests for the controlled market scanner (Milestone 4).

Deterministic and offline: no AWS, no brokerage, no network.

The properties that matter most are the controls — that ineligible
securities are rejected with reasons, that stale data cannot be ranked
beside fresh data, that the regime actually changes what is surfaced, and
that nothing here can express a trade.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import (  # noqa: E402
    AgentConfig, ScannerConfig, ScannerWeights, UniverseConfig,
)
from agent.providers.base import Bar, Provenance, Quote  # noqa: E402
from agent.scanner import (  # noqa: E402
    Candidate, DataFreshness, InMemoryScannerStore, MarketScannerService,
    RejectionReason, ScanContext, ScanStatus, ScannerRun, ScannerScorer,
    ScannerSnapshot, SecurityType, StaticUniverseProvider, UniverseSecurity,
    average_daily_volume, candidate_sk, classify_freshness, compute_features,
    dynamic_eligibility, elapsed_session_fraction, looks_leveraged,
    rank_candidates, relative_volume, static_eligibility,
)
from agent.scanner.features import RELATIVE_VOLUME_BASIS  # noqa: E402

POLICY = UniverseConfig()


def security(symbol="AAPL", **kw):
    defaults = dict(
        name="Apple Inc. Common Stock", exchange="NASDAQ",
        asset_class="us_equity", security_type=SecurityType.EQUITY,
        tradable=True, fractionable=True, status="active", leveraged=False,
    )
    defaults.update(kw)
    return UniverseSecurity(symbol=symbol, **defaults)


def snapshot(symbol="AAPL", price=100.0, volume=1_000_000, bid=99.98,
             ask=100.02, age=10.0, **kw):
    snap = ScannerSnapshot(
        symbol=symbol, price=price, volume=volume, bid=bid, ask=ask,
        age_seconds=age, provider="test", session_open=99.0,
        previous_close=98.5, day_high=101.0, day_low=98.0, vwap=99.5,
    )
    for key, value in kw.items():
        setattr(snap, key, value)
    snap.freshness = classify_freshness(age, POLICY.max_data_age_seconds)
    return snap


def bars(closes, volume=1_000_000):
    return [Bar(timestamp=f"2026-09-30T{13 + i // 12:02d}:{(i % 12) * 5:02d}:00Z",
                open=c, high=c * 1.001, low=c * 0.999, close=c, volume=volume)
            for i, c in enumerate(closes)]


# ---------------------------------------------------------------- universe

class TestStaticEligibility(unittest.TestCase):
    def test_ordinary_equity_is_eligible(self):
        ok, reasons = static_eligibility(security(), POLICY)
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_inactive_is_rejected(self):
        ok, reasons = static_eligibility(security(status="delisted"), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.INACTIVE.value, reasons)

    def test_not_tradable_is_rejected(self):
        ok, reasons = static_eligibility(security(tradable=False), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.NOT_TRADABLE.value, reasons)

    def test_otc_is_rejected(self):
        ok, reasons = static_eligibility(security(exchange="OTC"), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.OTC_EXCLUDED.value, reasons)

    def test_unsupported_asset_class_is_rejected(self):
        ok, reasons = static_eligibility(security(asset_class="crypto"), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.UNSUPPORTED_ASSET_CLASS.value, reasons)

    def test_etf_accepted_when_configured(self):
        etf = security("SPY", name="SPDR S&P 500 ETF Trust",
                       exchange="ARCA", security_type=SecurityType.ETF)
        self.assertTrue(static_eligibility(etf, POLICY)[0])

    def test_etf_rejected_when_disallowed(self):
        from dataclasses import replace
        etf = security("SPY", name="SPDR S&P 500 ETF Trust",
                       exchange="ARCA", security_type=SecurityType.ETF)
        ok, reasons = static_eligibility(etf, replace(POLICY, allow_etfs=False))
        self.assertFalse(ok)
        self.assertIn(RejectionReason.ETF_EXCLUDED.value, reasons)

    def test_leveraged_etf_is_rejected(self):
        lev = security("TQQQ", name="ProShares UltraPro QQQ",
                       exchange="ARCA", security_type=SecurityType.ETF,
                       leveraged=True)
        ok, reasons = static_eligibility(lev, POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.LEVERAGED_ETF_EXCLUDED.value, reasons)

    def test_unknown_security_type_is_rejected(self):
        """Unknown is not benign: an unclassifiable security cannot be
        checked against the type policy at all."""
        ok, reasons = static_eligibility(
            security(security_type=SecurityType.UNKNOWN), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.UNKNOWN_SECURITY_TYPE.value, reasons)

    def test_all_failures_are_reported_not_just_the_first(self):
        bad = security(exchange="OTC", tradable=False, status="inactive")
        _ok, reasons = static_eligibility(bad, POLICY)
        self.assertGreaterEqual(len(reasons), 3)

    def test_fractionable_requirement_when_enabled(self):
        from dataclasses import replace
        ok, reasons = static_eligibility(
            security(fractionable=False),
            replace(POLICY, require_fractionable=True))
        self.assertFalse(ok)
        self.assertIn(RejectionReason.NOT_FRACTIONABLE.value, reasons)


class TestLeveragedDetection(unittest.TestCase):
    """Validated against the live universe: 0 false negatives across 22
    known leveraged/inverse products, 0 false positives across 20 ordinary
    ones."""

    INVERSE_OR_LEVERAGED = [
        "ProShares Short Russell2000", "ProShares Short S&P500",
        "ProShares Short QQQ", "ProShares UltraShort S&P500",
        "ProShares UltraPro QQQ", "ProShares UltraPro Short QQQ",
        "ProShares Ultra QQQ", "Direxion Daily Small Cap Bear 3x ETF",
        "Direxion Daily Semiconductor Bull 3X ETF",
        "GraniteShares 2x Short NVDA Daily ETF",
    ]
    ORDINARY = [
        "SPDR S&P 500 ETF Trust", "Invesco QQQ Trust",
        "Schwab Short-Term U.S. Treasury ETF",
        "PIMCO Enhanced Short Maturity Active Exchange-Traded Fund",
        "iShares Short Duration Bond Active ETF",
        "Vanguard Total Stock Market ETF",
    ]

    def test_inverse_and_leveraged_are_detected(self):
        for name in self.INVERSE_OR_LEVERAGED:
            with self.subTest(name=name):
                self.assertTrue(looks_leveraged(name, SecurityType.ETF),
                                f"missed leveraged/inverse product: {name}")

    def test_ordinary_funds_are_not_flagged(self):
        for name in self.ORDINARY:
            with self.subTest(name=name):
                self.assertFalse(looks_leveraged(name, SecurityType.ETF),
                                 f"wrongly flagged ordinary fund: {name}")

    def test_short_duration_is_not_short_the_index(self):
        """A naive \\bshort\\b match excluded 217 legitimate bond funds."""
        self.assertFalse(looks_leveraged(
            "Schwab Short-Term U.S. Treasury ETF", SecurityType.ETF))
        self.assertTrue(looks_leveraged(
            "ProShares Short S&P500", SecurityType.ETF))

    def test_operating_companies_are_never_flagged(self):
        """The pattern hits 12 real companies in the live universe."""
        for name in ("Ultra Clean Holdings, Inc. Common Stock",
                     "10x Genomics, Inc. Class A Common Stock",
                     "Ultragenyx Pharmaceutical Inc. Common Stock"):
            with self.subTest(name=name):
                self.assertFalse(looks_leveraged(name, SecurityType.EQUITY))


# --------------------------------------------------------------- liquidity

class TestDynamicEligibility(unittest.TestCase):
    def test_liquid_symbol_passes(self):
        ok, reasons = dynamic_eligibility(snapshot(price=100, volume=1_000_000),
                                          POLICY)
        self.assertTrue(ok, reasons)

    def test_spread_within_limit_passes(self):
        self.assertTrue(dynamic_eligibility(
            snapshot(bid=99.9, ask=100.1), POLICY)[0])

    def test_spread_too_wide_is_rejected(self):
        ok, reasons = dynamic_eligibility(snapshot(bid=99.0, ask=101.0), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.SPREAD_TOO_WIDE.value, reasons)

    def test_unknown_spread_is_rejected_distinctly(self):
        """An unknown spread is not a tight one, and it is reported
        separately from a known-too-wide spread."""
        ok, reasons = dynamic_eligibility(
            snapshot(bid=None, ask=None), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.SPREAD_UNKNOWN.value, reasons)
        self.assertNotIn(RejectionReason.SPREAD_TOO_WIDE.value, reasons)

    def test_price_below_minimum_is_rejected(self):
        ok, reasons = dynamic_eligibility(
            snapshot(price=2.0, bid=1.99, ask=2.01), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.PRICE_BELOW_MINIMUM.value, reasons)
        self.assertIn(RejectionReason.PENNY_STOCK_EXCLUDED.value, reasons)

    def test_price_above_maximum_is_rejected(self):
        ok, reasons = dynamic_eligibility(
            snapshot(price=5000.0, bid=4999.0, ask=5001.0), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.PRICE_ABOVE_MAXIMUM.value, reasons)

    def test_dollar_volume_below_minimum_is_rejected(self):
        ok, reasons = dynamic_eligibility(snapshot(price=10, volume=1000),
                                          POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.DOLLAR_VOLUME_BELOW_MINIMUM.value, reasons)

    def test_sufficient_dollar_volume_passes(self):
        self.assertTrue(dynamic_eligibility(
            snapshot(price=100, volume=500_000), POLICY)[0])

    def test_avg_daily_volume_enforced_when_present(self):
        snap = snapshot(price=100, volume=1_000_000)
        snap.avg_daily_volume = 1000
        ok, reasons = dynamic_eligibility(snap, POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.VOLUME_BELOW_MINIMUM.value, reasons)

    def test_missing_quote_short_circuits(self):
        snap = ScannerSnapshot(symbol="X", error="unavailable")
        ok, reasons = dynamic_eligibility(snap, POLICY)
        self.assertFalse(ok)
        self.assertEqual(reasons, [RejectionReason.MISSING_QUOTE.value])

    def test_regime_multipliers_tighten_limits(self):
        snap = snapshot(bid=99.8, ask=100.2)       # 0.40% spread
        self.assertTrue(dynamic_eligibility(snap, POLICY)[0])
        ok, reasons = dynamic_eligibility(snap, POLICY, spread_multiplier=0.5)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.SPREAD_TOO_WIDE.value, reasons)


class TestFreshness(unittest.TestCase):
    def test_labels(self):
        self.assertIs(classify_freshness(10, 1800), DataFreshness.FRESH)
        self.assertIs(classify_freshness(600, 1800), DataFreshness.STALE)
        self.assertIs(classify_freshness(9999, 1800), DataFreshness.MISSING)
        self.assertIs(classify_freshness(None, 1800), DataFreshness.MISSING)

    def test_stale_quote_is_rejected_by_default(self):
        ok, reasons = dynamic_eligibility(snapshot(age=900.0), POLICY)
        self.assertFalse(ok)
        self.assertIn(RejectionReason.STALE_QUOTE.value, reasons)

    def test_stale_allowed_only_when_configured(self):
        self.assertTrue(dynamic_eligibility(
            snapshot(age=900.0), POLICY, allow_stale=True)[0])


# ---------------------------------------------------------------- features

class TestRelativeVolume(unittest.TestCase):
    def test_denominator_is_stated(self):
        """Calling a partial session's volume "relative volume" against a
        full-day average would be a quiet lie."""
        self.assertIn("projected", RELATIVE_VOLUME_BASIS.lower())
        self.assertIn("NOT a same-time-of-day", RELATIVE_VOLUME_BASIS)

    def test_projection_uses_elapsed_fraction(self):
        # Half the session gone, 1M traded -> 2M projected against 2M ADV.
        self.assertAlmostEqual(
            relative_volume(1_000_000, 2_000_000, 0.5), 1.0, places=6)

    def test_none_when_elapsed_is_unknown(self):
        """Projecting from an unknown elapsed time would invent a
        denominator."""
        self.assertIsNone(relative_volume(1_000_000, 2_000_000, None))

    def test_none_without_an_average(self):
        self.assertIsNone(relative_volume(1_000_000, None, 0.5))

    def test_elapsed_fraction_is_clamped(self):
        self.assertAlmostEqual(elapsed_session_fraction(195), 0.5, places=3)
        self.assertEqual(elapsed_session_fraction(500), 1.0)
        self.assertIsNone(elapsed_session_fraction(0))
        self.assertIsNone(elapsed_session_fraction(None))


class TestAverageDailyVolume(unittest.TestCase):
    def test_excludes_the_partial_session(self):
        """Including today would drag the average down all morning and
        make every symbol look unusually active."""
        series = bars([100] * 5, volume=1_000_000)
        series.append(Bar("today", 100, 100, 100, 100, volume=1))
        self.assertAlmostEqual(average_daily_volume(series), 1_000_000, places=0)

    def test_none_without_data(self):
        self.assertIsNone(average_daily_volume([]))


class TestComputeFeatures(unittest.TestCase):
    def test_session_change_and_vwap(self):
        feat = compute_features(snapshot(price=100.0, vwap=99.0),
                                minutes_elapsed=195)
        self.assertAlmostEqual(feat.session_change_pct, (100 - 99) / 99 * 100,
                               places=4)
        self.assertAlmostEqual(feat.distance_from_vwap_pct,
                               (100 - 99) / 99 * 100, places=4)
        self.assertTrue(feat.above_vwap)

    def test_at_vwap_is_neither_above_nor_below(self):
        feat = compute_features(snapshot(price=100.0, vwap=100.0))
        self.assertIsNone(feat.above_vwap)

    def test_short_window_returns(self):
        series = bars([100, 101, 102, 103, 104, 105, 106, 107])
        feat = compute_features(snapshot(price=107.0), intraday_bars=series,
                                timeframe_minutes=5)
        self.assertIsNotNone(feat.return_5m)
        self.assertIsNotNone(feat.return_15m)
        self.assertGreater(feat.return_15m, 0)

    def test_returns_are_none_without_enough_bars(self):
        feat = compute_features(snapshot(price=100.0),
                                intraday_bars=bars([100]),
                                timeframe_minutes=5)
        self.assertIsNone(feat.return_5m)

    def test_range_position(self):
        feat = compute_features(snapshot(price=101.0, day_high=101.0,
                                         day_low=98.0))
        self.assertAlmostEqual(feat.range_position, 1.0, places=4)

    def test_relative_strength_versus_benchmark(self):
        feat = compute_features(snapshot(price=100.0, session_open=99.0),
                                benchmark_change_pct=0.5,
                                benchmark_symbol="SPY")
        self.assertAlmostEqual(feat.market_relative_strength,
                               feat.session_change_pct - 0.5, places=6)
        self.assertEqual(feat.benchmark_symbol, "SPY")


# ----------------------------------------------------------------- scoring

class TestScoring(unittest.TestCase):
    def setUp(self):
        self.scorer = ScannerScorer()

    def test_weights_sum_to_one(self):
        ScannerWeights().validate()

    def test_bad_weights_are_rejected(self):
        with self.assertRaises(ValueError):
            ScannerWeights(liquidity=0.9).validate()

    def test_deterministic(self):
        snap = snapshot(price=100, volume=2_000_000)
        feat = compute_features(snap, minutes_elapsed=195)
        a, da = self.scorer.score(snap, feat)
        b, db = self.scorer.score(snap, feat)
        self.assertEqual(a, b)
        self.assertEqual(da, db)

    def test_score_is_bounded(self):
        for price, volume in ((100, 50_000_000), (6, 100)):
            snap = snapshot(price=price, volume=volume)
            score, _ = self.scorer.score(snap, compute_features(snap))
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 100.0)

    def test_components_are_auditable(self):
        snap = snapshot(price=100, volume=2_000_000)
        _score, detail = self.scorer.score(snap, compute_features(
            snap, minutes_elapsed=195))
        for name in ("liquidity", "short_term_momentum", "data_quality"):
            self.assertIn(name, detail)
            self.assertIn(f"{name}_contribution", detail)
        self.assertIn("active_weight", detail)

    def test_more_liquid_scores_higher(self):
        thin = snapshot(price=50, volume=500_000, bid=49.8, ask=50.2)
        deep = snapshot(price=50, volume=20_000_000, bid=49.99, ask=50.01)
        s_thin, _ = self.scorer.score(thin, compute_features(thin))
        s_deep, _ = self.scorer.score(deep, compute_features(deep))
        self.assertGreater(s_deep, s_thin)

    def test_stale_snapshot_is_penalised(self):
        fresh = snapshot(age=10.0, price=100, volume=2_000_000)
        stale = snapshot(age=900.0, price=100, volume=2_000_000)
        s_fresh, _ = self.scorer.score(fresh, compute_features(fresh))
        s_stale, detail = self.scorer.score(stale, compute_features(stale))
        self.assertLess(s_stale, s_fresh)
        self.assertIn("stale_penalty_applied", detail)

    def test_missing_components_are_redistributed_not_zeroed(self):
        """A missing feature must not be indistinguishable from a poor
        one."""
        snap = snapshot(price=100, volume=2_000_000)
        full = compute_features(snap, intraday_bars=bars([100, 101, 102, 103]),
                                benchmark_change_pct=0.1, minutes_elapsed=195)
        sparse = compute_features(snap)
        _s1, d1 = self.scorer.score(snap, full)
        _s2, d2 = self.scorer.score(snap, sparse)
        self.assertLess(d2["active_weight"], d1["active_weight"])

    def test_score_meaning_disclaims_prediction(self):
        cand = Candidate(candidate_id="c", scanner_run_id="r", symbol="AAPL",
                         timestamp="now")
        self.assertIn("NOT an expected return", cand.score_meaning)


class TestRanking(unittest.TestCase):
    def make(self, symbol, score):
        c = Candidate(candidate_id=Candidate.make_id("r", symbol),
                      scanner_run_id="r", symbol=symbol, timestamp="now")
        c.scanner_score = score
        return c

    def test_higher_score_ranks_first(self):
        ranked = rank_candidates([self.make("A", 10), self.make("B", 90),
                                  self.make("C", 50)])
        self.assertEqual([c.symbol for c in ranked], ["B", "C", "A"])
        self.assertEqual([c.rank for c in ranked], [1, 2, 3])

    def test_ties_break_on_symbol_and_are_stable(self):
        """Equal scores must not swap places between runs on the same
        data, or a ranking cannot be audited."""
        first = rank_candidates([self.make("ZZZ", 50), self.make("AAA", 50),
                                 self.make("MMM", 50)])
        second = rank_candidates([self.make("MMM", 50), self.make("ZZZ", 50),
                                  self.make("AAA", 50)])
        self.assertEqual([c.symbol for c in first], ["AAA", "MMM", "ZZZ"])
        self.assertEqual([c.symbol for c in first],
                         [c.symbol for c in second])


class TestCandidateShape(unittest.TestCase):
    def test_candidate_cannot_express_a_trade(self):
        """A candidate that reads as an instruction would be a step toward
        execution by accident."""
        cand = Candidate(candidate_id="c", scanner_run_id="r", symbol="AAPL",
                         timestamp="now")
        serialised = cand.as_dict()
        for forbidden in ("side", "entry", "entry_price", "target",
                          "target_price", "stop", "stop_price", "quantity",
                          "action", "order"):
            self.assertNotIn(forbidden, serialised,
                             f"candidate must not carry {forbidden}")

    def test_candidate_ids_are_stable(self):
        self.assertEqual(Candidate.make_id("run1", "AAPL"),
                         Candidate.make_id("run1", "AAPL"))
        self.assertNotEqual(Candidate.make_id("run1", "AAPL"),
                            Candidate.make_id("run2", "AAPL"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
