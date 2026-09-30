"""
Statistical regression guards for the signal engine.

Unit tests check individual cases. These check *relationships across a
population*, which is the only way some defects are visible at all:

  * The original MACD bug produced a perfectly reasonable-looking number
    on every single case. It was only detectable as "the histogram sign
    matched the MACD line's sign 300 times out of 300".
  * Two indicators that have quietly become the same measurement still
    each return a sensible value. The tell is their correlation.
  * An indicator that never abstains, or never varies, looks like
    corroboration while contributing nothing.

The thresholds here are deliberately loose. They are tripwires for a
structural break, not a specification of the right answer - a test that
pinned the exact correlation would fail on any legitimate retune and
teach nothing.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from analyze_signal_statistics import INDICATORS, analyse   # noqa: E402

SAMPLES = 400
SEED = 7

# |r| above this means two indicators are voting as one, and counting
# them separately inflates apparent independent agreement.
DUPLICATE_R = 0.80

# Above this, one indicator is effectively deciding every call and the
# rest are decoration.
DOMINANCE_CEILING = 0.95


class SignalStatistics(unittest.TestCase):
    """One expensive population, shared by every assertion."""

    @classmethod
    def setUpClass(cls):
        cls.stats = analyse(SAMPLES, SEED)

    # --- invariants ------------------------------------------------------

    def test_no_impossible_states(self):
        self.assertEqual(self.stats["impossible_states"], [],
                         "engine produced states that cannot be valid")

    def test_the_population_actually_evaluated(self):
        """A sweep where nothing computed would pass every other test."""
        self.assertGreaterEqual(self.stats["evaluated"], SAMPLES * 0.9)

    # --- duplicates ------------------------------------------------------

    def test_no_two_indicators_vote_as_duplicates(self):
        offenders = {
            pair: r for pair, r in
            self.stats["pairwise_vote_correlation"].items()
            if r == r and abs(r) > DUPLICATE_R
        }
        self.assertEqual(offenders, {},
                         f"indicator votes are near-duplicates: {offenders}")

    def test_macd_is_not_a_restatement_of_the_moving_average_crossover(self):
        """
        The specific duplication the signal-line defect created. With
        `signal = macd * 0.9`, `macd > signal` reduced to `EMA12 >
        EMA26`, which is a trend comparison - so MACD was voting as a
        second moving-average crossover while appearing independent.
        """
        r = self.stats["pairwise_vote_correlation"].get("ma_crossover vs macd")
        self.assertIsNotNone(r)
        self.assertLess(abs(r), 0.50,
                        f"MACD is tracking the MA crossover too closely "
                        f"(r={r:+.3f}); the signal line may have regressed")

    # --- dead indicators -------------------------------------------------

    def test_every_indicator_fires_sometimes(self):
        for name in INDICATORS:
            with self.subTest(indicator=name):
                self.assertGreater(self.stats["indicator_fire_rate"][name],
                                   0.01, f"{name} never casts a vote")

    def test_no_indicator_fires_in_only_one_direction(self):
        """A vote that never changes is a constant, not a measurement."""
        for pair, r in self.stats["pairwise_vote_correlation"].items():
            with self.subTest(pair=pair):
                self.assertEqual(r, r,
                                 f"{pair}: correlation is NaN, which means one "
                                 f"indicator's vote never varied")

    def test_strength_actually_varies_per_indicator(self):
        """
        The defect this milestone set out to fix: every strength was a
        hardcoded constant, so RSI 71 and RSI 95 scored identically. A
        spread near zero means the constants are back.
        """
        for name in INDICATORS:
            with self.subTest(indicator=name):
                self.assertGreater(
                    self.stats["indicator_strength_spread"][name], 0.10,
                    f"{name} strength barely varies; it may have reverted to "
                    f"a constant")

    # --- dominance -------------------------------------------------------

    def test_no_single_indicator_decides_every_call(self):
        offenders = {n: v for n, v in self.stats["dominance"].items()
                     if v == v and v > DOMINANCE_CEILING}
        self.assertEqual(offenders, {},
                         f"these indicators effectively decide the outcome "
                         f"alone: {offenders}")

    # --- grouping behaviour ---------------------------------------------

    def test_disagreement_is_common_enough_to_be_worth_modelling(self):
        """If groups never conflicted, the halving rule would be dead
        code and the whole correlation model would be unnecessary."""
        self.assertGreater(self.stats["cross_group_conflict_rate"], 0.05)

    def test_indecision_is_possible_and_not_universal(self):
        """
        Both failure modes matter. An engine that always calls a
        direction is not reading disagreement; one that never does is
        useless.
        """
        rate = self.stats["hold_rate"]
        self.assertGreater(rate, 0.02, "the engine never declines to call")
        self.assertLess(rate, 0.95, "the engine almost never calls anything")

    def test_both_directions_occur(self):
        dist = self.stats["direction_distribution"]
        self.assertGreater(dist.get("BUY", 0), 0)
        self.assertGreater(dist.get("SELL", 0), 0)

    def test_mean_reversion_is_mostly_silent_by_design(self):
        """
        Bollinger only votes on a close outside a band, so its group
        should be neutral most of the time. If it started voting
        constantly, the 'near a band is not a signal' rule would have
        been lost.
        """
        dist = self.stats["group_direction_distribution"]["mean_reversion"]
        total = sum(dist.values())
        neutral_share = dist.get("NEUTRAL", 0) / total
        self.assertGreater(neutral_share, 0.50)

    # --- sign sanity -----------------------------------------------------

    def test_trend_indicators_follow_the_generating_drift(self):
        """
        A sign error would show up as a trend indicator systematically
        voting against the drift that produced the series.

        Only the trend indicators are checked. RSI and Bollinger are
        contrarian by construction - oversold in a downtrend is a BUY -
        so alignment near 50% is correct for them, and asserting
        otherwise would encode a misunderstanding as a test.
        """
        alignment = self.stats["drift_alignment"]["golden_cross"]
        self.assertGreater(alignment, 0.60,
                           f"golden cross votes against the drift "
                           f"{1 - alignment:.0%} of the time; possible sign "
                           f"error")


class TestFalsifyingControls(unittest.TestCase):
    """
    The guards above pass trivially if the harness cannot see a problem.
    These prove each detector fires on a constructed fault.
    """

    def test_duplicate_detector_catches_perfectly_correlated_votes(self):
        from analyze_signal_statistics import pearson
        identical = [1, -1, 1, 1, -1, -1, 1]
        self.assertGreater(abs(pearson(identical, identical)), DUPLICATE_R)

    def test_duplicate_detector_passes_independent_votes(self):
        from analyze_signal_statistics import pearson
        a = [1, -1, 1, -1, 1, -1, 1, -1]
        b = [1, 1, -1, -1, 1, 1, -1, -1]
        self.assertLess(abs(pearson(a, b)), DUPLICATE_R)

    def test_constant_vote_is_reported_as_nan_not_as_independence(self):
        """
        A dead indicator has zero variance. Correlation is undefined,
        and returning 0.0 would make it look ideally independent - the
        most dangerous possible misreading.
        """
        from analyze_signal_statistics import pearson
        constant = [1] * 8
        varying = [1, -1, 1, -1, 1, -1, 1, -1]
        r = pearson(constant, varying)
        self.assertNotEqual(r, r, "a constant vote must not read as r = 0.0")


if __name__ == "__main__":
    unittest.main()
