"""
Strategy evaluation and calibration.

Both modules exist to argue against their own output, so most of these
tests check that they refuse to conclude things.

The two specific traps:

1. Seeing that high-strength trades beat low-strength trades over thirty
   trades, concluding the score works, and raising size on high-strength
   signals. The difference between two 15-trade buckets is almost
   entirely sampling variation.

2. Trying twenty parameter settings and reporting the best. That
   produces an apparent improvement even when every setting is
   identical in truth, because the maximum of twenty noisy measurements
   is positive by construction.
"""
import os
import random
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.evaluation import (                                    # noqa: E402
    DEFAULT_HOLDOUT, MIN_BUCKET_SIZE, MIN_TRADES_PER_ARM,
    MonotonicityVerdict, Recommendation, assess, bucket_by_strength,
    expected_best_of_n, intervals_overlap, run, split_holdout,
    strength_vs_outcome_correlation,
)
from agent.journal import TradeRecord                             # noqa: E402

RISK = 2.0


def trade(r, strength=0.5, i=0):
    entry = 100.0
    return TradeRecord(
        trade_id=f"t{i}", symbol="XYZ", quantity=1.0, entry_price=entry,
        exit_price=entry + r * RISK, opened_at="2026-09-30T14:00:00+00:00",
        closed_at=f"2026-09-30T15:{i % 60:02d}:00+00:00",
        planned_stop=entry - RISK, hypothesis_strength=strength,
        session_date="2026-09-30")


def band(r, strength, count, start=0):
    return [trade(r, strength, start + i) for i in range(count)]


class TestCalibrationRefusesSmallSamples(unittest.TestCase):

    def test_three_trades_per_band_cannot_test_the_score(self):
        trades = (band(-0.5, 0.30, 3) + band(0.3, 0.50, 3, 10)
                  + band(1.5, 0.80, 3, 20))
        result = assess(trades)
        self.assertEqual(result["verdict"],
                         str(MonotonicityVerdict.INSUFFICIENT_DATA))
        self.assertFalse(result["is_evidence"])

    def test_a_perfectly_ordered_small_sample_is_still_insufficient(self):
        """
        The seductive case: the ordering is exactly as hoped and means
        nothing. If this returned a positive verdict the module would be
        worse than useless.
        """
        trades = (band(-1.0, 0.30, 5) + band(0.0, 0.50, 5, 10)
                  + band(2.0, 0.80, 5, 20))
        result = assess(trades)
        self.assertEqual(result["verdict"],
                         str(MonotonicityVerdict.INSUFFICIENT_DATA))

    def test_the_detail_says_it_is_untested_not_that_it_fails(self):
        """
        "Not tested" and "does not work" are different, and the second
        would justify removing the score.
        """
        result = assess(band(1.0, 0.8, 4))
        self.assertIn("not been tested", result["detail"])

    def test_no_parameter_change_is_justified_by_an_untested_score(self):
        result = assess(band(1.0, 0.8, 4))
        self.assertIn("No parameter change", result["action"])

    def test_one_comparable_band_is_not_a_comparison(self):
        trades = band(1.0, 0.80, MIN_BUCKET_SIZE + 5) + band(0.0, 0.30, 2)
        result = assess(trades)
        self.assertEqual(result["bands_comparable"], 1)
        self.assertEqual(result["verdict"],
                         str(MonotonicityVerdict.INSUFFICIENT_DATA))

    def test_a_large_ordered_sample_can_reach_a_verdict(self):
        """
        The falsifying control: the gate must be passable, or it is a
        refusal to measure rather than a standard.
        """
        trades = (band(-0.5, 0.30, 40) + band(0.3, 0.50, 40, 100)
                  + band(1.5, 0.80, 40, 200))
        result = assess(trades)
        self.assertEqual(result["verdict"],
                         str(MonotonicityVerdict.MONOTONIC_AND_SIGNIFICANT))
        self.assertTrue(result["is_evidence"])

    def test_a_large_unordered_sample_is_reported_as_not_monotonic(self):
        trades = (band(1.5, 0.30, 40) + band(0.3, 0.50, 40, 100)
                  + band(-0.5, 0.80, 40, 200))
        result = assess(trades)
        self.assertEqual(result["verdict"],
                         str(MonotonicityVerdict.NOT_MONOTONIC))
        self.assertFalse(result["is_evidence"])

    def test_an_ordering_within_noise_is_not_significant(self):
        """
        Monotonic but overlapping intervals is the common real case and
        must not read as a finding.

        Built deterministically. An earlier version used random data and
        accepted EITHER this verdict or NOT_MONOTONIC - so when the
        sample came out unordered, a bug that ignored interval overlap
        was invisible. Accepting two outcomes made the test unable to
        distinguish them.
        """
        # Band means 0.40 then 0.45 - ordered - with a spread far wider
        # than the gap, so the intervals must overlap.
        low = [trade(0.40 + (3.0 if i % 2 else -3.0), 0.30, i)
               for i in range(40)]
        high = [trade(0.45 + (3.0 if i % 2 else -3.0), 0.80, i + 100)
                for i in range(40)]
        result = assess(low + high, buckets=((0.0, 0.45), (0.45, 1.01)))

        # Assert the SETUP first, so the test cannot pass by accident
        # on data that is not actually monotonic.
        self.assertTrue(result["monotonic"],
                        "the fixture is not ordered, so this cannot test "
                        "what it claims to")
        self.assertEqual(
            result["verdict"],
            str(MonotonicityVerdict.MONOTONIC_BUT_NOT_SIGNIFICANT))
        self.assertFalse(result["is_evidence"])


class TestBucketing(unittest.TestCase):

    def test_trades_land_in_the_right_band(self):
        buckets = bucket_by_strength(
            band(1.0, 0.30, 2) + band(1.0, 0.80, 3, 10))
        self.assertEqual(buckets[0].count, 2)
        self.assertEqual(buckets[-1].count, 3)

    def test_a_trade_without_a_strength_is_excluded_not_binned_at_zero(self):
        """
        An unknown strength is missing data. Placing it at zero would
        fabricate a reading the system never made - and would then make
        the lowest band look worse than it was.
        """
        unknown = trade(1.0, 0.5, 99)
        unknown.hypothesis_strength = None
        result = assess(band(1.0, 0.3, 3) + [unknown])
        self.assertEqual(result["trades_without_strength"], 1)
        self.assertEqual(sum(b["count"] for b in result["buckets"]), 3)

    def test_band_boundaries_do_not_double_count(self):
        exact = trade(1.0, 0.45, 1)
        buckets = bucket_by_strength([exact])
        self.assertEqual(sum(b.count for b in buckets), 1)

    def test_a_band_below_the_threshold_is_not_comparable(self):
        buckets = bucket_by_strength(band(1.0, 0.80, MIN_BUCKET_SIZE - 1))
        self.assertFalse(buckets[-1].comparable)

    def test_a_band_at_the_threshold_is_comparable(self):
        buckets = bucket_by_strength(band(1.0, 0.80, MIN_BUCKET_SIZE))
        self.assertTrue(buckets[-1].comparable)

    def test_overlapping_intervals_are_detected(self):
        a, b = bucket_by_strength(
            band(1.0, 0.30, 20) + band(1.0, 0.80, 20, 50))[0:3:2]
        self.assertIsNotNone(intervals_overlap(a, b))

    def test_a_missing_interval_gives_none_not_false(self):
        """
        False would mean "they do not overlap", which is a claim. None
        means we cannot tell.
        """
        [single] = bucket_by_strength(band(1.0, 0.80, 1))[-1:]
        other = bucket_by_strength(band(1.0, 0.30, 20))[0]
        self.assertIsNone(intervals_overlap(single, other))


class TestCorrelation(unittest.TestCase):

    def test_too_few_pairs_gives_no_correlation(self):
        result = strength_vs_outcome_correlation(band(1.0, 0.5, 2))
        self.assertIsNone(result["r"])

    def test_a_correlation_is_reported_with_an_interval(self):
        rng = random.Random(3)
        trades = [trade(s * 3 + rng.gauss(0, 0.5), s, i)
                  for i, s in enumerate([0.2 + 0.006 * j
                                         for j in range(120)])]
        result = strength_vs_outcome_correlation(trades)
        self.assertIsNotNone(result["r"])
        self.assertIsNotNone(result["ci_low"])
        self.assertIsNotNone(result["ci_high"])

    def test_a_small_sample_correlation_is_not_evidence(self):
        rng = random.Random(3)
        trades = [trade(s * 3 + rng.gauss(0, 0.5), s, i)
                  for i, s in enumerate([0.3, 0.4, 0.5, 0.6, 0.7, 0.8])]
        result = strength_vs_outcome_correlation(trades)
        self.assertFalse(result["is_evidence"])

    def test_no_variation_gives_none_not_zero(self):
        """
        Zero would claim there is no relationship. None says the
        question is undefined on this data.
        """
        result = strength_vs_outcome_correlation(band(1.0, 0.5, 20))
        self.assertIsNone(result["r"])
        self.assertIn("not zero", result["note"])

    def test_an_interval_spanning_zero_is_flagged(self):
        rng = random.Random(11)
        trades = [trade(rng.gauss(0, 2.0), 0.2 + 0.01 * i, i)
                  for i in range(60)]
        result = strength_vs_outcome_correlation(trades)
        if result["ci_low"] is not None:
            self.assertEqual(result["spans_zero"],
                             result["ci_low"] <= 0 <= result["ci_high"])


class TestSelectionNoise(unittest.TestCase):
    """
    The arithmetic that makes most sweep results meaningless.
    """

    def test_one_arm_has_no_selection_effect(self):
        self.assertEqual(expected_best_of_n(1, 0.5), 0.0)

    def test_more_arms_means_a_larger_expected_gap(self):
        floors = [expected_best_of_n(n, 0.2) for n in (2, 5, 20, 50)]
        self.assertEqual(floors, sorted(floors))

    def test_zero_spread_has_no_selection_effect(self):
        self.assertEqual(expected_best_of_n(20, 0.0), 0.0)

    def test_the_floor_scales_with_the_spread(self):
        self.assertAlmostEqual(expected_best_of_n(20, 0.4),
                               expected_best_of_n(20, 0.2) * 2, places=6)


class TestSweepRefusesOverfitting(unittest.TestCase):

    def _identical_arms(self, n=20, spread=0.2, seed_base=0):
        """Arms that differ ONLY by sampling noise."""
        def evaluate(params, data):
            rng = random.Random(seed_base + params["x"])
            return {"trades": 50, "expectancy_r": rng.gauss(0, spread),
                    "label": f"x={params['x']}"}
        return [{"x": i} for i in range(n)], evaluate

    def test_identical_arms_produce_no_recommendation(self):
        """
        The headline test. Twenty arms with no real difference produce a
        winner that looks better than the rest, and the module must
        attribute that to the selection.
        """
        arms, evaluate = self._identical_arms()
        result = run(arms, evaluate, list(range(500)))
        self.assertEqual(result["recommendation"],
                         str(Recommendation.NO_CHANGE_WITHIN_NOISE))
        self.assertFalse(result["permits_change"])

    def test_the_noise_floor_is_reported_alongside_the_gap(self):
        arms, evaluate = self._identical_arms()
        result = run(arms, evaluate, list(range(500)))
        self.assertIsNotNone(result["selection_noise_floor_r"])
        self.assertIsNotNone(result["winner_gap_r"])

    def test_small_arms_cannot_be_compared_at_all(self):
        """
        Uses a LITERAL trade count, not MIN_TRADES_PER_ARM - 1.

        Deriving the fixture from the constant makes the test move with
        it, so lowering the threshold to 1 left this passing - a test
        that can never catch a change to the very thing it guards.
        """
        self.assertGreaterEqual(MIN_TRADES_PER_ARM, 10,
                                "the literal below assumes a meaningful "
                                "threshold")

        def tiny(params, data):
            return {"trades": 5, "expectancy_r": 1.0}
        result = run([{"x": 1}, {"x": 2}], tiny, list(range(100)))
        self.assertEqual(result["recommendation"],
                         str(Recommendation.NO_CHANGE_INSUFFICIENT_DATA))

    def test_the_arm_threshold_is_high_enough_to_matter(self):
        """
        Guards the constant directly. A sweep is the most
        overfitting-prone thing in the project, so its bar must be
        higher than a single metric's.
        """
        self.assertGreaterEqual(MIN_TRADES_PER_ARM, 30)

    def test_a_clear_winner_without_a_holdout_is_refused(self):
        """
        A result confirmed only on the data that selected it is not a
        result.
        """
        def separated(params, data):
            return {"trades": 100,
                    "expectancy_r": 5.0 if params["x"] == 0 else 0.0}
        result = run([{"x": 0}, {"x": 1}], separated, list(range(500)))
        self.assertEqual(result["recommendation"],
                         str(Recommendation.NO_CHANGE_FAILED_HOLDOUT))
        self.assertFalse(result["permits_change"])

    def test_a_winner_that_fails_out_of_sample_is_refused(self):
        """
        The signature of a parameter fitted to the selection period
        rather than a real effect.
        """
        selection = list(range(500))
        holdout = list(range(500, 700))

        def overfit(params, data):
            in_sample = data is selection
            return {"trades": 100,
                    "expectancy_r": (5.0 if params["x"] == 0 and in_sample
                                     else -1.0)}
        result = run([{"x": 0}, {"x": 1}], overfit, selection, holdout)
        self.assertEqual(result["recommendation"],
                         str(Recommendation.NO_CHANGE_FAILED_HOLDOUT))

    def test_a_winner_that_survives_out_of_sample_is_a_candidate(self):
        """
        The falsifying control: something must be able to pass, or this
        is a refusal to evaluate rather than a standard.
        """
        selection = list(range(500))
        holdout = list(range(500, 700))

        def genuine(params, data):
            return {"trades": 100,
                    "expectancy_r": 5.0 if params["x"] == 0 else 0.0}
        result = run([{"x": 0}, {"x": 1}], genuine, selection, holdout)
        self.assertEqual(result["recommendation"],
                         str(Recommendation.CANDIDATE_FOR_CHANGE))
        self.assertTrue(result["permits_change"])

    def test_even_a_candidate_is_not_an_approved_change(self):
        """
        A sweep cannot authorise a change. The strongest value says
        CANDIDATE, and the detail says so in words.
        """
        selection, holdout = list(range(500)), list(range(500, 700))

        def genuine(params, data):
            return {"trades": 100,
                    "expectancy_r": 5.0 if params["x"] == 0 else 0.0}
        result = run([{"x": 0}, {"x": 1}], genuine, selection, holdout)
        self.assertIn("not an approved change", result["detail"])

    def test_every_arm_is_reported_not_only_the_winner(self):
        arms, evaluate = self._identical_arms(n=7)
        result = run(arms, evaluate, list(range(500)))
        self.assertEqual(len(result["arms"]), 7)

    def test_permits_change_is_derived_from_the_recommendation(self):
        for value in Recommendation:
            with self.subTest(value=value):
                self.assertEqual(
                    value.permits_change,
                    value is Recommendation.CANDIDATE_FOR_CHANGE)

    def test_a_sweep_needs_at_least_one_arm(self):
        with self.assertRaises(ValueError):
            run([], lambda p, d: {}, [1, 2, 3])


class TestHoldoutSplit(unittest.TestCase):

    def test_the_split_is_chronological_not_random(self):
        """
        Market conditions cluster in time, so a randomly split holdout
        is contaminated by the same regime the selection was fitted to.
        """
        items = list(range(100))
        selection, holdout = split_holdout(items, 0.3)
        self.assertEqual(selection, list(range(70)))
        self.assertEqual(holdout, list(range(70, 100)))

    def test_the_split_sizes_follow_the_fraction(self):
        selection, holdout = split_holdout(list(range(100)), 0.2)
        self.assertEqual(len(selection), 80)
        self.assertEqual(len(holdout), 20)

    def test_an_invalid_fraction_is_refused(self):
        for fraction in (0.0, 1.0, -0.1, 1.5):
            with self.subTest(fraction=fraction):
                with self.assertRaises(ValueError):
                    split_holdout(list(range(10)), fraction)

    def test_the_default_holds_back_a_meaningful_share(self):
        self.assertGreaterEqual(DEFAULT_HOLDOUT, 0.2)


class TestDeterminism(unittest.TestCase):

    def test_the_same_trades_give_the_same_assessment(self):
        trades = band(-0.5, 0.30, 40) + band(1.5, 0.80, 40, 100)
        self.assertEqual(assess(trades), assess(trades))

    def test_both_modules_record_a_configuration_version(self):
        self.assertTrue(assess(band(1.0, 0.5, 3))["config_version"])

        def evaluate(params, data):
            return {"trades": 50, "expectancy_r": 0.1}
        self.assertTrue(
            run([{"x": 1}], evaluate, [1, 2])["config_version"])


if __name__ == "__main__":
    unittest.main()
