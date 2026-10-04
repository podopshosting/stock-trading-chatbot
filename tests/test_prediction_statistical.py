"""Splits, scaling, statistical models and walk-forward.

The tests that matter are the leakage ones, and the adversarial fixture
is the same idea as the feature tests: put something EXTREME in the
future and require that it cannot reach backwards.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent.prediction import splits as S                     # noqa: E402
from agent.prediction import statistical as ST               # noqa: E402
from agent.prediction import walkforward as WF               # noqa: E402
from agent.prediction.models import DOWN, FLAT, UP           # noqa: E402

NOW = "2026-10-04T12:00:00+00:00"


def stamps(n, start=1):
    return [f"2026-01-{d:02d}T10:00:00" for d in range(start, start + n)]


class TestChronologicalSplit(unittest.TestCase):

    def test_the_split_is_never_shuffled(self):
        sp = S.chronological_split(stamps(100))
        self.assertFalse(sp.shuffled)

    def test_the_splits_are_in_time_order_and_do_not_overlap(self):
        sp = S.chronological_split(stamps(100))
        self.assertLess(sp.train_end, sp.validation_start)
        self.assertLess(sp.validation_end, sp.holdout_start)

    def test_boundaries_are_stored_not_fractions(self):
        # Fractions reproduce only against an identical row count.
        sp = S.chronological_split(stamps(100))
        d = sp.to_dict()
        self.assertEqual(d["train"]["start"], sp.train_start)
        self.assertEqual(d["holdout"]["end"], sp.holdout_end)

    def test_too_few_rows_returns_none_not_an_empty_holdout(self):
        # A holdout of zero rows reports no error and would read as a
        # perfect score.
        self.assertIsNone(S.chronological_split(stamps(10)))
        self.assertIsNone(S.chronological_split([]))

    def test_assignment_matches_the_boundaries(self):
        sp = S.chronological_split(stamps(100))
        self.assertEqual(sp.assigned(sp.train_start), S.TRAIN)
        self.assertEqual(sp.assigned(sp.validation_start), S.VALIDATION)
        self.assertEqual(sp.assigned(sp.holdout_end), S.HOLDOUT)
        self.assertIsNone(sp.assigned("1999-01-01T00:00:00"))


class TestClassPriorIsMeasuredNotAssumed(unittest.TestCase):

    def test_the_prior_reports_what_each_trivial_baseline_scores(self):
        # The whole point. An assumed 0.50 for a three-class target is
        # simply wrong, and this is how the right bar is known.
        labels = [UP] * 45 + [DOWN] * 39 + [FLAT] * 16
        prior = S.class_prior(labels)
        self.assertEqual(prior["majority"], UP)
        self.assertAlmostEqual(prior["majority_rate"], 0.45, places=4)
        exp = prior["expected_accuracy"]
        self.assertAlmostEqual(exp["ALWAYS_UP"], 0.45, places=4)
        # A two-class flip on a three-class target.
        self.assertAlmostEqual(exp["UNIFORM_TWO_CLASS_FLIP"], 0.42,
                               places=4)
        self.assertLess(exp["UNIFORM_TWO_CLASS_FLIP"], 0.5)

    def test_a_two_class_flip_can_never_reach_one_half_here(self):
        """Why NO_SKILL measured 0.4182 and not 0.50.

        A predictor that only ever says UP or DOWN cannot score on the
        FLAT rows at all, so its ceiling is 1 - P(FLAT) and its
        expectation is (P(UP)+P(DOWN))/2.
        """
        labels = [UP] * 4472 + [DOWN] * 3928 + [FLAT] * 1600
        exp = S.class_prior(labels)["expected_accuracy"]
        self.assertAlmostEqual(exp["UNIFORM_TWO_CLASS_FLIP"], 0.42,
                               places=3)
        self.assertAlmostEqual(exp["ALWAYS_UP"], 0.4472, places=3)

    def test_no_labels_gives_no_prior_rather_than_a_default(self):
        prior = S.class_prior([])
        self.assertIsNone(prior["majority"])
        self.assertEqual(prior["distribution"], {})


class TestScalingIsTrainOnly(unittest.TestCase):

    def test_a_future_extreme_cannot_change_historical_scaling(self):
        """The adversarial leakage control.

        Fit on training rows, then show that adding a wild future row
        to the dataset does not move the fitted statistics. If scaling
        were computed across all splits, every historical feature would
        silently shift.
        """
        names = ("a",)
        train = [[1.0], [2.0], [3.0], [4.0]]
        fitted = S.fit_scaler(train, names)
        before = fitted.transform([2.0])

        # The future arrives, and it is enormous.
        polluted = train + [[1_000_000.0]]
        polluted_fit = S.fit_scaler(polluted, names)

        self.assertEqual(fitted.means, (2.5,))
        self.assertNotEqual(polluted_fit.means, fitted.means)
        # The train-only scaler is unchanged, so the historical row
        # scales identically.
        self.assertEqual(fitted.transform([2.0]), before)

    def test_the_control_would_notice_if_scaling_were_shared(self):
        # Falsifying control: prove the polluted fit really does differ,
        # so the test above is not passing because nothing changed.
        names = ("a",)
        a = S.fit_scaler([[1.0], [2.0]], names)
        b = S.fit_scaler([[1.0], [2.0], [999.0]], names)
        self.assertNotAlmostEqual(a.means[0], b.means[0])
        self.assertNotEqual(a.transform([1.0]), b.transform([1.0]))

    def test_a_constant_feature_scales_to_zero_not_infinity(self):
        sc = S.fit_scaler([[5.0], [5.0], [5.0]], ("a",))
        self.assertEqual(sc.stdevs, (0.0,))
        self.assertEqual(sc.transform([5.0]), [0.0])
        self.assertEqual(sc.transform([99.0]), [0.0])

    def test_a_wrong_width_vector_is_refused_not_reshaped(self):
        # A silently padded row would be scored against the wrong
        # coefficients.
        sc = S.fit_scaler([[1.0, 2.0]], ("a", "b"))
        self.assertIsNone(sc.transform([1.0]))
        self.assertIsNone(sc.transform([1.0, 2.0, 3.0]))

    def test_the_scaler_records_what_it_was_fitted_on(self):
        sc = S.fit_scaler([[1.0], [2.0]], ("a",))
        self.assertEqual(sc.fitted_on, S.TRAIN)
        self.assertEqual(sc.fitted_rows, 2)


class TestLogistic(unittest.TestCase):

    def _data(self, n=300):
        # A deliberately learnable pattern: feature 0 determines the
        # label. Used to prove the fitter WORKS, so that its failure on
        # real data means something.
        vectors, labels = [], []
        for i in range(n):
            x = 1.0 if i % 2 == 0 else -1.0
            vectors.append([x] + [0.0] * 15)
            labels.append(UP if x > 0 else DOWN)
        return vectors, labels

    def test_it_learns_a_learnable_pattern(self):
        v, l = self._data()
        # 200 epochs, not the 8000 default: this proves the fitter
        # LEARNS, and a trivially separable pattern needs no more. The
        # default exists for real data, where 2000 was not enough.
        art = ST.train_logistic(v, l, training_start="a",
                                training_end="b", trained_at=NOW,
                                epochs=200)
        self.assertIsNotNone(art)
        correct = sum(1 for vec, lab in zip(v, l)
                      if ST.predict_logistic(art, vec)[0] == lab)
        self.assertGreater(correct / len(v), 0.95)

    def test_too_few_rows_returns_none_not_a_weak_model(self):
        # Coefficients fitted on fifty rows are numbers, not a model,
        # and returning one invites it to be read as a result.
        v, l = self._data(40)
        self.assertIsNone(ST.train_logistic(v, l, training_start="a",
                                            training_end="b",
                                            trained_at=NOW))

    def test_flat_rows_are_excluded_and_counted(self):
        # Calling a flat outcome "up" teaches the model that no
        # movement is a rise.
        v, l = self._data(300)
        v += [[0.0] * 16 for _ in range(50)]
        l += [FLAT] * 50
        art = ST.train_logistic(v, l, training_start="a",
                                training_end="b", trained_at=NOW,
                                epochs=50)
        self.assertEqual(art.hyperparameters["flat_rows_excluded"], 50)
        self.assertEqual(art.training_rows, 300)

    def test_convergence_is_reported_not_assumed(self):
        # 300 epochs did not converge on real data and 2000 did not
        # either. A non-converged fit still returns coefficients, so the
        # flag is the only way to notice.
        v, l = self._data()
        slow = ST.train_logistic(v, l, training_start="a",
                                 training_end="b", trained_at=NOW,
                                 epochs=2)
        self.assertFalse(slow.converged)

    def test_the_artifact_identity_includes_its_training_window(self):
        v, l = self._data()
        a = ST.train_logistic(v, l, training_start="2026-01-01",
                              training_end="2026-02-01", trained_at=NOW,
                              epochs=20)
        b = ST.train_logistic(v, l, training_start="2026-01-01",
                              training_end="2026-03-01", trained_at=NOW,
                              epochs=20)
        self.assertNotEqual(a.artifact_id, b.artifact_id)

    def test_a_retrained_model_is_a_new_artifact(self):
        # Overwriting one under the same identity would silently
        # invalidate every result attributed to it.
        v, l = self._data()
        a = ST.train_logistic(v, l, training_start="a", training_end="b",
                              trained_at=NOW, dataset_hash="h1", epochs=20)
        b = ST.train_logistic(v, l, training_start="a", training_end="b",
                              trained_at=NOW, dataset_hash="h2", epochs=20)
        self.assertNotEqual(a.artifact_id, b.artifact_id)

    def test_the_probability_is_not_called_confidence(self):
        # "Confidence" implies calibrated semantics, and calibration is
        # measured separately and may be poor.
        with open(os.path.join(REPO, "agent", "prediction",
                               "statistical.py")) as fh:
            src = fh.read()
        self.assertIn("NOT called", src)
        self.assertNotIn('"confidence"', src)


class TestRidge(unittest.TestCase):

    def _data(self, n=300):
        vectors, values = [], []
        for i in range(n):
            x = (i % 20) / 10.0 - 1.0
            vectors.append([x] + [0.0] * 15)
            values.append(3.0 * x + 0.5)
        return vectors, values

    def test_it_recovers_a_linear_relationship(self):
        v, y = self._data()
        art = ST.train_ridge(v, y, training_start="a", training_end="b",
                             trained_at=NOW, l2=0.0001)
        self.assertIsNotNone(art)
        errors = [abs(ST.predict_ridge(art, vec) - target)
                  for vec, target in zip(v, y)]
        self.assertLess(sum(errors) / len(errors), 0.1)

    def test_it_says_it_predicts_return_not_profit(self):
        # A +0.05% forecast is not an edge once a 0.05% spread is paid.
        v, y = self._data()
        art = ST.train_ridge(v, y, training_start="a", training_end="b",
                             trained_at=NOW)
        self.assertIn("does not predict profit", art.note)

    def test_too_few_rows_returns_none(self):
        v, y = self._data(20)
        self.assertIsNone(ST.train_ridge(v, y, training_start="a",
                                         training_end="b",
                                         trained_at=NOW))


class TestWalkForwardLeakage(unittest.TestCase):

    def _rows(self, n=300, per_stamp=1):
        rows = []
        day = 1
        for i in range(n):
            if i and i % per_stamp == 0:
                day += 1
            rows.append({
                "feature_time": f"2026-{1 + day // 28:02d}-"
                                f"{1 + day % 28:02d}T10:00:00",
                "feature_hash": f"h{i}",
                "vector": [1.0 if i % 2 == 0 else -1.0] + [0.0] * 15,
                "label": UP if i % 2 == 0 else DOWN,
            })
        return rows

    def _fit(self, train_rows):
        return ST.train_logistic(
            [r["vector"] for r in train_rows],
            [r["label"] for r in train_rows],
            training_start=train_rows[0]["feature_time"],
            training_end=train_rows[-1]["feature_time"],
            trained_at=NOW, epochs=50)

    def _predict(self, artifact, row):
        out = ST.predict_logistic(artifact, row["vector"])
        return None if out is None else {"prediction": out[0],
                                         "probability": out[1],
                                         "label": row["label"]}

    def test_no_prediction_predates_its_training_window(self):
        res = WF.run(self._rows(400), train_window=200, step=50,
                     fit=self._fit, predict=self._predict)
        self.assertEqual(res["voided_folds"], 0, res.get("folds"))
        for fold in res["folds"]:
            if fold["status"] != WF.COMPLETE:
                continue
            self.assertLess(fold["training_range"]["end"],
                            fold["prediction_range"]["start"])

    def test_a_leaking_fold_is_voided_whole_not_row_by_row(self):
        """If one row leaked, the window that produced it was wrong.

        Dropping the row would leave the rest of the fold - fitted on
        the same window - counted as clean.
        """
        fold = WF.Fold(index=0, train_start="a", train_end="2026-05-01",
                       predict_start="2026-05-01", predict_end="z",
                       train_rows=10, predict_rows=2)
        fold.predictions = [{"feature_time": "2026-05-01"},
                            {"feature_time": "2026-06-01"}]
        leak = WF.check_no_future_training(fold)
        self.assertIsNotNone(leak)
        self.assertIn("at or before", leak)

    def test_a_prediction_with_no_timestamp_is_a_leak(self):
        # It cannot be PROVEN to post-date training, which is the same
        # as not post-dating it.
        fold = WF.Fold(index=0, train_start="a", train_end="b",
                       predict_start="c", predict_end="d",
                       train_rows=1, predict_rows=1)
        fold.predictions = [{"probability": 0.6}]
        self.assertIn("no feature_time",
                      WF.check_no_future_training(fold))

    def test_multi_symbol_timestamps_do_not_split_mid_stamp(self):
        """The defect the leakage check caught on its first real run.

        Six symbols share every timestamp. Slicing by ROW index put some
        of a timestamp's rows in training and the rest in prediction, so
        the model trained on one symbol at time T and predicted another
        at time T. All five folds voided, correctly.
        """
        res = WF.run(self._rows(360, per_stamp=6), train_window=120,
                     step=60, fit=self._fit, predict=self._predict)
        self.assertEqual(res["voided_folds"], 0,
                         "a fold split inside a timestamp group")
        self.assertGreater(res["complete_folds"], 0)

    def test_insufficient_data_is_not_a_result(self):
        res = WF.run(self._rows(20), train_window=200, step=50,
                     fit=self._fit, predict=self._predict)
        self.assertEqual(res["status"], WF.INSUFFICIENT_DATA)
        self.assertIn("nothing was measured", res["note"])


class TestStability(unittest.TestCase):

    def test_a_mean_alone_would_hide_one_lucky_fold(self):
        # One huge win and five losses averages above a baseline while
        # beating it in one fold out of six.
        out = WF.stability([0.9, 0.3, 0.3, 0.3, 0.3, 0.3], baseline=0.45)
        self.assertEqual(out["folds_beating_baseline"], 1)
        self.assertIn("MIXED", out["verdict"])
        self.assertIn("not stability", out["verdict"])

    def test_consistent_and_never_are_distinguished(self):
        self.assertEqual(
            WF.stability([0.6, 0.7, 0.65], baseline=0.5)["verdict"],
            "CONSISTENT")
        self.assertEqual(
            WF.stability([0.4, 0.3, 0.2], baseline=0.5)["verdict"],
            "NEVER")

    def test_no_scores_gives_no_mean(self):
        self.assertIsNone(WF.stability([None, None])["mean"])


class TestResearchOnlyStaysStructural(unittest.TestCase):

    def test_the_new_modules_import_no_decision_path(self):
        from agent.prediction import FORBIDDEN_IMPORTS
        for name in ("splits.py", "statistical.py", "walkforward.py"):
            path = os.path.join(REPO, "agent", "prediction", name)
            with open(path) as fh:
                src = fh.read()
            for forbidden in FORBIDDEN_IMPORTS:
                self.assertNotIn(f"import {forbidden}", src,
                                 f"{name} -> {forbidden}")
                self.assertNotIn(f"from {forbidden}", src,
                                 f"{name} -> {forbidden}")


if __name__ == "__main__":
    unittest.main()


class TestBalancedAccuracyIsReported(unittest.TestCase):
    """Plain accuracy on an imbalanced target rewards the majority.

    Measured: UP occurs 44.7% of the time, so ALWAYS_UP scores 0.4472
    and beat every model tried. Balanced accuracy cannot be gamed that
    way - a constant scores 1/n_classes however skewed the data.
    """

    def test_a_constant_predictor_scores_one_third_balanced(self):
        from agent.prediction import maturation as PM
        from agent.prediction import metrics as PMET
        from agent.prediction import models as PMOD

        preds, outs = [], []
        for i, realized in enumerate([UP] * 45 + [DOWN] * 39
                                     + [FLAT] * 16):
            p = PMOD.PredictionRecord(
                prediction_id=f"p{i}", model=PMOD.ALWAYS_UP,
                model_version="v", symbol="TEST", target="DIRECTION",
                horizon_minutes=15, prediction=UP, probability=None,
                prediction_feature_id=f"f{i}", feature_hash=f"h{i}",
                feature_time=f"2026-01-01T10:{i:02d}:00",
                feature_schema_version="v", generated_at=NOW)
            preds.append(p)
            outs.append(PMOD.PredictionOutcome(
                prediction_id=p.prediction_id, status=PMOD.MATURED,
                evaluated_at=NOW, realized_direction=realized,
                realized_return_pct=1.0 if realized == UP else -1.0,
                correct=(realized == UP)))
        rep = PMET.report(preds, outs)
        g = rep["by_model"][PMOD.ALWAYS_UP]
        self.assertAlmostEqual(g["directional_accuracy"], 0.45, places=2)
        # Plain accuracy looks respectable; balanced accuracy does not.
        self.assertAlmostEqual(g["balanced_accuracy"], 1 / 3, places=2)
        self.assertEqual(
            rep["verdict_by_model"][PMOD.ALWAYS_UP]["verdict"],
            "NO_DEMONSTRATED_PREDICTIVE_EDGE")

    def test_the_hardest_baseline_is_not_assumed_to_be_one_half(self):
        from agent.prediction import metrics as PMET
        from agent.prediction import models as PMOD
        preds, outs = [], []
        for i, realized in enumerate([UP] * 45 + [DOWN] * 39
                                     + [FLAT] * 16):
            p = PMOD.PredictionRecord(
                prediction_id=f"q{i}", model=PMOD.NO_SKILL,
                model_version="v", symbol="TEST", target="DIRECTION",
                horizon_minutes=15, prediction=UP, probability=None,
                prediction_feature_id=f"f{i}", feature_hash=f"h{i}",
                feature_time=f"2026-01-01T10:{i:02d}:00",
                feature_schema_version="v", generated_at=NOW)
            preds.append(p)
            outs.append(PMOD.PredictionOutcome(
                prediction_id=p.prediction_id, status=PMOD.MATURED,
                evaluated_at=NOW, realized_direction=realized,
                correct=(realized == UP)))
        bc = PMET.baseline_comparison(
            preds, {o.prediction_id: o for o in outs})
        name, value = bc["hardest_baseline"]
        self.assertEqual(name, "ALWAYS_UP")
        self.assertAlmostEqual(value, 0.45, places=2)
        # And the two-class flip is NOT 0.5 on a three-class target.
        self.assertLess(bc["baselines"]["UNIFORM_TWO_CLASS_FLIP"], 0.5)
