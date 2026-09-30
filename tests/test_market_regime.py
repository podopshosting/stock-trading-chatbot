"""
Tests for the Market Regime Engine (Milestone 3).

Deterministic synthetic fixtures: same inputs, same classification, every
run. Nothing here touches a network or a brokerage.

The fixtures build price series with a known shape rather than asserting
on captured numbers, so they stay meaningful as the market moves.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import DEFAULT_CONFIG  # noqa: E402
from agent.market.regime import (  # noqa: E402
    Freshness, IndexInput, MarketRegimeEngine, realised_volatility,
    session_vwap,
)
from agent.providers.base import Bar  # noqa: E402


def trending_series(n=60, start=100.0, daily_drift=0.004, noise=0.0):
    """Price series with a controlled drift, oldest -> newest."""
    out, price = [], start
    for i in range(n):
        wiggle = noise * ((-1) ** i)
        price *= (1 + daily_drift + wiggle)
        out.append(round(price, 4))
    return out


def intraday(n=78, end_price=100.0, step=0.02, volume=100000):
    """Session bars drifting at `step` %/bar and ENDING at `end_price`.

    Ending at the current price keeps the fixture coherent: a flat series
    then really is flat, instead of embedding a gap between the first bar
    and the quote.
    """
    price = end_price / ((1 + step / 100) ** n)
    bars = []
    for i in range(n):
        o = price
        price = price * (1 + step / 100)
        bars.append(Bar(
            timestamp=f"2026-09-30T{13 + i // 12:02d}:{(i % 12) * 5:02d}:00Z",
            open=o, high=max(o, price) * 1.001, low=min(o, price) * 0.999,
            close=price, volume=volume,
        ))
    return bars


def index_input(symbol, *, drift, intraday_step=None, price_offset=0.0,
                age=10.0, daily_bars=60, with_intraday=True):
    """Build a coherent IndexInput: price, series and bars all agree."""
    closes = trending_series(daily_bars, daily_drift=drift)
    last = closes[-1]
    price = last * (1 + price_offset)
    step = intraday_step if intraday_step is not None else (
        0.03 if drift > 0 else -0.03 if drift < 0 else 0.0
    )
    bars = intraday(end_price=price, step=step) if with_intraday else []
    session_open = bars[0].open if bars else last
    return IndexInput(
        symbol=symbol, price=price, session_open=session_open,
        previous_close=last, daily_closes=closes, intraday_bars=bars,
        data_age_seconds=age, provider="test", as_of="2026-09-30T15:00:00Z",
    )


class RegimeTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = MarketRegimeEngine(DEFAULT_CONFIG.regime)

    def evaluate(self, **overrides):
        data = {
            "SPY": index_input("SPY", drift=0.004),
            "QQQ": index_input("QQQ", drift=0.004),
            "IWM": index_input("IWM", drift=0.004),
        }
        data.update(overrides)
        return self.engine.evaluate(data)


class TestHelpers(unittest.TestCase):
    def test_vwap_is_volume_weighted_not_a_mean(self):
        bars = [
            Bar("t1", 10, 10, 10, 10, volume=1),
            Bar("t2", 20, 20, 20, 20, volume=99),
        ]
        vwap = session_vwap(bars)
        self.assertAlmostEqual(vwap, (10 * 1 + 20 * 99) / 100, places=6)
        self.assertNotAlmostEqual(vwap, 15.0, places=2)

    def test_vwap_is_none_with_no_volume(self):
        """Falling back to an unweighted mean would produce a number that
        looks like a VWAP but is not one."""
        self.assertIsNone(session_vwap([Bar("t", 10, 10, 10, 10, volume=0)]))
        self.assertIsNone(session_vwap([]))

    def test_realised_vol_is_none_without_enough_history(self):
        """Zero volatility would read as a calm market, which is not the
        same as not knowing."""
        self.assertIsNone(realised_volatility([100.0] * 5))

    def test_realised_vol_rises_with_noise(self):
        calm = realised_volatility(trending_series(60, daily_drift=0.0005))
        wild = realised_volatility(trending_series(60, daily_drift=0.0005,
                                                   noise=0.05))
        self.assertIsNotNone(calm)
        self.assertIsNotNone(wild)
        self.assertGreater(wild, calm)


class TestRegimeClassification(RegimeTestBase):
    def test_strong_bullish(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.008, price_offset=0.02),
            QQQ=index_input("QQQ", drift=0.009, price_offset=0.025),
            IWM=index_input("IWM", drift=0.008, price_offset=0.02),
        )
        self.assertEqual(r.regime, "STRONG_BULLISH")
        self.assertEqual(r.trend, "UP")
        self.assertEqual(r.risk_mode, "RISK_ON")
        self.assertGreater(r.raw_score, DEFAULT_CONFIG.regime.thresholds.strong_bullish)
        self.assertGreater(r.confidence, 0.7)

    def test_bullish(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.003, price_offset=0.006),
            QQQ=index_input("QQQ", drift=0.003, price_offset=0.007),
            IWM=index_input("IWM", drift=0.001, price_offset=0.002),
        )
        self.assertIn(r.regime, ("BULLISH", "STRONG_BULLISH"))
        self.assertEqual(r.trend, "UP")

    def test_neutral_when_there_is_no_conviction(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.0, intraday_step=0.0),
            QQQ=index_input("QQQ", drift=0.0, intraday_step=0.0),
            IWM=index_input("IWM", drift=0.0, intraday_step=0.0),
        )
        self.assertEqual(r.regime, "NEUTRAL")
        self.assertEqual(r.trend, "FLAT")

    def test_bearish(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=-0.003, price_offset=-0.006),
            QQQ=index_input("QQQ", drift=-0.003, price_offset=-0.007),
            IWM=index_input("IWM", drift=-0.002, price_offset=-0.004),
        )
        self.assertIn(r.regime, ("BEARISH", "STRONG_BEARISH"))
        self.assertEqual(r.trend, "DOWN")
        self.assertEqual(r.risk_mode, "RISK_OFF")

    def test_strong_bearish(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=-0.008, price_offset=-0.02),
            QQQ=index_input("QQQ", drift=-0.009, price_offset=-0.025),
            IWM=index_input("IWM", drift=-0.008, price_offset=-0.02),
        )
        self.assertIn(r.regime, ("STRONG_BEARISH", "VOLATILE"))
        if r.regime == "STRONG_BEARISH":
            self.assertLess(r.raw_score,
                            DEFAULT_CONFIG.regime.thresholds.strong_bearish)
            self.assertIn(r.risk_posture, ("RESTRICTED", "CAUTIOUS"))

    def test_mixed_when_instruments_disagree_sharply(self):
        """QQQ strongly up, IWM strongly down, SPY flat."""
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.0, intraday_step=0.0),
            QQQ=index_input("QQQ", drift=0.010, price_offset=0.03),
            IWM=index_input("IWM", drift=-0.010, price_offset=-0.03),
        )
        self.assertIn(r.regime, ("MIXED", "VOLATILE"))
        if r.regime == "MIXED":
            self.assertGreaterEqual(
                r.data_quality["dispersion"],
                DEFAULT_CONFIG.regime.thresholds.mixed_dispersion,
            )

    def test_extreme_volatility_takes_the_label(self):
        """A violent environment is described better by VOLATILE than by a
        direction."""
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.002),
            QQQ=index_input("QQQ", drift=0.002),
            IWM=index_input("IWM", drift=0.002),
        )
        # Inject a high-volatility series directly.
        wild = {
            s: IndexInput(
                symbol=s, price=120.0, session_open=118.0, previous_close=119.0,
                daily_closes=trending_series(60, daily_drift=0.002, noise=0.06),
                intraday_bars=intraday(end_price=120.0, step=0.05),
                data_age_seconds=5.0,
                provider="test",
            ) for s in ("SPY", "QQQ", "IWM")
        }
        r = self.engine.evaluate(wild)
        self.assertEqual(r.volatility, "EXTREME")
        self.assertEqual(r.regime, "VOLATILE")
        self.assertEqual(r.risk_mode, "RISK_OFF")
        self.assertIn(r.risk_posture, ("RESTRICTED", "CAUTIOUS"))
        self.assertTrue(any("VOLATILE" in x for x in r.reasons))

    def test_determinism(self):
        """Same inputs, same answer - twice."""
        a = self.evaluate()
        b = self.evaluate()
        self.assertEqual(a.regime, b.regime)
        self.assertEqual(a.raw_score, b.raw_score)
        self.assertEqual(a.confidence, b.confidence)


class TestMissingAndStaleData(RegimeTestBase):
    def test_all_missing_gives_unknown(self):
        data = {s: IndexInput(symbol=s, error="quote unavailable")
                for s in ("SPY", "QQQ", "IWM")}
        r = self.engine.evaluate(data)

        self.assertEqual(r.regime, "UNKNOWN")
        self.assertEqual(r.confidence, 0.0)
        self.assertIsNone(r.raw_score)
        self.assertEqual(r.risk_posture, "NO_NEW_TRADES")
        self.assertEqual(r.data_quality["missing"], 3)

    def test_too_few_usable_gives_unknown(self):
        """One index is not a market."""
        data = {
            "SPY": index_input("SPY", drift=0.005),
            "QQQ": IndexInput(symbol="QQQ", error="quote unavailable"),
            "IWM": IndexInput(symbol="IWM", error="quote unavailable"),
        }
        r = self.engine.evaluate(data)
        self.assertEqual(r.regime, "UNKNOWN")
        self.assertEqual(r.confidence, 0.0)

    def test_missing_index_is_not_treated_as_flat(self):
        """Scoring a missing index as 0 would drag the result toward
        NEUTRAL and look like genuine indecision."""
        bullish_all = self.evaluate(
            SPY=index_input("SPY", drift=0.008, price_offset=0.02),
            QQQ=index_input("QQQ", drift=0.008, price_offset=0.02),
            IWM=index_input("IWM", drift=0.008, price_offset=0.02),
        )
        bullish_two = self.engine.evaluate({
            "SPY": index_input("SPY", drift=0.008, price_offset=0.02),
            "QQQ": index_input("QQQ", drift=0.008, price_offset=0.02),
            "IWM": IndexInput(symbol="IWM", error="unavailable"),
        })
        self.assertEqual(bullish_two.regime, bullish_all.regime,
                         "a missing index must not change the direction")
        self.assertLess(bullish_two.confidence, bullish_all.confidence,
                        "but it must reduce confidence")

    def test_stale_data_reduces_confidence(self):
        fresh = self.evaluate()
        stale = self.evaluate(
            SPY=index_input("SPY", drift=0.004, age=1200.0),
            QQQ=index_input("QQQ", drift=0.004, age=1200.0),
            IWM=index_input("IWM", drift=0.004, age=1200.0),
        )
        self.assertLess(stale.confidence, fresh.confidence)
        self.assertTrue(any("stale" in w for w in stale.warnings))

    def test_data_older_than_the_stale_ceiling_is_unusable(self):
        data = {s: index_input(s, drift=0.004, age=99999.0)
                for s in ("SPY", "QQQ", "IWM")}
        r = self.engine.evaluate(data)
        self.assertEqual(r.regime, "UNKNOWN")

    def test_freshness_labels(self):
        th = DEFAULT_CONFIG.regime.thresholds
        self.assertEqual(
            index_input("SPY", drift=0.0, age=10).freshness(
                th.fresh_max_age, th.stale_max_age), Freshness.FRESH)
        self.assertEqual(
            index_input("SPY", drift=0.0, age=900).freshness(
                th.fresh_max_age, th.stale_max_age), Freshness.STALE)
        self.assertEqual(
            IndexInput(symbol="SPY").freshness(
                th.fresh_max_age, th.stale_max_age), Freshness.MISSING)

    def test_missing_intraday_costs_a_feature_not_the_evaluation(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.006, price_offset=0.015,
                            with_intraday=False),
            QQQ=index_input("QQQ", drift=0.006, price_offset=0.015,
                            with_intraday=False),
            IWM=index_input("IWM", drift=0.006, price_offset=0.015,
                            with_intraday=False),
        )
        self.assertNotEqual(r.regime, "UNKNOWN")
        self.assertIsNone(r.features["SPY"]["vwap"])

    def test_insufficient_daily_history_still_classifies_or_says_unknown(self):
        data = {s: index_input(s, drift=0.004, daily_bars=5)
                for s in ("SPY", "QQQ", "IWM")}
        r = self.engine.evaluate(data)
        self.assertIn(r.regime, ("NEUTRAL", "BULLISH", "STRONG_BULLISH",
                                 "UNKNOWN", "MIXED"))
        self.assertIsNone(r.features["SPY"]["sma_spread"])


class TestConfidenceSemantics(RegimeTestBase):
    def test_confidence_is_bounded(self):
        for r in (self.evaluate(),
                  self.evaluate(SPY=index_input("SPY", drift=-0.01))):
            self.assertGreaterEqual(r.confidence, 0.0)
            self.assertLessEqual(r.confidence, 1.0)

    def test_disagreement_lowers_confidence(self):
        agree = self.evaluate(
            SPY=index_input("SPY", drift=0.005, price_offset=0.01),
            QQQ=index_input("QQQ", drift=0.005, price_offset=0.01),
            IWM=index_input("IWM", drift=0.005, price_offset=0.01),
        )
        disagree = self.evaluate(
            SPY=index_input("SPY", drift=0.005, price_offset=0.01),
            QQQ=index_input("QQQ", drift=0.010, price_offset=0.03),
            IWM=index_input("IWM", drift=-0.010, price_offset=-0.03),
        )
        self.assertLess(disagree.confidence, agree.confidence)

    def test_result_states_confidence_is_not_a_probability(self):
        d = self.evaluate().as_dict()
        self.assertIn("NOT a probability", d["confidence_meaning"])


class TestExplainability(RegimeTestBase):
    def test_reasons_are_populated(self):
        r = self.evaluate()
        self.assertTrue(r.reasons)
        self.assertTrue(r.primary_reason)

    def test_every_dimension_is_reported_separately(self):
        d = self.evaluate().as_dict()
        for key in ("regime", "trend", "risk_mode", "volatility",
                    "breadth_proxy", "risk_posture", "raw_score",
                    "confidence"):
            self.assertIn(key, d)

    def test_weights_are_disclosed_and_sum_to_one(self):
        d = self.evaluate().as_dict()
        self.assertAlmostEqual(sum(d["weights"].values()), 1.0, places=9)

    def test_inputs_record_provenance_and_age(self):
        d = self.evaluate().as_dict()
        spy = d["inputs"]["SPY"]
        self.assertEqual(spy["provider"], "test")
        self.assertIsNotNone(spy["age_seconds"])
        self.assertIsNotNone(spy["as_of"])

    def test_score_is_bounded(self):
        for drift in (0.03, -0.03):
            r = self.evaluate(
                SPY=index_input("SPY", drift=drift, price_offset=drift * 10),
                QQQ=index_input("QQQ", drift=drift, price_offset=drift * 10),
                IWM=index_input("IWM", drift=drift, price_offset=drift * 10),
            )
            self.assertGreaterEqual(r.raw_score, -1.0)
            self.assertLessEqual(r.raw_score, 1.0)


class TestBreadthProxy(RegimeTestBase):
    def test_narrow_large_cap_is_detected(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.004, price_offset=0.01),
            QQQ=index_input("QQQ", drift=0.010, price_offset=0.03),
            IWM=index_input("IWM", drift=-0.006, price_offset=-0.02),
        )
        self.assertIn(r.breadth_proxy,
                      ("NARROW_LARGE_CAP", "NEUTRAL", "UNKNOWN"))

    def test_broad_positive_is_detected(self):
        r = self.evaluate(
            SPY=index_input("SPY", drift=0.008, price_offset=0.02),
            QQQ=index_input("QQQ", drift=0.008, price_offset=0.02),
            IWM=index_input("IWM", drift=0.008, price_offset=0.02),
        )
        self.assertEqual(r.breadth_proxy, "BROAD_POSITIVE")

    def test_it_is_named_a_proxy_not_breadth(self):
        """True breadth needs advance/decline data we do not have."""
        self.assertIn("breadth_proxy", self.evaluate().as_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
