"""Scoring predictions, with the denominators visible.

NO AGGREGATE SCORE

There is no single number. Directional accuracy, error magnitude,
information coefficient and calibration measure different things, and a
weighted blend of them would hide which one moved. The same rule the
rest of this system follows: keep the dimensions separate.

EVERY RATE CARRIES ITS DENOMINATOR

A 100% accuracy over two predictions is not a result. `sample_size` is
reported beside every rate, and `comparable` states whether the sample
is large enough to be set against another group.

WHAT IS EXCLUDED, AND COUNTED

  - PENDING    the horizon has not elapsed. Not a miss.
  - VOID       the prediction could not be scored at all.
  - ABSTAIN    the model made no claim. Scoring it as wrong punishes
               honesty; scoring it as right rewards silence.

Each is counted in its own field. A metric that quietly drops them
improves every rate it reports.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (ABSTAIN, DETERMINISTIC_MODELS, MATURED, PENDING,
                     VOID, PredictionOutcome, PredictionRecord)
from .targets import DIRECTION, DOWN, FLAT, FORWARD_RETURN, UP

# Below this, a rate is reported but must not be compared with another
# group. Not a significance test - an honest floor, stated rather than
# implied.
MIN_COMPARABLE_SAMPLE = 30


def _mean(values: Sequence[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Correlation, or None when it is undefined.

    None rather than 0.0 when either series is constant: a constant
    predictor has NO correlation with the outcome, which is not the same
    as a measured correlation of zero.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / math.sqrt(sxx * syy), 6)


def _brier(probabilities: Sequence[float],
           actuals: Sequence[int]) -> Optional[float]:
    if not probabilities or len(probabilities) != len(actuals):
        return None
    return round(_mean([(p - a) ** 2 for p, a in
                        zip(probabilities, actuals)]), 6)


def _log_loss(probabilities: Sequence[float],
              actuals: Sequence[int]) -> Optional[float]:
    """Log loss, with probabilities clipped away from 0 and 1.

    An unclipped 0 or 1 that is wrong gives infinite loss, which would
    make one confident mistake swamp every other observation. Clipping
    is standard and is stated rather than hidden.
    """
    if not probabilities or len(probabilities) != len(actuals):
        return None
    eps = 1e-15
    total = 0.0
    for p, a in zip(probabilities, actuals):
        p = min(max(p, eps), 1 - eps)
        total += -(a * math.log(p) + (1 - a) * math.log(1 - p))
    return round(total / len(probabilities), 6)


def _calibration(probabilities: Sequence[float], actuals: Sequence[int],
                 bins: int = 5) -> List[Dict]:
    """Predicted probability against observed frequency, per bin.

    Empty bins are OMITTED rather than reported as 0% observed. A bin
    nobody predicted into has no observed frequency, and showing zero
    would read as a catastrophic miscalibration that never happened.
    """
    out: List[Dict] = []
    for i in range(bins):
        low, high = i / bins, (i + 1) / bins
        pairs = [(p, a) for p, a in zip(probabilities, actuals)
                 if (low <= p < high or (i == bins - 1 and p == 1.0))]
        if not pairs:
            continue
        out.append({
            "bin": f"{low:.1f}-{high:.1f}",
            "predicted_mean": round(_mean([p for p, _ in pairs]), 6),
            "observed_rate": round(_mean([float(a) for _, a in pairs]), 6),
            "sample_size": len(pairs),
        })
    return out


def score_group(predictions: Sequence[PredictionRecord],
                outcomes: Dict[str, PredictionOutcome]) -> Dict:
    """Metrics for one group of predictions. No blended score."""
    total = len(predictions)
    pending = voided = abstained = 0
    scored: List[Tuple[PredictionRecord, PredictionOutcome]] = []

    for prediction in predictions:
        outcome = outcomes.get(prediction.prediction_id)
        if outcome is None or outcome.status == PENDING:
            pending += 1
            continue
        if outcome.status == VOID:
            voided += 1
            continue
        if prediction.prediction == ABSTAIN:
            abstained += 1
            continue
        scored.append((prediction, outcome))

    directional = [(p, o) for p, o in scored
                   if p.target == DIRECTION and o.correct is not None]
    regression = [(p, o) for p, o in scored
                  if p.target == FORWARD_RETURN and o.error is not None]

    result: Dict[str, Any] = {
        "total_predictions": total,
        "scored": len(scored),
        "pending": pending,
        "void": voided,
        "abstained": abstained,
        "sample_size": len(directional) or len(regression),
        "comparable": (len(directional) or len(regression))
                      >= MIN_COMPARABLE_SAMPLE,
        "min_comparable_sample": MIN_COMPARABLE_SAMPLE,
    }
    if not result["comparable"]:
        result["note"] = (
            f"only {result['sample_size']} scored prediction(s); not "
            f"comparable with another group below "
            f"{MIN_COMPARABLE_SAMPLE}")

    if directional:
        correct = sum(1 for _, o in directional if o.correct)
        result["directional_accuracy"] = round(
            correct / len(directional), 6)
        result["directional_correct"] = correct
        # Per-class precision and recall. A model that always says UP
        # can score well on accuracy alone in a rising market; these
        # are what expose that.
        per_class = {}
        for label in (UP, DOWN, FLAT):
            predicted = [o for p, o in directional
                         if p.prediction == label]
            actual = [o for _, o in directional
                      if o.realized_direction == label]
            tp = sum(1 for o in predicted
                     if o.realized_direction == label)
            per_class[label] = {
                "predicted": len(predicted),
                "actual": len(actual),
                "true_positives": tp,
                # None, not zero: a model that never predicted UP has
                # UNDEFINED precision for UP, not perfect or terrible.
                "precision": (round(tp / len(predicted), 6)
                              if predicted else None),
                "recall": (round(tp / len(actual), 6) if actual else None),
            }
        result["per_class"] = per_class

        # BALANCED ACCURACY: the mean of per-class recall.
        #
        # Plain accuracy on an imbalanced target rewards predicting the
        # majority. On the real data UP occurs 44.7% of the time, so
        # "always UP" scores 0.4472 and beat every model tried - a
        # number that looks like skill and is not. Balanced accuracy
        # cannot be gamed that way: a constant predictor scores
        # 1/n_classes however skewed the data.
        recalls = [v["recall"] for v in per_class.values()
                   if v["recall"] is not None]
        result["balanced_accuracy"] = (round(_mean(recalls), 6)
                                       if recalls else None)
        result["balanced_accuracy_note"] = (
            "mean of per-class recall over the classes that OCCUR. A "
            "constant predictor scores 1/n_classes no matter how skewed "
            "the target, which plain accuracy does not penalise.")
        result["classes_observed"] = len(recalls)

        # F1 per class, where both precision and recall are defined.
        for label, v in per_class.items():
            pr, rc = v["precision"], v["recall"]
            v["f1"] = (round(2 * pr * rc / (pr + rc), 6)
                       if pr and rc and (pr + rc) > 0 else None)

        # Information coefficient: the predicted sign against the
        # realised return. Measures whether the call carries information
        # about MAGNITUDE, which accuracy alone cannot show.
        signs, realized = [], []
        for p, o in directional:
            if o.realized_return_pct is None:
                continue
            sign = {UP: 1.0, DOWN: -1.0, FLAT: 0.0}.get(p.prediction)
            if sign is None:
                continue
            signs.append(sign)
            realized.append(o.realized_return_pct)
        result["information_coefficient"] = _pearson(signs, realized)

    if regression:
        errors = [abs(o.error) for _, o in regression]
        squared = [o.error ** 2 for _, o in regression]
        result["mae"] = round(_mean(errors), 6)
        result["rmse"] = round(math.sqrt(_mean(squared)), 6)
        predicted = [float(p.prediction) for p, _ in regression]
        actual = [o.realized_return_pct for _, o in regression]
        if all(a is not None for a in actual):
            result["information_coefficient"] = _pearson(predicted, actual)

    # Probabilistic metrics, for probabilistic models only. A
    # deterministic rule computed no likelihood, so there is nothing to
    # calibrate and a Brier score over invented numbers would be
    # meaningless.
    probabilistic = [(p, o) for p, o in directional
                     if p.probability is not None
                     and p.model not in DETERMINISTIC_MODELS]
    if probabilistic:
        probs = [p.probability for p, _ in probabilistic]
        actuals = [1 if o.correct else 0 for _, o in probabilistic]
        result["brier_score"] = _brier(probs, actuals)
        result["log_loss"] = _log_loss(probs, actuals)
        result["calibration"] = _calibration(probs, actuals)
        result["probabilistic_sample_size"] = len(probabilistic)
    else:
        result["brier_score"] = None
        result["log_loss"] = None
        result["calibration_note"] = (
            "no probabilistic predictions in this group; a deterministic "
            "rule computed no likelihood, so there is nothing to "
            "calibrate")

    return result


def baseline_comparison(predictions: Sequence[PredictionRecord],
                        outcomes: Dict[str, PredictionOutcome]) -> Dict:
    """What the trivial baselines score on THESE outcomes.

    Computed from the realised labels actually present, not assumed. An
    assumed 0.50 is wrong for a three-class target: a two-class
    predictor cannot score on the FLAT rows at all, so its expectation
    is (P(UP)+P(DOWN))/2 - which measured 0.4200 against NO_SKILL's
    observed 0.4182.
    """
    realized = []
    for prediction in predictions:
        outcome = outcomes.get(prediction.prediction_id)
        if (outcome is None or outcome.status != MATURED
                or outcome.realized_direction is None):
            continue
        realized.append(outcome.realized_direction)
    if not realized:
        return {"sample_size": 0,
                "note": ("no matured outcome carries a realised "
                         "direction, so no baseline can be computed")}
    n = len(realized)
    counts: Dict[str, int] = {}
    for label in realized:
        counts[label] = counts.get(label, 0) + 1
    distribution = {k: round(v / n, 6) for k, v in counts.items()}
    majority = max(counts.items(), key=lambda kv: kv[1])[0]
    two_class = sum(v for k, v in distribution.items()
                    if k in (UP, DOWN))
    return {
        "sample_size": n,
        "class_distribution": distribution,
        "majority_class": majority,
        "baselines": {
            f"ALWAYS_{majority}": distribution[majority],
            "UNIFORM_TWO_CLASS_FLIP": round(two_class / 2.0, 6),
            "RANDOM_CLASS_PRIOR": round(
                sum(v * v for v in distribution.values()), 6),
            "BALANCED_ACCURACY_OF_ANY_CONSTANT": round(
                1.0 / max(1, len(counts)), 6),
        },
        "hardest_baseline": max(
            [(f"ALWAYS_{majority}", distribution[majority]),
             ("UNIFORM_TWO_CLASS_FLIP", round(two_class / 2.0, 6)),
             ("RANDOM_CLASS_PRIOR",
              round(sum(v * v for v in distribution.values()), 6))],
            key=lambda kv: kv[1]),
        "note": ("A model must beat the HARDEST of these, not 0.50. "
                 "Measured on real data, ALWAYS_UP scored 0.4472 and "
                 "beat every model tried."),
    }


def verdict(group: Dict, comparison: Dict) -> Dict:
    """Does this group beat the hardest trivial baseline?

    Said in words, because a table of numbers invites the reader to
    find the one that looks best.
    """
    acc = group.get("directional_accuracy")
    hardest = (comparison or {}).get("hardest_baseline")
    if acc is None or hardest is None:
        return {"verdict": "NOT_MEASURABLE",
                "detail": ("no directional accuracy or no baseline; "
                           "nothing can be concluded")}
    name, value = hardest
    if not group.get("comparable"):
        return {"verdict": "SAMPLE_TOO_SMALL",
                "detail": (f"{group.get('sample_size')} scored "
                           f"prediction(s) is below the comparable "
                           f"threshold; the number is reported but no "
                           f"comparison is justified")}
    if acc > value:
        return {"verdict": "ABOVE_BASELINE",
                "detail": (f"{acc} beats {name} at {value}. Check the "
                           f"information coefficient before reading "
                           f"that as skill: a model can be more often "
                           f"right while its calls are anti-correlated "
                           f"with the size of the move."),
                "baseline": name, "baseline_value": value}
    return {"verdict": "NO_DEMONSTRATED_PREDICTIVE_EDGE",
            "detail": f"{acc} does not beat {name} at {value}.",
            "baseline": name, "baseline_value": value}


def _time_bucket(feature_time: str) -> str:
    """Morning, midday or afternoon, from the timestamp.

    Coarse on purpose: finer buckets would split an already small
    sample into groups too thin to compare.
    """
    try:
        hh = int(feature_time.split("T")[1][:2])
    except (IndexError, ValueError):
        return "UNKNOWN"
    if hh < 11:
        return "MORNING"
    if hh < 14:
        return "MIDDAY"
    return "AFTERNOON"


def report(predictions: Sequence[PredictionRecord],
           outcomes: Sequence[PredictionOutcome]) -> Dict:
    """Metrics broken out by model, horizon, regime and time of day.

    Deliberately NOT summed into one figure. A blend would hide which
    dimension moved, which is the only thing these numbers are for.
    """
    index = {o.prediction_id: o for o in outcomes}
    out: Dict[str, Any] = {
        "overall_note": (
            "There is deliberately no single score. Accuracy, error, "
            "information coefficient and calibration measure different "
            "things and a blend would hide which one moved."),
        "mode": "RESEARCH_ONLY",
        "execution_influence": (
            "NONE - predictions do not affect scanner selection, "
            "hypotheses, the Risk Governor, sizing, exits or any order"),
        "by_model": {}, "by_horizon": {}, "by_regime": {},
        "by_time_of_day": {}, "by_model_and_horizon": {},
    }

    def group(key_fn):
        buckets: Dict[Any, List[PredictionRecord]] = {}
        for prediction in predictions:
            buckets.setdefault(key_fn(prediction), []).append(prediction)
        return {str(k): score_group(v, index)
                for k, v in sorted(buckets.items(), key=lambda kv: str(kv[0]))}

    out["by_model"] = group(lambda p: p.model)
    out["by_horizon"] = group(lambda p: p.horizon_minutes)
    out["by_regime"] = group(lambda p: p.regime_state or "UNKNOWN")
    out["by_time_of_day"] = group(lambda p: _time_bucket(p.feature_time))
    out["by_model_and_horizon"] = group(
        lambda p: f"{p.model}@{p.horizon_minutes}m")
    out["totals"] = score_group(predictions, index)
    out["baseline_comparison"] = baseline_comparison(predictions, index)
    out["verdict"] = verdict(out["totals"], out["baseline_comparison"])
    # Per model too, so one model clearing the bar cannot be read off
    # an aggregate that it dragged up.
    out["verdict_by_model"] = {
        model: verdict(group, out["baseline_comparison"])
        for model, group in out["by_model"].items()}
    return out
