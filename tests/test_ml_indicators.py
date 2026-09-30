"""
Tests for the indicator library in lambda-micro/chatbot-router/ml_agent_lite.py.

Two defects are pinned here, both measured rather than asserted from
reading the code.

**The MACD signal line was not a signal line.** It was `macd * 0.9`, so
`histogram = macd - 0.9*macd = 0.1*macd` always carried the sign of
`macd`, and the test `macd > signal` reduced to `macd > 0` — i.e. to
`EMA12 > EMA26`, a plain trend check with no crossover information at all.

**Signals were counted as independent when they were not.** `analyze_stock`
averaged the confidences of votes that measured the same thing, so a
single trend observation could be counted three times and reported as
agreement.

The reference MACD in this file is written independently of the
implementation, so the two are not the same code checked against itself.
"""
import os
import random
import statistics
import sys
import unittest

ROUTER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lambda-micro", "chatbot-router",
)
if ROUTER_DIR not in sys.path:
    sys.path.insert(0, ROUTER_DIR)

from ml_agent_lite import MLTradingAgent, get_ml_recommendation  # noqa: E402


# --- independent reference implementation --------------------------------

def ref_ema_series(values, period):
    """EMA series, seeded with the SMA of the first `period` values.

    Written separately from the implementation on purpose: checking a
    function against itself proves nothing.
    """
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out = [ema]
    for v in values[period:]:
        ema = (v - ema) * k + ema
        out.append(ema)
    return out


def ref_macd(prices, fast=12, slow=26, signal_period=9):
    """Textbook MACD: EMA(fast) - EMA(slow), signalled by EMA of that line."""
    fast_series = ref_ema_series(prices, fast)
    slow_series = ref_ema_series(prices, slow)
    if not fast_series or not slow_series:
        return None
    offset = slow - fast
    macd_line = [fast_series[i + offset] - slow_series[i]
                 for i in range(len(slow_series))]
    signal_series = ref_ema_series(macd_line, signal_period)
    if not signal_series:
        return None
    return {"macd": macd_line[-1], "signal": signal_series[-1],
            "histogram": macd_line[-1] - signal_series[-1],
            "macd_line": macd_line}


def walk(n=200, start=100.0, drift=0.0, vol=0.02, seed=7):
    rng = random.Random(seed)
    price, out = start, []
    for _ in range(n):
        price *= (1 + drift + rng.gauss(0, vol))
        out.append(price)
    return out


def oscillating(n=200, start=100.0, amplitude=0.08, period=40):
    """A series that genuinely crosses back and forth, so a real signal
    line must produce histogram sign changes."""
    import math
    return [start * (1 + amplitude * math.sin(2 * math.pi * i / period))
            for i in range(n)]


class TestMacdCorrectness(unittest.TestCase):
    def setUp(self):
        self.agent = MLTradingAgent("TEST")

    def test_signal_is_not_a_fixed_multiple_of_macd(self):
        """The defect in one line: signal was always exactly 0.9 * macd."""
        ratios = []
        for seed in range(30):
            prices = walk(seed=seed)
            m = self.agent.calculate_macd(prices)
            if m and abs(m["macd"]) > 1e-9:
                ratios.append(m["signal"] / m["macd"])
        self.assertGreater(len(ratios), 20)
        self.assertGreater(
            statistics.pstdev(ratios), 1e-6,
            "signal/macd is constant, so the signal line is not an EMA of MACD",
        )

    def test_matches_an_independent_reference(self):
        for seed in (1, 2, 3, 11, 42):
            prices = walk(seed=seed)
            got = self.agent.calculate_macd(prices)
            want = ref_macd(prices)
            with self.subTest(seed=seed):
                self.assertIsNotNone(got)
                self.assertIsNotNone(want)
                self.assertAlmostEqual(got["macd"], want["macd"], places=9)
                self.assertAlmostEqual(got["signal"], want["signal"], places=9)
                self.assertAlmostEqual(got["histogram"], want["histogram"],
                                       places=9)

    def test_histogram_carries_crossover_information(self):
        """On an oscillating series the histogram must change sign
        independently of the MACD line. The old formula made that
        impossible."""
        prices = oscillating(n=240, period=50)
        disagreements = 0
        for end in range(60, len(prices)):
            m = self.agent.calculate_macd(prices[:end])
            if not m:
                continue
            if (m["macd"] > 0) != (m["histogram"] > 0):
                disagreements += 1
        self.assertGreater(
            disagreements, 0,
            "histogram sign never differs from macd sign, so no crossover "
            "information exists",
        )

    def test_macd_above_signal_is_not_just_macd_above_zero(self):
        """`macd > signal` must not reduce to `macd > 0`."""
        differing = 0
        total = 0
        for seed in range(80):
            prices = walk(n=160, seed=seed, vol=0.03)
            m = self.agent.calculate_macd(prices)
            if not m:
                continue
            total += 1
            if (m["macd"] > m["signal"]) != (m["macd"] > 0):
                differing += 1
        self.assertGreater(total, 50)
        self.assertGreater(
            differing, 0,
            "the crossover test is equivalent to a sign test on macd",
        )

    def test_requires_enough_history_for_a_real_signal_line(self):
        """A 9-period EMA of MACD needs 26 + 9 - 1 = 34 closes. Returning a
        number before then would be inventing one."""
        self.assertIsNone(self.agent.calculate_macd(walk(n=30)))
        self.assertIsNotNone(self.agent.calculate_macd(walk(n=40)))

    def test_flat_series_gives_zero_macd(self):
        m = self.agent.calculate_macd([100.0] * 60)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m["macd"], 0.0, places=9)
        self.assertAlmostEqual(m["signal"], 0.0, places=9)
        self.assertAlmostEqual(m["histogram"], 0.0, places=9)

    def test_steady_uptrend_gives_positive_macd(self):
        prices = [100.0 * (1.01 ** i) for i in range(80)]
        m = self.agent.calculate_macd(prices)
        self.assertGreater(m["macd"], 0)


class TestSignalIndependence(unittest.TestCase):
    """Correlated votes must not be counted as agreement.

    Measured before the fix: P(MACD buy) was 0.48 unconditionally but 0.93
    given the moving-average buy — a 1.95x lift — because both reduced to
    the same trend comparison.
    """

    def setUp(self):
        self.agent = MLTradingAgent("TEST")

    def _co_fire_lift(self, trials=600):
        ma_buy = macd_buy = both = usable = 0
        for seed in range(trials):
            prices = walk(n=260, seed=seed, vol=0.015)
            s20 = self.agent.calculate_sma(prices, 20)
            s50 = self.agent.calculate_sma(prices, 50)
            m = self.agent.calculate_macd(prices)
            if not (s20 and s50 and m):
                continue
            usable += 1
            a = s20 > s50 * 1.02
            b = m["macd"] > m["signal"]
            ma_buy += a
            macd_buy += b
            both += (a and b)
        if not ma_buy or not usable:
            return None
        return (both / ma_buy) / (macd_buy / usable)

    def test_macd_is_no_longer_a_proxy_for_the_moving_average_signal(self):
        lift = self._co_fire_lift()
        self.assertIsNotNone(lift)
        self.assertLess(
            lift, 1.35,
            f"MACD still co-fires with the MA signal at {lift:.2f}x lift; "
            f"they are measuring the same thing",
        )

    def test_analysis_declares_correlation_groups(self):
        result = get_ml_recommendation("TEST", walk(n=260, drift=0.004))
        self.assertIn("signals", result)
        self.assertIn(
            "groups", result["signals"],
            "aggregation must expose which signals were treated as "
            "correlated, or the confidence cannot be checked",
        )

    def test_correlated_trend_signals_count_once(self):
        """A strong single-direction trend fires the MA, cross and MACD
        checks together. They must contribute one trend opinion, not
        three."""
        result = get_ml_recommendation(
            "TEST", [100.0 * (1.01 ** i) for i in range(260)])
        groups = result["signals"]["groups"]
        self.assertLessEqual(
            groups.get("trend", {}).get("counted", 99), 1,
            "correlated trend signals were counted more than once",
        )

    def test_confidence_is_not_inflated_by_duplicate_evidence(self):
        """A one-directional trend must not report near-certainty from
        what is effectively a single observation."""
        result = get_ml_recommendation(
            "TEST", [100.0 * (1.01 ** i) for i in range(260)])
        self.assertLessEqual(result["confidence"], 0.85)

    def test_confidence_states_what_it_measures(self):
        result = get_ml_recommendation("TEST", walk(n=260))
        self.assertIn("confidence_meaning", result)
        self.assertIn("not a probability",
                      result["confidence_meaning"].lower())


class TestRegressionOfExistingBehaviour(unittest.TestCase):
    """The response shape the production handler and frontend rely on."""

    def test_recommendation_shape_is_unchanged(self):
        result = get_ml_recommendation("AAPL", walk(n=260, drift=0.003))
        for key in ("symbol", "recommendation", "confidence", "ml_score",
                    "signals", "indicators", "reasoning", "risk_level"):
            self.assertIn(key, result)
        self.assertIn(result["recommendation"], ("BUY", "SELL", "HOLD"))
        for key in ("rsi", "sma_20", "sma_50", "macd", "momentum_10d",
                    "volatility_pct"):
            self.assertIn(key, result["indicators"])
        for key in ("buy", "sell", "hold", "total"):
            self.assertIn(key, result["signals"])

    def test_short_history_still_returns_the_error_shape(self):
        result = get_ml_recommendation("AAPL", [100.0] * 10)
        self.assertIn("error", result)
        self.assertEqual(result["recommendation"], "hold")
        self.assertEqual(result["confidence"], 0.0)

    def test_confidence_and_score_are_bounded(self):
        for seed in range(20):
            result = get_ml_recommendation("X", walk(n=260, seed=seed))
            self.assertGreaterEqual(result["confidence"], 0.0)
            self.assertLessEqual(result["confidence"], 1.0)
            self.assertGreaterEqual(result["ml_score"], 0.0)
            self.assertLessEqual(result["ml_score"], 100.0)

    def test_deterministic(self):
        prices = walk(n=260, seed=5)
        a = get_ml_recommendation("X", prices)
        b = get_ml_recommendation("X", prices)
        self.assertEqual(a["recommendation"], b["recommendation"])
        self.assertEqual(a["confidence"], b["confidence"])


class TestOtherIndicators(unittest.TestCase):
    def setUp(self):
        self.agent = MLTradingAgent("TEST")

    def test_sma_known_value(self):
        self.assertAlmostEqual(
            self.agent.calculate_sma([1, 2, 3, 4, 5], 5), 3.0, places=9)

    def test_ema_matches_reference(self):
        prices = walk(n=80, seed=3)
        self.assertAlmostEqual(self.agent.calculate_ema(prices, 12),
                               ref_ema_series(prices, 12)[-1], places=9)

    def test_rsi_all_gains_is_100(self):
        self.assertAlmostEqual(
            self.agent.calculate_rsi([100 + i for i in range(30)], 14),
            100.0, places=6)

    def test_rsi_is_bounded(self):
        for seed in range(15):
            rsi = self.agent.calculate_rsi(walk(n=60, seed=seed), 14)
            if rsi is not None:
                self.assertGreaterEqual(rsi, 0.0)
                self.assertLessEqual(rsi, 100.0)

    def test_bollinger_bands_ordered(self):
        bb = self.agent.calculate_bollinger_bands(walk(n=60), 20)
        self.assertLess(bb["lower"], bb["middle"])
        self.assertLess(bb["middle"], bb["upper"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestConfidenceScaling(unittest.TestCase):
    """Confidence must fall when the independent groups disagree.

    Without this, a 2-1 split reports the same confidence as unanimity,
    which is the inflation the grouping was introduced to remove.
    """

    def setUp(self):
        self.agent = MLTradingAgent("TEST")

    def test_a_dissenting_group_lowers_confidence(self):
        """Isolates the agreement scaling.

        Both cases have the SAME winning groups with the same
        confidences, so the only difference is that the second has a
        group arguing the other way. Comparing cases whose winning means
        also differ would not test the scaling at all.
        """
        agreeing = [
            ("ma_crossover", "trend", "buy", 0.75),
            ("macd", "momentum", "buy", 0.75),
        ]
        with_dissent = agreeing + [
            ("bollinger", "mean_reversion", "sell", 0.80),
        ]
        _r1, c1, _g1, _t1 = self.agent._aggregate_signals(agreeing)
        _r2, c2, _g2, _t2 = self.agent._aggregate_signals(with_dissent)

        self.assertAlmostEqual(c1, 0.75, places=6)
        self.assertLess(
            c2, c1,
            "a dissenting group must lower confidence; a 2-1 split cannot "
            "report the same confidence as unanimity",
        )

    def test_evenly_split_groups_return_hold(self):
        """Genuine indecision, not a coin toss dressed up as a call."""
        even = [
            ("ma_crossover", "trend", "buy", 0.75),
            ("bollinger", "mean_reversion", "sell", 0.80),
        ]
        rec, conf, _g, _t = self.agent._aggregate_signals(even)
        self.assertEqual(rec, "hold")
        self.assertEqual(conf, 0.5)

    def test_disagreement_inside_a_group_weakens_it(self):
        agree = [("rsi", "momentum", "buy", 0.85),
                 ("macd", "momentum", "buy", 0.70)]
        conflict = [("rsi", "momentum", "buy", 0.85),
                    ("macd", "momentum", "sell", 0.70)]
        _r1, c1, g1, _t1 = self.agent._aggregate_signals(agree)
        _r2, _c2, g2, _t2 = self.agent._aggregate_signals(conflict)
        self.assertEqual(g1["momentum"]["internal_agreement"], 1.0)
        self.assertEqual(g2["momentum"]["internal_agreement"], 0.5)
        self.assertLess(g2["momentum"]["confidence"],
                        g1["momentum"]["confidence"])

    def test_three_correlated_signals_yield_one_opinion(self):
        """The defect in its original form: three trend-ish signals
        reported as three agreeing votes."""
        signals = [
            ("rsi", "momentum", "buy", 0.85),
            ("macd", "momentum", "buy", 0.70),
            ("momentum_10d", "momentum", "buy", 0.65),
        ]
        _rec, _conf, groups, tally = self.agent._aggregate_signals(signals)
        self.assertEqual(groups["momentum"]["signals_fired"], 3)
        self.assertEqual(groups["momentum"]["counted"], 1)
        self.assertEqual(tally["total"], 1,
                         "three correlated signals are one opinion")
        self.assertEqual(tally["signals_fired"], 3)

    def test_no_signals_returns_hold_not_a_guess(self):
        rec, conf, _g, tally = self.agent._aggregate_signals([])
        self.assertEqual(rec, "hold")
        self.assertEqual(conf, 0.5)
        self.assertEqual(tally["total"], 0)
