"""
Tests for the canonical quantitative signal engine.

Two kinds of assertion appear here.

*Anchored* tests pin behaviour that must never change: the MACD
crossover semantics, the RSI thresholds, what counts as a vote.

*Property* tests pin relationships rather than magic numbers -
monotonicity, bounds, volatility invariance - so the normalisation
scale constants can be revised without rewriting the suite. A test that
asserts "strength == 0.4831" would fail on any retune and teach nothing.
"""
import math
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.signals import indicators as ind          # noqa: E402
from agent.signals import normalization as norm      # noqa: E402
from agent.signals import engine                     # noqa: E402
from agent.signals.models import (                   # noqa: E402
    CorrelationGroup, DataFreshness, GroupDirection, SignalDirection,
    SignalResult, StrengthBand,
)


def flat(n, value=100.0):
    return [value] * n


def ramp(n, start=100.0, step=1.0):
    """A perfectly LINEAR series.

    Deliberately kept for the cases that want a degenerate input. Note
    it is useless as a "rising market" fixture: EMA lag is constant for
    a linear series, so the MACD line is constant, its own EMA converges
    to it, and the histogram is exactly 0.0. Use `trending()` for a
    market that is actually going somewhere.
    """
    return [start + i * step for i in range(n)]


def falling(n, start=300.0, step=1.0):
    return [start - i * step for i in range(n)]


def phases(segments, start=100.0, seed=11, noise=0.003):
    """A continuous multi-phase series: each phase resumes from the last
    price, so there is no artificial gap where the regime changes.

    Needed for the crossover cases. Concatenating two independent series
    inserts a price discontinuity that the EMAs then spend 20+ bars
    digesting, which swamps the crossover under test.
    """
    import random
    rng = random.Random(seed)
    prices = [start]
    for count, daily, seg_noise in segments:
        for _ in range(count):
            prices.append(max(0.01, prices[-1]
                              * (1 + daily + rng.gauss(0, seg_noise or noise))))
    return prices


def trending(n, start=100.0, daily=0.004, noise=0.006, seed=11):
    """A compounding series with reproducible noise.

    Geometric rather than arithmetic so percentage returns are stable
    across the series, and noisy so the moving averages actually
    separate and re-converge the way they do on real data.
    """
    import random
    rng = random.Random(seed)
    prices = [start]
    for _ in range(n - 1):
        prices.append(max(0.01, prices[-1] * (1 + daily + rng.gauss(0, noise))))
    return prices


# =========================================================================
# MACD
# =========================================================================

class TestMacd(unittest.TestCase):

    def test_insufficient_history_returns_none_not_a_substitute(self):
        self.assertIsNone(ind.macd(ramp(33)))
        self.assertIsNotNone(ind.macd(ramp(34)))

    def test_bullish_crossover_on_a_rising_series(self):
        m = ind.macd(trending(120, daily=0.006, noise=0.004, seed=3))
        self.assertGreater(m["macd"], m["signal"])
        self.assertGreater(m["histogram"], 0)

    def test_bearish_crossover_on_a_falling_series(self):
        # A steady geometric decline is NOT the right fixture: each day's
        # dollar loss shrinks, so the MACD line rises toward zero and sits
        # ABOVE its own average, giving a positive histogram. A bearish
        # crossover is a decline arriving, not a decline persisting.
        m = ind.macd(phases([(100, 0.000, 0.003), (14, -0.018, 0.003)],
                            start=400.0, seed=1))
        self.assertLess(m["macd"], m["signal"])
        self.assertLess(m["histogram"], 0)

    def test_histogram_is_exactly_macd_minus_signal(self):
        m = ind.macd(trending(120))
        self.assertAlmostEqual(m["histogram"], m["macd"] - m["signal"], places=12)

    def test_positive_macd_line_can_still_be_a_bearish_crossover(self):
        """
        The case the old implementation could not represent.

        With `signal = macd * 0.9`, `macd > signal` was algebraically
        identical to `macd > 0`, so a positive MACD line was ALWAYS
        bullish. A long rally that rolls over leaves the MACD line
        positive while it falls below its own 9-period average - which
        is precisely when the crossover matters.
        """
        # A long rally, then a short sharp rollover: long enough to
        # drag MACD below its 9-period average, short enough that the
        # MACD line itself is still positive.
        prices = phases([(100, 0.008, 0.003), (6, -0.020, 0.003)], seed=1)
        m = ind.macd(prices)
        self.assertGreater(m["macd"], 0, "setup failed: MACD line is not positive")
        self.assertLess(m["histogram"], 0,
                        "positive MACD line should be able to show a bearish "
                        "crossover")
        sig = engine.macd_signal(m, prices[-1], 0.02, DataFreshness.FRESH)
        self.assertIs(sig.direction, SignalDirection.SELL)

    def test_negative_macd_line_can_still_be_a_bullish_crossover(self):
        prices = phases([(100, -0.008, 0.003), (6, 0.020, 0.003)],
                        start=400.0, seed=1)
        m = ind.macd(prices)
        self.assertLess(m["macd"], 0, "setup failed: MACD line is not negative")
        self.assertGreater(m["histogram"], 0)
        sig = engine.macd_signal(m, prices[-1], 0.02, DataFreshness.FRESH)
        self.assertIs(sig.direction, SignalDirection.BUY)

    def test_missing_macd_is_no_signal_not_neutral(self):
        sig = engine.macd_signal(None, 100.0, 0.02, DataFreshness.FRESH)
        self.assertIs(sig.direction, SignalDirection.NO_SIGNAL)
        self.assertNotEqual(sig.direction, SignalDirection.NEUTRAL)

    def test_signal_line_is_not_a_scalar_multiple_of_the_macd_line(self):
        """
        Direct guard on the defect itself. If signal were k*macd for a
        fixed k, the ratio would be constant across series.
        """
        ratios = []
        for seed in (1, 2, 3, 4, 5, 6):
            m = ind.macd(trending(160, daily=0.003, noise=0.012, seed=seed))
            ratios.append(m["signal"] / m["macd"])
        spread = max(ratios) - min(ratios)
        self.assertGreater(spread, 1e-6,
                           f"signal/macd is near-constant ({ratios}), which is "
                           f"the fixed-multiplier defect")


# =========================================================================
# RSI
# =========================================================================

class TestRsi(unittest.TestCase):

    def test_insufficient_history(self):
        self.assertIsNone(ind.rsi(flat(14)))

    def test_monotonic_rise_is_overbought(self):
        self.assertEqual(ind.rsi(ramp(40)), 100.0)

    def test_monotonic_fall_is_oversold(self):
        self.assertEqual(ind.rsi(falling(40)), 0.0)

    def _sig(self, value):
        return engine.rsi_signal(value, DataFreshness.FRESH)

    def test_oversold_votes_buy(self):
        self.assertIs(self._sig(22.0).direction, SignalDirection.BUY)

    def test_overbought_votes_sell(self):
        self.assertIs(self._sig(78.0).direction, SignalDirection.SELL)

    def test_fifty_is_neutral(self):
        self.assertIs(self._sig(50.0).direction, SignalDirection.NEUTRAL)

    def test_sixty_to_sixtysix_casts_no_directional_vote(self):
        """
        'Strong relative strength' is not a buy. Treating it as one
        double-counts trend, which RSI is measured to track.
        """
        for value in (60.0, 62.5, 66.0, 69.9):
            with self.subTest(rsi=value):
                sig = self._sig(value)
                self.assertIs(sig.direction, SignalDirection.NEUTRAL)
                self.assertEqual(sig.strength, 0.0)

    def test_exact_thresholds_do_not_vote(self):
        """30 and 70 are the boundaries; the rule is strictly beyond."""
        self.assertIs(self._sig(30.0).direction, SignalDirection.NEUTRAL)
        self.assertIs(self._sig(70.0).direction, SignalDirection.NEUTRAL)

    def test_further_past_the_threshold_is_stronger(self):
        """RSI 72 must be weaker than RSI 86, which a constant cannot do."""
        weak = self._sig(72.0).strength
        mid = self._sig(80.0).strength
        strong = self._sig(95.0).strength
        self.assertLess(weak, mid)
        self.assertLess(mid, strong)

    def test_strength_is_bounded(self):
        for value in (0.0, 5.0, 29.9, 50.0, 70.1, 99.0, 100.0):
            with self.subTest(rsi=value):
                s = self._sig(value).strength
                self.assertGreaterEqual(s, 0.0)
                self.assertLessEqual(s, 1.0)


# =========================================================================
# Moving averages
# =========================================================================

class TestMovingAverages(unittest.TestCase):

    def _sig(self, sma20, sma50, price=100.0, vol=0.02):
        return engine.ma_crossover_signal(sma20, sma50, price, vol,
                                          DataFreshness.FRESH)

    def test_clear_bullish_separation_votes_buy(self):
        self.assertIs(self._sig(110.0, 100.0).direction, SignalDirection.BUY)

    def test_clear_bearish_separation_votes_sell(self):
        self.assertIs(self._sig(90.0, 100.0).direction, SignalDirection.SELL)

    def test_equal_averages_are_neutral_not_directional(self):
        self.assertIs(self._sig(100.0, 100.0).direction,
                      SignalDirection.NEUTRAL)

    def test_separation_inside_the_band_is_neutral(self):
        """
        Two averages within 2% are effectively the same line. Voting on
        that produces a signal that flips on noise.
        """
        self.assertIs(self._sig(101.0, 100.0).direction,
                      SignalDirection.NEUTRAL)
        self.assertIs(self._sig(99.0, 100.0).direction,
                      SignalDirection.NEUTRAL)

    def test_missing_history_is_no_signal(self):
        self.assertIs(self._sig(None, 100.0).direction,
                      SignalDirection.NO_SIGNAL)
        self.assertIs(self._sig(100.0, None).direction,
                      SignalDirection.NO_SIGNAL)

    def test_wider_separation_is_stronger(self):
        near = self._sig(103.0, 100.0).strength
        far = self._sig(130.0, 100.0).strength
        self.assertLess(near, far)

    def test_golden_cross_needs_two_hundred_closes(self):
        sig = engine.golden_cross_signal(None, None, 100.0, 0.02,
                                         DataFreshness.FRESH)
        self.assertIs(sig.direction, SignalDirection.NO_SIGNAL)

    def test_golden_cross_direction(self):
        up = engine.golden_cross_signal(110.0, 100.0, 100.0, 0.02,
                                        DataFreshness.FRESH)
        down = engine.golden_cross_signal(90.0, 100.0, 100.0, 0.02,
                                          DataFreshness.FRESH)
        self.assertIs(up.direction, SignalDirection.BUY)
        self.assertIs(down.direction, SignalDirection.SELL)


# =========================================================================
# Momentum
# =========================================================================

class TestMomentum(unittest.TestCase):

    def _sig(self, pct, vol=0.02):
        return engine.momentum_signal(pct, vol, DataFreshness.FRESH)

    def test_strong_positive_votes_buy(self):
        self.assertIs(self._sig(8.0).direction, SignalDirection.BUY)

    def test_strong_negative_votes_sell(self):
        self.assertIs(self._sig(-8.0).direction, SignalDirection.SELL)

    def test_inside_the_threshold_is_neutral(self):
        for value in (0.0, 2.0, -2.0, 4.9, -4.9):
            with self.subTest(pct=value):
                self.assertIs(self._sig(value).direction,
                              SignalDirection.NEUTRAL)

    def test_missing_history_is_no_signal_not_zero(self):
        """
        'No change' and 'cannot tell' are different claims. Returning
        0.0 for a short series would make the second look like the first.
        """
        self.assertIsNone(ind.momentum_pct(flat(5), 10))
        self.assertIs(self._sig(None).direction, SignalDirection.NO_SIGNAL)

    def test_sign_convention_positive_means_price_rose(self):
        prices = [100.0] * 10 + [110.0]
        self.assertAlmostEqual(ind.momentum_pct(prices, 10), 10.0, places=6)

    def test_bigger_excess_is_stronger(self):
        self.assertLess(self._sig(6.0).strength, self._sig(25.0).strength)

    def test_same_move_is_weaker_on_a_more_volatile_security(self):
        """A 10% ten-day move is unremarkable on a name that swings 6% a
        day and notable on one that swings 0.5%."""
        calm = self._sig(10.0, vol=0.005).strength
        wild = self._sig(10.0, vol=0.06).strength
        self.assertGreater(calm, wild)


# =========================================================================
# Bollinger
# =========================================================================

class TestBollinger(unittest.TestCase):

    BANDS = {"upper": 110.0, "middle": 100.0, "lower": 90.0}

    _UNSET = object()

    def _sig(self, price, bands=_UNSET):
        # Sentinel, not `bands or self.BANDS`: the falsy default
        # swallowed an explicit None and the "missing bands" test was
        # silently running with bands present.
        if bands is self._UNSET:
            bands = self.BANDS
        return engine.bollinger_signal(price, bands, DataFreshness.FRESH)

    def test_inside_the_bands_is_neutral_not_directional(self):
        for price in (95.0, 100.0, 105.0):
            with self.subTest(price=price):
                self.assertIs(self._sig(price).direction,
                              SignalDirection.NEUTRAL)

    def test_close_above_the_upper_band_votes_sell(self):
        self.assertIs(self._sig(112.0).direction, SignalDirection.SELL)

    def test_close_below_the_lower_band_votes_buy(self):
        self.assertIs(self._sig(88.0).direction, SignalDirection.BUY)

    def test_near_but_not_beyond_a_band_is_not_a_signal(self):
        """
        Being near a band is not a signal. Making it one would fire the
        mean-reversion group almost continuously, and it would stop
        being independent of trend.
        """
        near_upper = self._sig(109.5)
        near_lower = self._sig(90.5)
        self.assertIs(near_upper.direction, SignalDirection.NEUTRAL)
        self.assertIs(near_lower.direction, SignalDirection.NEUTRAL)
        self.assertEqual(near_upper.strength, 0.0)
        self.assertEqual(near_lower.strength, 0.0)

    def test_exactly_at_a_band_does_not_vote(self):
        self.assertIs(self._sig(110.0).direction, SignalDirection.NEUTRAL)
        self.assertIs(self._sig(90.0).direction, SignalDirection.NEUTRAL)

    def test_missing_bands_is_no_signal(self):
        self.assertIs(self._sig(100.0, bands=None).direction,
                      SignalDirection.NO_SIGNAL)

    def test_degenerate_bands_are_no_signal(self):
        """Zero dispersion means the band has no width to breach."""
        flat_bands = {"upper": 100.0, "middle": 100.0, "lower": 100.0}
        self.assertIs(self._sig(100.0, flat_bands).direction,
                      SignalDirection.NO_SIGNAL)

    def test_further_beyond_the_band_is_stronger(self):
        self.assertLess(self._sig(111.0).strength, self._sig(125.0).strength)


# =========================================================================
# Grouping
# =========================================================================

def sig(indicator, direction, strength=0.5):
    return SignalResult(
        indicator=indicator, group=engine.INDICATOR_GROUPS[indicator],
        direction=direction, strength=strength,
    )


class TestGrouping(unittest.TestCase):

    def _groups(self, signals):
        return {str(g.group): g for g in engine.group_signals(signals)}

    def test_agreeing_members_produce_one_aligned_group(self):
        g = self._groups([sig("ma_crossover", SignalDirection.BUY, 0.6),
                          sig("golden_cross", SignalDirection.BUY, 0.8)])
        trend = g["trend"]
        self.assertIs(trend.direction, GroupDirection.BUY)
        self.assertFalse(trend.internal_disagreement)
        self.assertEqual(trend.internal_agreement, 1.0)
        self.assertEqual(trend.counted, 1, "a group is ONE opinion")

    def test_internal_disagreement_halves_the_group_weight(self):
        g = self._groups([sig("ma_crossover", SignalDirection.BUY, 0.8),
                          sig("golden_cross", SignalDirection.SELL, 0.3)])
        trend = g["trend"]
        self.assertTrue(trend.internal_disagreement)
        self.assertEqual(trend.internal_agreement, 0.5)
        self.assertAlmostEqual(trend.weight, trend.magnitude * 0.5, places=9)

    def test_opposing_members_net_rather_than_both_counting(self):
        g = self._groups([sig("ma_crossover", SignalDirection.BUY, 0.8),
                          sig("golden_cross", SignalDirection.SELL, 0.3)])
        trend = g["trend"]
        self.assertAlmostEqual(trend.net, 0.5, places=9)
        self.assertIs(trend.direction, GroupDirection.BUY)

    def test_equal_and_opposite_members_deadlock_and_cast_no_vote(self):
        g = self._groups([sig("ma_crossover", SignalDirection.BUY, 0.5),
                          sig("golden_cross", SignalDirection.SELL, 0.5)])
        trend = g["trend"]
        self.assertIs(trend.direction, GroupDirection.MIXED)
        self.assertEqual(trend.counted, 0)

    def test_three_agreeing_momentum_signals_are_still_one_opinion(self):
        """The defect groups exist to prevent: one fact, seen three times."""
        g = self._groups([sig("rsi", SignalDirection.BUY, 0.9),
                          sig("macd", SignalDirection.BUY, 0.9),
                          sig("momentum_10d", SignalDirection.BUY, 0.9)])
        momentum = g["momentum"]
        self.assertEqual(momentum.opinionated_members, 3)
        self.assertEqual(momentum.counted, 1)

    def test_neutral_members_do_not_make_a_group_directional(self):
        g = self._groups([sig("rsi", SignalDirection.NEUTRAL),
                          sig("macd", SignalDirection.NEUTRAL)])
        self.assertIs(g["momentum"].direction, GroupDirection.NEUTRAL)
        self.assertEqual(g["momentum"].counted, 0)

    def test_all_members_absent_is_no_signal_not_neutral(self):
        g = self._groups([sig("rsi", SignalDirection.NO_SIGNAL),
                          sig("macd", SignalDirection.NO_SIGNAL),
                          sig("momentum_10d", SignalDirection.NO_SIGNAL)])
        self.assertIs(g["momentum"].direction, GroupDirection.NO_SIGNAL)

    def test_every_group_appears_even_when_silent(self):
        groups = engine.group_signals([sig("rsi", SignalDirection.BUY)])
        self.assertEqual(len(groups), 3)
        self.assertEqual({str(g.group) for g in groups},
                         {"trend", "momentum", "mean_reversion"})

    def test_group_magnitude_is_the_strongest_supporting_member(self):
        g = self._groups([sig("rsi", SignalDirection.BUY, 0.3),
                          sig("macd", SignalDirection.BUY, 0.9)])
        self.assertAlmostEqual(g["momentum"].magnitude, 0.9, places=9)


# =========================================================================
# Aggregation: direction / agreement / magnitude kept apart
# =========================================================================

class TestAggregation(unittest.TestCase):

    def _agg(self, signals):
        return engine.aggregate(engine.group_signals(signals))

    def test_two_independent_groups_agreeing_gives_full_agreement(self):
        agg = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.6),
                         sig("macd", SignalDirection.BUY, 0.6)])
        self.assertIs(agg["direction"], SignalDirection.BUY)
        self.assertAlmostEqual(agg["signal_agreement"], 1.0, places=9)

    def test_cross_group_disagreement_yields_no_direction(self):
        agg = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.9),
                         sig("macd", SignalDirection.SELL, 0.9)])
        self.assertIs(agg["direction"], SignalDirection.NEUTRAL)
        self.assertEqual(agg["signal_agreement"], 0.0)
        self.assertIs(agg["strength_band"], StrengthBand.MIXED)

    def test_two_to_one_split_lowers_agreement_below_unanimity(self):
        split = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.6),
                           sig("macd", SignalDirection.BUY, 0.6),
                           sig("bollinger", SignalDirection.SELL, 0.6)])
        unanimous = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.6),
                               sig("macd", SignalDirection.BUY, 0.6),
                               sig("bollinger", SignalDirection.BUY, 0.6)])
        self.assertIs(split["direction"], SignalDirection.BUY)
        self.assertLess(split["signal_agreement"],
                        unanimous["signal_agreement"])

    def test_agreement_is_structural_and_ignores_magnitude(self):
        """
        The separation that matters. Raising every member's strength must
        not move agreement at all - if it does, the two concepts have
        been recombined.
        """
        weak = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.26),
                          sig("macd", SignalDirection.BUY, 0.26)])
        strong = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.99),
                            sig("macd", SignalDirection.BUY, 0.99)])
        self.assertAlmostEqual(weak["signal_agreement"],
                               strong["signal_agreement"], places=9)
        self.assertLess(weak["signal_magnitude"], strong["signal_magnitude"])

    def test_magnitude_is_about_size_and_ignores_how_many_agreed(self):
        one = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.8)])
        two = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.8),
                         sig("macd", SignalDirection.BUY, 0.8)])
        self.assertAlmostEqual(one["signal_magnitude"],
                               two["signal_magnitude"], places=9)
        self.assertLess(one["signal_agreement"] + 1e-9,
                        two["signal_agreement"] + 1.0)

    def test_internal_disagreement_in_the_winning_group_lowers_agreement(self):
        clean = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.8),
                           sig("macd", SignalDirection.BUY, 0.8)])
        messy = self._agg([sig("ma_crossover", SignalDirection.BUY, 0.8),
                           sig("golden_cross", SignalDirection.SELL, 0.1),
                           sig("macd", SignalDirection.BUY, 0.8)])
        self.assertIs(messy["direction"], SignalDirection.BUY)
        self.assertLess(messy["signal_agreement"], clean["signal_agreement"])

    def test_nothing_directional_gives_no_direction(self):
        agg = self._agg([sig("rsi", SignalDirection.NEUTRAL),
                         sig("bollinger", SignalDirection.NEUTRAL)])
        self.assertFalse(agg["direction"].is_directional)
        self.assertEqual(agg["signal_agreement"], 0.0)
        self.assertEqual(agg["signal_magnitude"], 0.0)

    def test_agreement_and_magnitude_are_bounded(self):
        for strength in (0.0, 0.25, 0.5, 1.0):
            agg = self._agg([sig("ma_crossover", SignalDirection.BUY, strength),
                             sig("macd", SignalDirection.SELL, strength / 2)])
            self.assertTrue(0.0 <= agg["signal_agreement"] <= 1.0)
            self.assertTrue(0.0 <= agg["signal_magnitude"] <= 1.0)


# =========================================================================
# Regime adjustment
# =========================================================================

class TestRegimeAdjustment(unittest.TestCase):

    def test_aligned_regime_does_not_discount(self):
        self.assertEqual(
            engine.regime_adjustment(SignalDirection.BUY, "BULLISH"), 1.0)
        self.assertEqual(
            engine.regime_adjustment(SignalDirection.SELL, "BEARISH"), 1.0)

    def test_hostile_regime_discounts(self):
        self.assertLess(
            engine.regime_adjustment(SignalDirection.BUY, "STRONG_BEARISH"),
            engine.regime_adjustment(SignalDirection.BUY, "NEUTRAL"))

    def test_unknown_regime_is_not_treated_as_calm(self):
        self.assertLess(
            engine.regime_adjustment(SignalDirection.BUY, "UNKNOWN"),
            engine.regime_adjustment(SignalDirection.BUY, "NEUTRAL"))

    def test_regime_never_manufactures_a_direction(self):
        """
        The rule that keeps the regime from becoming a signal source.
        Whatever the market is doing, a security with no directional
        evidence must stay without one.
        """
        neutral_signals = [sig("rsi", SignalDirection.NEUTRAL),
                           sig("bollinger", SignalDirection.NEUTRAL)]
        for regime in ("STRONG_BULLISH", "BULLISH", "NEUTRAL", "MIXED",
                       "VOLATILE", "BEARISH", "STRONG_BEARISH", "UNKNOWN"):
            with self.subTest(regime=regime):
                prices = flat(120, 100.0)
                r = engine.evaluate("X", prices, DataFreshness.FRESH, regime)
                self.assertFalse(r.direction.is_directional)
                self.assertEqual(r.regime_adjustment, 1.0)

    def test_regime_never_reverses_a_direction(self):
        prices = trending(220, daily=0.002, noise=0.010)
        directions = set()
        for regime in ("STRONG_BULLISH", "STRONG_BEARISH", "VOLATILE",
                       "UNKNOWN"):
            r = engine.evaluate("X", prices, DataFreshness.FRESH, regime)
            directions.add(str(r.direction))
        self.assertEqual(len(directions), 1,
                         f"direction changed with regime: {directions}")

    def test_raw_and_adjusted_magnitude_are_both_retained(self):
        prices = trending(220, daily=0.002, noise=0.010)
        hostile = engine.evaluate("X", prices, DataFreshness.FRESH,
                                  "STRONG_BEARISH")
        self.assertGreater(hostile.signal_magnitude, 0)
        self.assertLess(hostile.regime_adjusted_magnitude,
                        hostile.signal_magnitude)
        self.assertAlmostEqual(
            hostile.regime_adjusted_magnitude,
            hostile.signal_magnitude * hostile.regime_adjustment, places=9)

    def test_regime_does_not_touch_agreement(self):
        """Agreement is structural. A hostile market does not change how
        many groups agreed."""
        prices = trending(220, daily=0.002, noise=0.010)
        a = engine.evaluate("X", prices, DataFreshness.FRESH, "BULLISH")
        b = engine.evaluate("X", prices, DataFreshness.FRESH, "STRONG_BEARISH")
        self.assertAlmostEqual(a.signal_agreement, b.signal_agreement,
                               places=12)


# =========================================================================
# End to end
# =========================================================================

class TestEvaluate(unittest.TestCase):

    def test_short_history_states_the_absence_rather_than_guessing(self):
        r = engine.evaluate("NEW", ramp(20), DataFreshness.FRESH)
        self.assertFalse(r.analysis_available)
        self.assertIs(r.direction, SignalDirection.NO_SIGNAL)
        self.assertTrue(any("at least" in w for w in r.warnings))

    def test_short_history_does_not_raise(self):
        for n in (0, 1, 10, 49):
            with self.subTest(n=n):
                engine.evaluate("X", ramp(n) if n else [], DataFreshness.FRESH)

    def test_sustained_uptrend_is_a_buy(self):
        r = engine.evaluate("UP", trending(260, daily=0.002, noise=0.010),
                            DataFreshness.FRESH, "BULLISH")
        self.assertIs(r.direction, SignalDirection.BUY)
        self.assertGreater(r.buy_groups, 0)

    def test_sustained_downtrend_is_a_sell(self):
        r = engine.evaluate("DOWN",
                            trending(260, start=500.0, daily=-0.003,
                                     noise=0.012, seed=4),
                            DataFreshness.FRESH, "BEARISH")
        self.assertIs(r.direction, SignalDirection.SELL)
        self.assertGreater(r.sell_groups, 0)

    def test_partial_indicator_availability_is_reported_not_hidden(self):
        """150 closes: SMA200 and therefore the golden cross cannot be
        computed. That must surface as NO_SIGNAL and a warning."""
        r = engine.evaluate("X", trending(150), DataFreshness.FRESH)
        golden = r.indicator("golden_cross")
        self.assertIs(golden.direction, SignalDirection.NO_SIGNAL)
        self.assertTrue(any("not computed" in w for w in r.warnings))

    def test_stale_data_raises_a_warning(self):
        r = engine.evaluate("X", trending(220), DataFreshness.STALE)
        self.assertTrue(any("stale" in w.lower() for w in r.warnings))

    def test_unknown_freshness_raises_a_warning(self):
        r = engine.evaluate("X", trending(220), DataFreshness.UNKNOWN)
        self.assertTrue(any("freshness" in w.lower() for w in r.warnings))

    def test_all_indicators_are_evaluated_and_grouped(self):
        r = engine.evaluate("X", trending(260), DataFreshness.FRESH)
        self.assertEqual(len(r.indicator_results), 6)
        self.assertEqual(len(r.group_results), 3)

    def test_result_is_json_serialisable(self):
        import json
        r = engine.evaluate("X", trending(260), DataFreshness.FRESH, "MIXED")
        json.dumps(r.as_dict())

    def test_result_carries_no_trade_semantics(self):
        """
        Milestone 5 produces evidence, not instructions. A field that
        could be read as an order is a step toward execution by accident.
        """
        import json
        r = engine.evaluate("X", trending(260), DataFreshness.FRESH)
        blob = json.dumps(r.as_dict()).lower()
        for banned in ("entry_price", "take_profit", "stop_loss",
                       "position_size", "expected_profit", "order_type",
                       "allocation", "quantity", "shares_to_buy"):
            self.assertNotIn(banned, blob)

    def test_execution_is_reported_unavailable(self):
        r = engine.evaluate("X", trending(260), DataFreshness.FRESH)
        self.assertFalse(r.execution_available)
        self.assertFalse(r.as_dict()["execution_available"])

    def test_reasons_are_deterministic(self):
        prices = trending(260)
        a = engine.evaluate("X", prices, DataFreshness.FRESH, "MIXED")
        b = engine.evaluate("X", prices, DataFreshness.FRESH, "MIXED")
        self.assertEqual(a.reasons, b.reasons)

    def test_a_frozen_price_produces_no_directional_vote_anywhere(self):
        """
        A halted or untraded security must not look like a signal.

        Two inherited defects both pointed the wrong way here. RSI
        short-circuited on `avg_loss == 0` and returned 100 - maximally
        overbought - for a series that never moved. MACD compared
        `line > signal` when both were exactly 0.0, so the absence of a
        crossover read as a bearish one. Together, a frozen price cast a
        SELL vote from two apparently independent indicators.
        """
        r = engine.evaluate("HALTED", flat(120, 100.0), DataFreshness.FRESH,
                            "BULLISH")
        self.assertFalse(r.direction.is_directional)
        for s in r.indicator_results:
            with self.subTest(indicator=s.indicator):
                self.assertFalse(
                    s.direction.is_directional,
                    f"{s.indicator} voted {s.direction} on a flat series")

    def test_rsi_of_a_flat_series_is_fifty_not_one_hundred(self):
        self.assertEqual(ind.rsi(flat(40)), 50.0)

    def test_rsi_of_a_series_that_only_rose_is_still_one_hundred(self):
        """The guard must not swallow a legitimate maximum."""
        self.assertEqual(ind.rsi(ramp(40)), 100.0)

    def test_a_strong_uptrend_can_be_neutral_because_it_is_overbought(self):
        """
        Not a defect: trend and momentum genuinely conflict when a
        relentless rally leaves RSI overbought. The engine reports the
        conflict rather than picking a side, and the group breakdown
        shows why.
        """
        r = engine.evaluate("HOT", trending(260, daily=0.004, noise=0.006),
                            DataFreshness.FRESH, "BULLISH")
        self.assertIs(r.direction, SignalDirection.NEUTRAL)
        self.assertEqual(r.buy_groups, 1)
        self.assertEqual(r.sell_groups, 1)
        self.assertIs(r.group("trend").direction, GroupDirection.BUY)
        self.assertIs(r.group("momentum").direction, GroupDirection.SELL)

    def test_agreement_is_never_described_as_a_probability(self):
        r = engine.evaluate("X", trending(260), DataFreshness.FRESH)
        blob = (" ".join(r.reasons) + " "
                + r.as_dict()["agreement_meaning"]).lower()
        self.assertTrue(
            "not a probability" in blob or "not the probability" in blob,
            f"agreement is not disclaimed as a non-probability: {blob[:200]}")


# =========================================================================
# Normalisation properties
# =========================================================================

class TestNormalisation(unittest.TestCase):

    def test_saturate_is_bounded_and_monotonic(self):
        prev = -1.0
        for ratio in (0.0, 0.1, 0.5, 1.0, 5.0, 100.0, 1e6):
            value = norm.saturate(ratio, 1.0)
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 1.0)
            self.assertGreater(value, prev)
            prev = value

    def test_a_signal_at_its_threshold_is_weak_not_absent(self):
        self.assertGreater(norm.scaled(0.0, 1.0), 0.0)
        self.assertEqual(norm.scaled(0.0, 1.0), norm.STRENGTH_FLOOR)

    def test_zero_volatility_does_not_produce_an_infinite_magnitude(self):
        s = norm.macd_strength(1.0, 100.0, 0.0)
        self.assertLessEqual(s, 1.0)
        self.assertGreaterEqual(s, 0.0)

    def test_missing_volatility_does_not_inflate_magnitude(self):
        self.assertEqual(norm.effective_volatility(None), norm.MIN_VOLATILITY)

    def test_identical_relative_moves_score_alike_across_price_levels(self):
        """
        The reason raw currency is never compared: a $1 histogram on a
        $10 stock and a $100 histogram on a $1,000 stock are the same
        signal.
        """
        cheap = norm.macd_strength(1.0, 10.0, 0.02)
        dear = norm.macd_strength(100.0, 1000.0, 0.02)
        self.assertAlmostEqual(cheap, dear, places=12)

    def test_strength_never_leaves_the_unit_interval(self):
        cases = [
            norm.macd_strength(1e9, 1.0, 1e-9),
            norm.macd_strength(-1e9, 1.0, 1e-9),
            norm.rsi_strength(1e6),
            norm.bollinger_strength(1e9, 110.0, 90.0),
            norm.momentum_strength(1e6, 5.0, 1e-9),
            norm.ma_spread_strength(1e9, 1.0, 1.0, 1e-9),
        ]
        for value in cases:
            self.assertTrue(0.0 <= value <= 1.0, value)

    def test_non_directional_signal_cannot_carry_a_magnitude(self):
        """A neutral reading with a strength would sort as weakly bullish
        in any consumer that ranked on strength."""
        s = SignalResult(indicator="rsi", group=CorrelationGroup.MOMENTUM,
                         direction=SignalDirection.NEUTRAL, strength=0.9)
        self.assertEqual(s.strength, 0.0)


if __name__ == "__main__":
    unittest.main()
