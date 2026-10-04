"""Reconstructing the regime as it was, at each replay timestamp.

Historical replay could only trade under an injected permissive regime,
so every number it produced described the strategy WITHOUT its market
filter. These tests pin the as-of boundary and the refusal to fall back
to a permissive default.
"""
from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent.replay import regime_reconstruction as RR          # noqa: E402
from agent.replay.configs import (                            # noqa: E402
    HISTORICAL_STRATEGY_REPLAY, HISTORICAL_SYNTHETIC_POLICY,
    classify_run,
)


@dataclass
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 1_000_000.0


def series(n, start=100.0, step=0.1, day0=1):
    return [Bar(f"2026-{1 + (day0 + i) // 28:02d}-"
                f"{1 + (day0 + i) % 28:02d}T05:00:00",
                start + i * step, start + i * step + 0.2,
                start + i * step - 0.2, start + i * step)
            for i in range(n)]


def benchmarks(n=120):
    return {"SPY": series(n), "QQQ": series(n, 300.0, 0.3),
            "IWM": series(n, 200.0, 0.2)}


class TestTheAsOfBoundary(unittest.TestCase):

    def test_only_bars_at_or_before_T_are_used(self):
        bench = benchmarks(120)
        mid = bench["SPY"][60].timestamp
        got = RR.build_index_input("SPY", bench["SPY"], mid)
        self.assertEqual(len(got.daily_closes), 61)   # inclusive of T
        self.assertEqual(got.price, bench["SPY"][60].close)

    def test_a_later_bar_cannot_change_an_earlier_reconstruction(self):
        """The adversarial control.

        Reconstruct at T, then append a violent future bar and
        reconstruct at the SAME T. The answer must be identical.
        """
        bench = benchmarks(120)
        t = bench["SPY"][80].timestamp
        before = RR.reconstruct(bench, t)
        for symbol in bench:
            bench[symbol] = list(bench[symbol]) + [
                Bar("2026-12-31T05:00:00", 1.0, 9999.0, 0.5, 9999.0)]
        after = RR.reconstruct(bench, t)
        self.assertEqual(before.regime, after.regime)
        self.assertEqual(before.confidence, after.confidence)
        self.assertEqual(before.risk_posture, after.risk_posture)

    def test_the_control_would_notice_a_leak(self):
        # Falsifying half: reconstructing AT the violent bar must
        # differ, or the test above proves nothing.
        bench = benchmarks(120)
        t = bench["SPY"][80].timestamp
        before = RR.reconstruct(bench, t)
        for symbol in bench:
            bench[symbol] = list(bench[symbol]) + [
                Bar("2026-12-31T05:00:00", 1.0, 9999.0, 0.5, 9999.0)]
        at_the_spike = RR.reconstruct(bench, "2026-12-31T05:00:00")
        self.assertNotEqual(
            (before.regime, before.confidence),
            (at_the_spike.regime, at_the_spike.confidence),
            "the spike changed nothing, so the control is vacuous")


class TestExplicitAvailabilityStates(unittest.TestCase):

    def test_too_little_history_is_insufficient_not_neutral(self):
        bench = benchmarks(10)
        got = RR.reconstruct(bench, bench["SPY"][-1].timestamp)
        self.assertEqual(got.availability, RR.INSUFFICIENT_DATA)
        self.assertFalse(got.established)
        self.assertIn("Too little history", got.detail)

    def test_too_few_benchmarks_is_insufficient(self):
        # One index is not a breadth measure.
        bench = {"SPY": series(120)}
        got = RR.reconstruct(bench, bench["SPY"][-1].timestamp)
        self.assertEqual(got.availability, RR.INSUFFICIENT_DATA)
        self.assertIn("1 of 3", got.detail)

    def test_two_of_three_benchmarks_is_enough(self):
        # One index may be halted or missing a bar without voiding the
        # whole reconstruction.
        bench = benchmarks(120)
        del bench["IWM"]
        got = RR.reconstruct(bench, bench["SPY"][-1].timestamp)
        self.assertEqual(got.availability, RR.OBSERVED_RECONSTRUCTED)
        self.assertEqual(set(got.benchmarks_used), {"SPY", "QQQ"})
        self.assertEqual(got.benchmarks_missing, ("IWM",))

    def test_a_hole_in_a_benchmark_refuses_that_benchmark(self):
        # A gap makes every average over the window wrong by an unknown
        # amount; dropping the bad bars would silently shorten it.
        bench = benchmarks(120)
        bench["SPY"][50] = Bar("2026-02-20T05:00:00", 1, 1, 1, None)  # type: ignore
        got = RR.build_index_input("SPY", bench["SPY"],
                                   bench["SPY"][-1].timestamp)
        self.assertIsNone(got)

    def test_a_benchmark_with_no_bar_yet_is_missing_not_zero(self):
        bench = benchmarks(120)
        got = RR.build_index_input("SPY", bench["SPY"],
                                   "2020-01-01T00:00:00")
        self.assertIsNone(got)


class TestNoSilentPermissiveFallback(unittest.TestCase):
    """The property that makes 'faithful' mean anything."""

    def test_an_unestablished_regime_REFUSES(self):
        bench = benchmarks(10)
        got = RR.reconstruct(bench, bench["SPY"][-1].timestamp)
        d = got.as_regime_dict()
        self.assertEqual(d["regime"], "UNKNOWN")
        # NO_NEW_TRADES, not NORMAL: it must refuse exactly as the live
        # system refuses when the regime cannot be established.
        self.assertEqual(d["risk_posture"], "NO_NEW_TRADES")
        self.assertEqual(d["regime_confidence"], 0.0)

    def test_it_never_emits_bullish_for_an_unknown_regime(self):
        for n in (0, 1, 5, 20, 49):
            bench = benchmarks(n) if n else {}
            ts = (bench["SPY"][-1].timestamp if n
                  else "2026-01-01T05:00:00")
            d = RR.reconstruct(bench, ts).as_regime_dict()
            self.assertNotEqual(d["regime"], "BULLISH", f"n={n}")
            self.assertNotEqual(d["risk_posture"], "NORMAL", f"n={n}")

    def test_an_established_regime_passes_the_engines_own_answer(self):
        # Falsifying control: if it always returned UNKNOWN, every test
        # above would pass while nothing ever traded.
        bench = benchmarks(120)
        got = RR.reconstruct(bench, bench["SPY"][-1].timestamp)
        self.assertEqual(got.availability, RR.OBSERVED_RECONSTRUCTED)
        self.assertIsNotNone(got.regime)
        self.assertNotEqual(got.regime, "UNKNOWN")
        d = got.as_regime_dict()
        self.assertEqual(d["regime"], got.regime)
        self.assertEqual(d["risk_posture"], got.risk_posture)

    def test_an_engine_failure_is_unknown_not_neutral(self):
        class Exploding:
            def evaluate(self, _market_data):
                raise RuntimeError("boom")
        got = RR.reconstruct(benchmarks(120),
                             benchmarks(120)["SPY"][-1].timestamp,
                             engine=Exploding())
        self.assertEqual(got.availability, RR.UNKNOWN)
        self.assertIn("RuntimeError", got.detail)
        self.assertEqual(got.as_regime_dict()["risk_posture"],
                         "NO_NEW_TRADES")


class TestItUsesTheDeployedEngine(unittest.TestCase):

    def test_it_calls_the_live_regime_engine(self):
        # A reimplementation here would be a second model that agrees
        # with the first until it doesn't, invisibly.
        with open(os.path.join(REPO, "agent", "replay",
                               "regime_reconstruction.py")) as fh:
            src = fh.read()
        self.assertIn("from ..market.regime import IndexInput, "
                      "MarketRegimeEngine", src)
        self.assertIn("engine.evaluate(inputs)", src)

    def test_intraday_bars_is_a_list_not_none(self):
        """IndexInput declares a List with a list default.

        Passing None made session_vwap raise on every bar, which the
        reconstruction reported as UNKNOWN - the fault was here, not in
        the engine.
        """
        got = RR.build_index_input("SPY", series(120),
                                   series(120)[-1].timestamp)
        self.assertEqual(got.intraday_bars, [])
        self.assertIsNotNone(got.intraday_bars)

    def test_the_benchmarks_match_the_engines_weighting(self):
        with open(os.path.join(REPO, "agent", "market",
                               "regime.py")) as fh:
            src = fh.read()
        for symbol in RR.BENCHMARKS:
            self.assertIn(f'"{symbol}"', src, symbol)


class TestRunClassification(unittest.TestCase):

    def test_only_reconstructed_and_decision_is_faithful(self):
        got = classify_run(regime_source="OBSERVED_RECONSTRUCTED",
                           stop_model="DECISION",
                           config_name="DEFAULT_LIVE_CONFIG",
                           deployable=True)
        self.assertEqual(got["label"], HISTORICAL_STRATEGY_REPLAY)
        self.assertTrue(got["faithful"])
        self.assertEqual(got["why_not"], [])

    def test_a_synthetic_regime_is_never_strategy_performance(self):
        got = classify_run(regime_source="SYNTHETIC_PERMISSIVE",
                           stop_model="DECISION",
                           config_name="DEFAULT_LIVE_CONFIG",
                           deployable=True)
        self.assertEqual(got["label"], HISTORICAL_SYNTHETIC_POLICY)
        self.assertFalse(got["may_be_called_strategy_performance"])
        self.assertTrue(any("market filter" in w for w in got["why_not"]))

    def test_a_fixed_stop_is_also_not_faithful(self):
        got = classify_run(regime_source="OBSERVED_RECONSTRUCTED",
                           stop_model="FIXED",
                           config_name="DEFAULT_LIVE_CONFIG",
                           deployable=True)
        self.assertFalse(got["faithful"])
        self.assertTrue(any("different strategy" in w
                            for w in got["why_not"]))

    def test_a_test_only_config_is_never_faithful(self):
        got = classify_run(regime_source="OBSERVED_RECONSTRUCTED",
                           stop_model="DECISION",
                           config_name="GAP_RISK_TEST",
                           deployable=False)
        self.assertFalse(got["faithful"])
        self.assertTrue(any("not deployable" in w for w in got["why_not"]))


if __name__ == "__main__":
    unittest.main()
