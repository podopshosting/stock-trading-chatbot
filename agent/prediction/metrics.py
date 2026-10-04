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
    return out
