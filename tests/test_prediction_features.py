"""Feature records, and the leakage they must not commit.

THE ADVERSARIAL FIXTURE

Most of these tests run on a series whose future is EXTREME: flat and
boring up to the decision bar, then a violent move afterwards. On a
smooth series, code that peeks one bar ahead produces almost the same
answer as code that does not, so every test passes while the system
leaks. The discontinuity is what makes peeking observable.

Leakage is VOID, not a warning. A contaminated feature looks identical
to a good one, so it has to be refused rather than annotated.
"""
from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent.prediction import features as F                   # noqa: E402


@dataclass
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 1_000_000.0


def flat_then_explode(n_flat: int = 60, price: float = 100.0):
    """Flat at `price`, then one catastrophic bar, then flat high.

    The decision index for these tests is the LAST flat bar. Anything
    that reads past it sees a 50% move and a volume spike, which no
    feature computed correctly can possibly reflect.
    """
    bars = [Bar(f"2026-01-01T{9 + i // 60:02d}:{i % 60:02d}:00",
                price, price, price, price, 1_000_000.0)
            for i in range(n_flat)]
    bars.append(Bar("2026-01-01T11:00:00", price, price * 1.5,
                    price * 0.5, price * 1.5, 99_000_000.0))
    bars += [Bar(f"2026-01-01T11:{i:02d}:00", price * 1.5, price * 1.5,
                 price * 1.5, price * 1.5, 1_000_000.0)
             for i in range(1, 10)]
    return bars


def rising(n: int = 60, start: float = 100.0, step: float = 0.5):
    return [Bar(f"2026-01-01T{9 + i // 60:02d}:{i % 60:02d}:00",
                start + i * step, start + i * step + 0.2,
                start + i * step - 0.2, start + i * step, 1_000_000.0)
            for i in range(n)]


class TestTheAsOfBoundary(unittest.TestCase):
    """The single property every other test depends on."""

    def test_a_record_at_the_last_flat_bar_sees_no_volatility(self):
        bars = flat_then_explode()
        decision = 59                     # last flat bar
        rec = F.build(bars, decision, "TEST")
        self.assertIsNotNone(rec)
        # A 50% move sits at index 60. If volatility is non-zero, the
        # feature read past the boundary.
        self.assertEqual(rec.values["volatility_20b_pct"], 0.0)
        self.assertEqual(rec.values["return_1b_pct"], 0.0)
        self.assertEqual(rec.values["return_20b_pct"], 0.0)

    def test_the_price_is_the_decision_bars_close_not_the_next(self):
        bars = flat_then_explode()
        rec = F.build(bars, 59, "TEST")
        self.assertEqual(rec.values["price"], 100.0)
        self.assertNotEqual(rec.values["price"], bars[60].close)

    def test_volume_features_do_not_see_the_future_spike(self):
        bars = flat_then_explode()
        rec = F.build(bars, 59, "TEST")
        # The spike bar has 99x volume. Relative volume must be 1.0.
        self.assertEqual(rec.values["relative_volume_20b"], 1.0)
        self.assertEqual(rec.values["volume"], 1_000_000.0)

    def test_bollinger_does_not_widen_from_a_future_bar(self):
        bars = flat_then_explode()
        rec = F.build(bars, 59, "TEST")
        # A flat window has no band at all, so the position is UNKNOWN
        # rather than a fabricated midpoint.
        self.assertIsNone(rec.values["bollinger_position"])

    def test_building_at_the_spike_DOES_see_it(self):
        """The falsifying control for every test above.

        If the builder ignored its index, or the fixture's spike were
        too small to register, the tests above would pass without
        proving anything. Building AT the spike must produce visibly
        different values.
        """
        bars = flat_then_explode()
        before = F.build(bars, 59, "TEST")
        at = F.build(bars, 60, "TEST")
        self.assertEqual(before.values["return_1b_pct"], 0.0)
        self.assertAlmostEqual(at.values["return_1b_pct"], 50.0, places=4)
        self.assertGreater(at.values["volatility_20b_pct"], 1.0)
        self.assertGreater(at.values["relative_volume_20b"], 10.0)

    def test_the_record_cannot_be_built_past_the_end(self):
        bars = flat_then_explode()
        self.assertIsNone(F.build(bars, len(bars), "TEST"))
        self.assertIsNone(F.build(bars, len(bars) + 5, "TEST"))
        self.assertIsNone(F.build(bars, -1, "TEST"))

    def test_time_of_day_comes_from_the_bar_not_the_clock(self):
        # Otherwise a replay would produce different features on every
        # run, and a historical record would drift as it aged.
        bars = flat_then_explode()
        rec1 = F.build(bars, 59, "TEST")
        rec2 = F.build(bars, 59, "TEST")
        self.assertEqual(rec1.values["minutes_since_midnight"],
                         rec2.values["minutes_since_midnight"])
        self.assertEqual(rec1.values["minutes_since_midnight"],
                         9 * 60 + 59)


class TestUnknownIsNotZero(unittest.TestCase):

    def test_early_bars_report_indicators_as_unavailable(self):
        bars = rising(5)
        rec = F.build(bars, 4, "TEST")
        self.assertIsNone(rec.values["rsi_14"])
        self.assertIsNone(rec.values["macd"])
        self.assertIsNone(rec.values["sma_50"])
        self.assertFalse(rec.complete)
        # And it says WHICH, with the shortfall, so a reader does not
        # have to guess why.
        joined = " ".join(rec.unavailable)
        self.assertIn("rsi_14", joined)
        self.assertIn("needs", joined)

    def test_an_unreadable_bar_in_the_window_voids_the_window(self):
        # Not "the ones that parsed": a hole makes every indicator over
        # that window wrong by an unknown amount.
        bars = rising(60)
        bars[30] = Bar("2026-01-01T09:30:00", 100, 100, 100, None)  # type: ignore
        rec = F.build(bars, 59, "TEST")
        self.assertIsNotNone(rec)
        self.assertIsNone(rec.values["rsi_14"])
        self.assertIsNone(rec.values["volatility_20b_pct"])

    def test_a_record_cannot_exist_without_a_timestamp(self):
        # Without one it cannot be ordered, matured or matched to a
        # target. A feature with no "as of" is not a feature.
        bars = rising(30)
        bars[29] = Bar(None, 100, 100, 100, 100)              # type: ignore
        self.assertIsNone(F.build(bars, 29, "TEST"))

    def test_a_record_cannot_exist_without_a_close(self):
        bars = rising(30)
        bars[29] = Bar("2026-01-01T09:29:00", 100, 100, 100, None)  # type: ignore
        self.assertIsNone(F.build(bars, 29, "TEST"))

    def test_a_genuine_zero_survives_as_a_value(self):
        # The unknown/zero distinction only means something if real
        # zeros are kept.
        bars = flat_then_explode()
        rec = F.build(bars, 59, "TEST")
        self.assertEqual(rec.values["return_1b_pct"], 0.0)
        self.assertNotIn("return_1b_pct", " ".join(rec.unavailable))

    def test_rsi_of_a_pure_uptrend_is_100_not_none(self):
        # All gains and no losses is a real extreme, not a division
        # error to be swallowed.
        bars = rising(40)
        rec = F.build(bars, 39, "TEST")
        self.assertEqual(rec.values["rsi_14"], 100.0)

    def test_zero_volume_is_kept_but_negative_is_not(self):
        bars = rising(30)
        bars[29] = Bar("2026-01-01T09:29:00", 100, 100, 100, 100, 0.0)
        rec = F.build(bars, 29, "TEST")
        self.assertEqual(rec.values["volume"], 0.0)
        bars[29] = Bar("2026-01-01T09:29:00", 100, 100, 100, 100, -5.0)
        rec = F.build(bars, 29, "TEST")
        self.assertIsNone(rec.values["volume"])


class TestImmutabilityAndIdentity(unittest.TestCase):

    def test_a_record_is_frozen(self):
        rec = F.build(rising(40), 39, "TEST")
        with self.assertRaises(Exception):
            rec.values = {}                                   # type: ignore
        with self.assertRaises(Exception):
            rec.feature_time = "tampered"                     # type: ignore

    def test_identical_inputs_produce_an_identical_hash(self):
        bars = rising(40)
        a = F.build(bars, 39, "TEST")
        b = F.build(bars, 39, "TEST")
        self.assertEqual(a.feature_hash, b.feature_hash)
        self.assertEqual(a.prediction_feature_id, b.prediction_feature_id)

    def test_a_different_bar_produces_a_different_hash(self):
        bars = rising(40)
        self.assertNotEqual(F.build(bars, 39, "TEST").feature_hash,
                            F.build(bars, 38, "TEST").feature_hash)

    def test_the_hash_is_keyed_by_symbol(self):
        # Two symbols with coincidentally identical readings must not
        # collide into one identity.
        bars = rising(40)
        self.assertNotEqual(F.build(bars, 39, "AAA").feature_hash,
                            F.build(bars, 39, "BBB").feature_hash)

    def test_the_hash_is_keyed_by_schema_version(self):
        values = {"a": 1.0}
        self.assertNotEqual(
            F.compute_feature_hash("X", "t", "features-v1.0.0", values),
            F.compute_feature_hash("X", "t", "features-v2.0.0", values))

    def test_the_hash_does_not_depend_on_dict_ordering(self):
        # Otherwise identical code would hash differently between runs.
        one = F.compute_feature_hash("X", "t", "v", {"a": 1.0, "b": 2.0})
        two = F.compute_feature_hash("X", "t", "v", {"b": 2.0, "a": 1.0})
        self.assertEqual(one, two)

    def test_a_changed_value_changes_the_hash(self):
        # The falsifying control for the ordering test: if the hash
        # ignored values entirely, that test would pass vacuously.
        self.assertNotEqual(
            F.compute_feature_hash("X", "t", "v", {"a": 1.0}),
            F.compute_feature_hash("X", "t", "v", {"a": 1.000001}))


class TestRegimeProvenance(unittest.TestCase):

    def test_the_default_regime_is_none_and_counts_as_synthetic(self):
        rec = F.build(rising(40), 39, "TEST")
        self.assertEqual(rec.regime_source, F.REGIME_NONE)
        self.assertTrue(rec.regime_is_synthetic)

    def test_only_observed_or_reconstructed_is_not_synthetic(self):
        bars = rising(40)
        for source, synthetic in ((F.REGIME_OBSERVED, False),
                                  (F.REGIME_RECONSTRUCTED, False),
                                  (F.REGIME_SYNTHETIC, True),
                                  (F.REGIME_NONE, True),
                                  ("SOMETHING_NEW", True)):
            rec = F.build(bars, 39, "TEST", regime_source=source)
            self.assertEqual(rec.regime_is_synthetic, synthetic, source)


class TestTheNumericVector(unittest.TestCase):

    def test_a_complete_record_yields_a_vector(self):
        rec = F.build(rising(80), 79, "TEST")
        vec = F.numeric_vector(rec)
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), len(F.NUMERIC_FEATURE_NAMES))

    def test_an_incomplete_record_yields_NONE_not_zeros(self):
        # Imputing zero for a missing RSI teaches a model that early
        # bars are oversold. Dropping the row is honest.
        rec = F.build(rising(5), 4, "TEST")
        self.assertIsNone(F.numeric_vector(rec))

    def test_the_feature_list_is_explicit_rather_than_whatever_exists(self):
        # A model iterating `values` would silently start training on a
        # new feature the day someone adds one, invalidating every
        # earlier comparison without a single failing test.
        rec = F.build(rising(80), 79, "TEST")
        self.assertGreater(len(rec.values), len(F.NUMERIC_FEATURE_NAMES))
        for name in F.NUMERIC_FEATURE_NAMES:
            self.assertIn(name, rec.values, name)

    def test_non_numeric_features_are_excluded_from_the_vector(self):
        for name in ("signal_direction", "minutes_since_midnight",
                     "bars_of_history", "price", "volume"):
            self.assertNotIn(name, F.NUMERIC_FEATURE_NAMES, name)


class TestResearchOnlyRemainsStructural(unittest.TestCase):

    def test_the_feature_module_imports_no_decision_path(self):
        from agent.prediction import FORBIDDEN_IMPORTS
        path = os.path.join(REPO, "agent", "prediction", "features.py")
        with open(path) as fh:
            src = fh.read()
        for forbidden in FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {forbidden}", src, forbidden)
            self.assertNotIn(f"from {forbidden}", src, forbidden)


if __name__ == "__main__":
    unittest.main()
