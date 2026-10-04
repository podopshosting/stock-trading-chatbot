"""Baseline models, maturation and metrics.

The properties that matter are negative ones: a deterministic rule must
carry NO probability, an abstention must not be scored, a pending
horizon must not count as a miss, and there must be no blended score.
"""
from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent.prediction import features as F                   # noqa: E402
from agent.prediction import maturation, metrics              # noqa: E402
from agent.prediction import models as M                      # noqa: E402
from agent.prediction.targets import (DIRECTION, DOWN, FLAT,  # noqa: E402
                                      FORWARD_RETURN, UP)


@dataclass
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 1_000_000.0


def series(closes, start_minute=0):
    return [Bar(f"2026-01-01T{9 + (start_minute + i) // 60:02d}:"
                f"{(start_minute + i) % 60:02d}:00",
                c, c * 1.001, c * 0.999, c)
            for i, c in enumerate(closes)]


def rising(n=80, start=100.0, step=0.5):
    return series([start + i * step for i in range(n)])


def falling(n=80, start=140.0, step=0.5):
    return series([start - i * step for i in range(n)])


def flat(n=80, price=100.0):
    return series([price] * n)


NOW = "2026-01-01T12:00:00+00:00"


class TestDeterministicModelsCarryNoProbability(unittest.TestCase):

    def test_no_baseline_produces_a_probability(self):
        rec = F.build(rising(), 79, "TEST")
        for model in M.DETERMINISTIC_MODELS:
            p = M.predict_baseline(model, rec, horizon_minutes=15,
                                   generated_at=NOW)
            self.assertIsNone(
                p.probability,
                f"{model} invented a probability it never computed")
            self.assertTrue(p.deterministic)

    def test_signal_agreement_is_not_used_as_a_probability(self):
        # Agreement between independent groups is not a calibrated
        # likelihood, and charting it beside a logistic model's output
        # would be a category error.
        rec = F.build(rising(), 79, "TEST", signal_direction="BUY",
                      signal_agreement=0.75)
        p = M.predict_baseline(M.CURRENT_SIGNAL_ENGINE, rec,
                               horizon_minutes=15, generated_at=NOW)
        self.assertEqual(p.prediction, UP)
        self.assertIsNone(p.probability)
        self.assertIn("NOT a probability", p.detail)

    def test_an_unknown_model_raises_rather_than_abstaining(self):
        # Silently recording an abstention would hide a typo as a model
        # with no opinions.
        rec = F.build(rising(), 79, "TEST")
        with self.assertRaises(ValueError):
            M.predict_baseline("NOT_A_MODEL", rec, horizon_minutes=15,
                               generated_at=NOW)


class TestNoSkillIsReproducible(unittest.TestCase):

    def test_the_same_input_gives_the_same_flip(self):
        # An unseeded flip would differ on every recomputation and
        # "better than random" would be unfalsifiable.
        rec = F.build(rising(), 79, "TEST")
        a = M.predict_baseline(M.NO_SKILL, rec, horizon_minutes=15,
                               generated_at=NOW)
        b = M.predict_baseline(M.NO_SKILL, rec, horizon_minutes=15,
                               generated_at="a-different-time")
        self.assertEqual(a.prediction, b.prediction)

    def test_different_horizons_can_differ(self):
        # Otherwise every horizon would share one flip and the control
        # would be correlated across horizons.
        rec = F.build(rising(), 79, "TEST")
        flips = {h: M.predict_baseline(M.NO_SKILL, rec,
                                       horizon_minutes=h,
                                       generated_at=NOW).prediction
                 for h in (5, 15, 30, 60, 120, 240)}
        self.assertGreater(len(set(flips.values())), 1,
                           "every horizon produced the same flip")

    def test_it_never_abstains(self):
        # A control that declines is not a control.
        for bars in (rising(), falling(), flat()):
            rec = F.build(bars, 79, "TEST")
            p = M.predict_baseline(M.NO_SKILL, rec, horizon_minutes=15,
                                   generated_at=NOW)
            self.assertNotEqual(p.prediction, M.ABSTAIN)


class TestBaselineBehaviour(unittest.TestCase):

    def test_momentum_says_up_in_an_uptrend(self):
        rec = F.build(rising(), 79, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=15,
                               generated_at=NOW)
        self.assertEqual(p.prediction, UP)

    def test_momentum_says_down_in_a_downtrend(self):
        rec = F.build(falling(), 79, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=15,
                               generated_at=NOW)
        self.assertEqual(p.prediction, DOWN)

    def test_momentum_abstains_when_its_indicators_disagree(self):
        # Picking one and ignoring the other would manufacture a
        # confident call out of a contradiction.
        rec = F.build(flat(), 79, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=15,
                               generated_at=NOW)
        self.assertEqual(p.prediction, M.ABSTAIN)

    def test_mean_reversion_opposes_momentum_at_an_extreme(self):
        # Kept as its own model rather than a negated momentum: it uses
        # RSI and band position, not moving averages.
        rec = F.build(rising(), 79, "TEST")
        mom = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=15,
                                 generated_at=NOW)
        mr = M.predict_baseline(M.MEAN_REVERSION, rec,
                                horizon_minutes=15, generated_at=NOW)
        self.assertEqual(mom.prediction, UP)
        self.assertEqual(mr.prediction, DOWN)

    def test_mean_reversion_abstains_when_nothing_is_extreme(self):
        bars = series([100 + (1 if i % 2 else -1) * 0.1 for i in range(80)])
        rec = F.build(bars, 79, "TEST")
        p = M.predict_baseline(M.MEAN_REVERSION, rec,
                               horizon_minutes=15, generated_at=NOW)
        self.assertEqual(p.prediction, M.ABSTAIN)

    def test_previous_return_sign_reports_flat_for_an_unchanged_bar(self):
        # FLAT is the honest answer for a rule with no sign to copy;
        # an arbitrary UP would be a fabricated call.
        rec = F.build(flat(), 79, "TEST")
        p = M.predict_baseline(M.PREVIOUS_RETURN_SIGN, rec,
                               horizon_minutes=15, generated_at=NOW)
        self.assertEqual(p.prediction, FLAT)

    def test_the_signal_baseline_abstains_with_no_signal_supplied(self):
        # Observed in the real run: 3312 abstentions, because nothing
        # populated signal_direction. The model must say so rather than
        # guess.
        rec = F.build(rising(), 79, "TEST")
        p = M.predict_baseline(M.CURRENT_SIGNAL_ENGINE, rec,
                               horizon_minutes=15, generated_at=NOW)
        self.assertEqual(p.prediction, M.ABSTAIN)
        self.assertIn("no signal direction", p.detail)

    def test_abstentions_are_returned_not_dropped(self):
        # A list that omits them makes a selective predictor look
        # complete.
        rec = F.build(flat(), 79, "TEST")
        preds = M.predict_all_baselines(rec, generated_at=NOW,
                                        horizons=(15,))
        self.assertEqual(len(preds), len(M.BASELINE_MODELS))
        self.assertTrue(any(p.prediction == M.ABSTAIN for p in preds))


class TestRecordsAreImmutable(unittest.TestCase):

    def test_a_prediction_cannot_be_edited(self):
        rec = F.build(rising(), 79, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=15,
                               generated_at=NOW)
        with self.assertRaises(Exception):
            p.prediction = DOWN                               # type: ignore

    def test_an_outcome_cannot_be_edited(self):
        o = M.PredictionOutcome(prediction_id="x", status=M.PENDING)
        with self.assertRaises(Exception):
            o.status = M.MATURED                              # type: ignore

    def test_the_id_is_deterministic_so_a_claim_is_recorded_once(self):
        # Re-running generation must not produce a second set of
        # apparently independent predictions, which would double every
        # sample count.
        rec = F.build(rising(), 79, "TEST")
        a = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=15,
                               generated_at=NOW)
        b = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=15,
                               generated_at="later")
        self.assertEqual(a.prediction_id, b.prediction_id)

    def test_different_horizons_get_different_ids(self):
        rec = F.build(rising(), 79, "TEST")
        ids = {M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=h,
                                  generated_at=NOW).prediction_id
               for h in (5, 15, 30, 60)}
        self.assertEqual(len(ids), 4)


class TestMaturation(unittest.TestCase):

    def test_a_horizon_that_has_not_elapsed_is_pending_not_wrong(self):
        bars = rising(80)
        rec = F.build(bars, 79, "TEST")          # the LAST bar
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=60,
                               generated_at=NOW)
        o = maturation.mature(p, bars, evaluated_at=NOW)
        self.assertEqual(o.status, M.PENDING)
        self.assertIsNone(o.correct)

    def test_a_matured_prediction_is_scored(self):
        bars = rising(80)
        rec = F.build(bars, 60, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=5,
                               generated_at=NOW)
        o = maturation.mature(p, bars, evaluated_at=NOW)
        self.assertEqual(o.status, M.MATURED)
        self.assertEqual(p.prediction, UP)
        self.assertTrue(o.correct)               # it kept rising
        self.assertIsNotNone(o.realized_return_pct)
        self.assertIsNotNone(o.max_favorable_excursion_pct)

    def test_a_wrong_call_is_scored_wrong(self):
        # The falsifying control for the test above: if `correct` were
        # always True, both would pass.
        bars = rising(80)
        rec = F.build(bars, 60, "TEST")
        p = M.predict_baseline(M.MEAN_REVERSION, rec, horizon_minutes=5,
                               generated_at=NOW)
        o = maturation.mature(p, bars, evaluated_at=NOW)
        self.assertEqual(p.prediction, DOWN)     # it called a reversal
        self.assertFalse(o.correct)              # price kept rising

    def test_an_abstention_is_neither_right_nor_wrong(self):
        bars = flat(80)
        rec = F.build(bars, 60, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=5,
                               generated_at=NOW)
        self.assertEqual(p.prediction, M.ABSTAIN)
        o = maturation.mature(p, bars, evaluated_at=NOW)
        self.assertEqual(o.status, M.MATURED)
        self.assertIsNone(o.correct)

    def test_a_feature_time_absent_from_the_series_is_void(self):
        # Guessing a position would score the claim against the wrong
        # future.
        rec = F.build(rising(80), 60, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=5,
                               generated_at=NOW)
        # A series over different minutes entirely.
        other = series([100.0] * 80, start_minute=300)
        o = maturation.mature(p, other, evaluated_at=NOW)
        self.assertEqual(o.status, M.VOID)
        self.assertIn("does not appear", o.void_reason)

    def test_identical_timestamps_in_a_DIFFERENT_series_are_void(self):
        """The defect this test originally exposed.

        rising() and falling() cover the same minutes, so every
        timestamp matches. Timestamp matching alone located the
        prediction in the wrong series and MATURED it against prices it
        had never seen - a confident wrong answer where VOID was
        correct. The checksum identifies the DATA; the timestamp only
        identifies the POSITION.
        """
        rec = F.build(rising(80), 60, "TEST", dataset_checksum="aaa111")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=5,
                               generated_at=NOW)
        o = maturation.mature(p, falling(80), evaluated_at=NOW,
                              dataset_checksum="bbb222")
        self.assertEqual(o.status, M.VOID)
        self.assertIn("dataset mismatch", o.void_reason)

    def test_a_matching_checksum_still_matures(self):
        # Falsifying control: if the checksum check rejected
        # everything, the test above would pass vacuously.
        bars = rising(80)
        rec = F.build(bars, 60, "TEST", dataset_checksum="aaa111")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=5,
                               generated_at=NOW)
        o = maturation.mature(p, bars, evaluated_at=NOW,
                              dataset_checksum="aaa111")
        self.assertEqual(o.status, M.MATURED)

    def test_a_missing_symbol_is_void_rather_than_skipped(self):
        # Silently dropping it would shrink the denominator and improve
        # every rate.
        rec = F.build(rising(80), 60, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=5,
                               generated_at=NOW)
        outs = maturation.mature_all([p], {}, evaluated_at=NOW)
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0].status, M.VOID)

    def test_it_matches_on_timestamp_not_a_stored_index(self):
        # The same prediction against a differently-sliced dataset must
        # not silently point at another bar.
        bars = rising(80)
        rec = F.build(bars, 60, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=5,
                               generated_at=NOW)
        shifted = bars[10:]                      # same bars, new indices
        o = maturation.mature(p, shifted, evaluated_at=NOW)
        self.assertEqual(o.status, M.MATURED)


class TestMetrics(unittest.TestCase):

    def _run(self, bars, model, horizon=5, indices=range(60, 70)):
        preds, outs = [], []
        for i in indices:
            rec = F.build(bars, i, "TEST")
            if rec is None:
                continue
            p = M.predict_baseline(model, rec, horizon_minutes=horizon,
                                   generated_at=NOW)
            preds.append(p)
            outs.append(maturation.mature(p, bars, evaluated_at=NOW))
        return preds, outs

    def test_there_is_no_single_aggregate_score(self):
        preds, outs = self._run(rising(90), M.MOMENTUM)
        rep = metrics.report(preds, outs)
        for forbidden in ("score", "overall_score", "ai_score",
                          "composite", "rating"):
            self.assertNotIn(forbidden, rep, forbidden)
        self.assertIn("deliberately no single score", rep["overall_note"])

    def test_every_rate_carries_its_denominator(self):
        preds, outs = self._run(rising(90), M.MOMENTUM)
        rep = metrics.report(preds, outs)
        g = rep["by_model"]["MOMENTUM"]
        self.assertIn("sample_size", g)
        self.assertIn("comparable", g)
        self.assertFalse(g["comparable"])        # ten is not thirty
        self.assertIn("not comparable", g["note"])

    def test_abstentions_are_counted_separately(self):
        preds, outs = self._run(flat(90), M.MOMENTUM)
        rep = metrics.report(preds, outs)
        g = rep["by_model"]["MOMENTUM"]
        self.assertGreater(g["abstained"], 0)
        self.assertEqual(g["sample_size"], 0)
        self.assertNotIn("directional_accuracy", g)

    def test_pending_is_counted_and_excluded(self):
        bars = rising(70)
        rec = F.build(bars, 69, "TEST")
        p = M.predict_baseline(M.MOMENTUM, rec, horizon_minutes=60,
                               generated_at=NOW)
        o = maturation.mature(p, bars, evaluated_at=NOW)
        g = metrics.score_group([p], {o.prediction_id: o})
        self.assertEqual(g["pending"], 1)
        self.assertEqual(g["scored"], 0)

    def test_precision_is_none_for_a_class_never_predicted(self):
        # Observed in the real run: MOMENTUM predicted FLAT zero times
        # while FLAT occurred 357 times. Undefined precision, not
        # perfect and not zero.
        preds, outs = self._run(rising(90), M.MOMENTUM)
        rep = metrics.report(preds, outs)
        pc = rep["by_model"]["MOMENTUM"].get("per_class", {})
        if pc:
            self.assertIsNone(pc[DOWN]["precision"])

    def test_deterministic_models_get_no_calibration(self):
        preds, outs = self._run(rising(90), M.MOMENTUM)
        rep = metrics.report(preds, outs)
        g = rep["by_model"]["MOMENTUM"]
        self.assertIsNone(g["brier_score"])
        self.assertIsNone(g["log_loss"])
        self.assertIn("nothing to calibrate", g["calibration_note"])

    def test_a_constant_predictor_has_no_correlation_not_zero(self):
        # None, because a constant predictor has NO correlation with the
        # outcome - which is not a measured correlation of zero.
        self.assertIsNone(metrics._pearson([1.0, 1.0, 1.0],
                                           [0.5, 0.2, 0.9]))

    def test_the_correlation_helper_still_works_on_real_input(self):
        # Falsifying control: if _pearson always returned None, the
        # test above would pass vacuously.
        self.assertAlmostEqual(
            metrics._pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]), 1.0,
            places=6)

    def test_log_loss_is_clipped_so_one_mistake_is_not_infinite(self):
        loss = metrics._log_loss([0.0], [1])
        self.assertIsNotNone(loss)
        self.assertLess(loss, 100.0)

    def test_empty_calibration_bins_are_omitted_not_zeroed(self):
        # A bin nobody predicted into has no observed frequency, and
        # showing 0% would read as a miscalibration that never happened.
        bins = metrics._calibration([0.9, 0.95], [1, 1], bins=5)
        self.assertEqual(len(bins), 1)
        self.assertEqual(bins[0]["sample_size"], 2)

    def test_metrics_state_that_predictions_do_not_affect_execution(self):
        preds, outs = self._run(rising(90), M.MOMENTUM)
        rep = metrics.report(preds, outs)
        self.assertEqual(rep["mode"], "RESEARCH_ONLY")
        self.assertIn("NONE", rep["execution_influence"])


class TestResearchOnlyStaysStructural(unittest.TestCase):

    def test_no_prediction_module_imports_a_decision_path(self):
        from agent.prediction import FORBIDDEN_IMPORTS
        import pathlib
        pkg = pathlib.Path(REPO) / "agent" / "prediction"
        offenders = []
        for path in pkg.rglob("*.py"):
            src = path.read_text()
            for forbidden in FORBIDDEN_IMPORTS:
                if (f"import {forbidden}" in src
                        or f"from {forbidden}" in src):
                    offenders.append(f"{path.name} -> {forbidden}")
        self.assertEqual(offenders, [], str(offenders))


if __name__ == "__main__":
    unittest.main()
