"""Turning a prediction into an outcome, without rewriting the claim.

A PredictionRecord is frozen. Maturation produces a SEPARATE
PredictionOutcome that references it by id. Nothing here can edit what
was predicted, which is the only arrangement under which a scoreboard
means anything.

A HORIZON THAT HAS NOT ELAPSED IS PENDING, NOT WRONG

The commonest way to flatter a prediction system is to evaluate only
the predictions that already have answers and quietly leave the rest
out of the denominator. PENDING is returned explicitly and counted
separately.

AN ABSTENTION IS NOT AN ERROR

A model that said ABSTAIN made no claim. Scoring it as wrong punishes
honesty and scoring it as right rewards silence, so it is excluded from
accuracy with its own count.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .models import (ABSTAIN, MATURED, PENDING, VOID, PredictionOutcome,
                     PredictionRecord)
from .targets import (AMBIGUOUS_SAME_BAR, DIRECTION, DOWN, FLAT,
                      FORWARD_RETURN, UP, direction as target_direction,
                      excursions, forward_return, stop_before_target)


def _index_of_feature_time(bars: Sequence[Any],
                           feature_time: str) -> Optional[int]:
    """Where in this series the prediction was made.

    Matched on the TIMESTAMP, never on a stored index. An index is only
    meaningful against the exact series it came from; the same
    prediction evaluated against a differently-sliced dataset would
    silently point at another bar.
    """
    for i, bar in enumerate(bars):
        ts = bar.get("timestamp") if isinstance(bar, dict) else getattr(
            bar, "timestamp", None)
        if ts is None:
            continue
        ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if ts == feature_time:
            return i
    return None


def mature(prediction: PredictionRecord, bars: Sequence[Any], *,
           evaluated_at: str,
           bar_interval_seconds: float = 60.0,
           dataset_checksum: Optional[str] = None) -> PredictionOutcome:
    """The outcome of one prediction, or why there isn't one yet.

    VOID when the prediction cannot be located in this series, or when
    the series is provably not the one it was made from.

    THE CHECKSUM CHECK IS NOT REDUNDANT WITH THE TIMESTAMP CHECK.

    A test caught this: two different price series built over the same
    minutes share every timestamp. A prediction made on a rising series
    was located in a falling one and MATURED against prices it had never
    seen, reporting a confident wrong answer instead of VOID. The
    timestamp identifies the POSITION; only the checksum identifies the
    DATA.

    When the caller supplies no checksum the check is skipped and that
    is recorded in the void_reason vocabulary rather than silently
    assumed safe - a caller who cannot say which dataset this is cannot
    be told their prediction matched the right one.
    """
    if (dataset_checksum is not None
            and prediction.dataset_checksum is not None
            and dataset_checksum != prediction.dataset_checksum):
        return PredictionOutcome(
            prediction_id=prediction.prediction_id, status=VOID,
            evaluated_at=evaluated_at,
            void_reason=(
                f"dataset mismatch: the prediction was made on "
                f"{prediction.dataset_checksum} and is being scored "
                f"against {dataset_checksum}. Identical timestamps in "
                f"two different series would otherwise mature it "
                f"against prices it never saw."))

    index = _index_of_feature_time(bars, prediction.feature_time)
    if index is None:
        return PredictionOutcome(
            prediction_id=prediction.prediction_id, status=VOID,
            evaluated_at=evaluated_at,
            void_reason=(
                f"feature_time {prediction.feature_time} does not appear "
                f"in this series, so the prediction cannot be scored "
                f"against it"))

    fr = forward_return(bars, index, prediction.horizon_minutes,
                        bar_interval_seconds)
    if not fr.resolved:
        # Not wrong - unanswerable yet. Reported as PENDING with the
        # reason, so it is excluded from accuracy rather than counted
        # as a miss.
        return PredictionOutcome(
            prediction_id=prediction.prediction_id, status=PENDING,
            evaluated_at=evaluated_at, void_reason=fr.reason)

    realized_return = float(fr.value)
    realized_dir = target_direction(bars, index,
                                    prediction.horizon_minutes,
                                    bar_interval_seconds)
    mfe, mae = excursions(bars, index, prediction.horizon_minutes,
                          bar_interval_seconds)
    stop_target = stop_before_target(bars, index, None,
                                     bar_interval_seconds)

    correct: Optional[bool] = None
    error: Optional[float] = None

    if prediction.prediction == ABSTAIN:
        # No claim was made, so there is nothing to be right about.
        # Left as None and counted separately by the metrics.
        correct = None
    elif prediction.target == DIRECTION:
        if realized_dir.resolved:
            correct = (prediction.prediction == realized_dir.value)
    elif prediction.target == FORWARD_RETURN:
        try:
            error = round(float(prediction.prediction) - realized_return, 6)
        except (TypeError, ValueError):
            error = None
        # A regression has no "correct"; leaving it None keeps it out of
        # directional accuracy instead of inventing a threshold.
        correct = None

    return PredictionOutcome(
        prediction_id=prediction.prediction_id,
        status=MATURED,
        evaluated_at=evaluated_at,
        realized_return_pct=realized_return,
        realized_direction=(realized_dir.value if realized_dir.resolved
                            else None),
        max_favorable_excursion_pct=(mfe.value if mfe.resolved else None),
        max_adverse_excursion_pct=(mae.value if mae.resolved else None),
        # AMBIGUOUS_SAME_BAR is carried through, never collapsed: a bar
        # spanning both levels has a genuinely unknown order, and both
        # available guesses are wrong in a direction that matters.
        stop_target_outcome=(stop_target.value if stop_target.resolved
                             else None),
        correct=correct,
        error=error,
    )


def mature_all(predictions: Sequence[PredictionRecord],
               bars_by_symbol: Dict[str, Sequence[Any]], *,
               evaluated_at: str,
               bar_interval_seconds: float = 60.0,
               dataset_checksum: Optional[str] = None
               ) -> List[PredictionOutcome]:
    """Outcomes for many predictions, including the ones with no answer.

    A prediction whose symbol is absent from `bars_by_symbol` is VOID
    rather than skipped: silently dropping it would shrink the
    denominator and improve every rate.
    """
    out: List[PredictionOutcome] = []
    for prediction in predictions:
        bars = bars_by_symbol.get(prediction.symbol)
        if not bars:
            out.append(PredictionOutcome(
                prediction_id=prediction.prediction_id, status=VOID,
                evaluated_at=evaluated_at,
                void_reason=(f"no bars supplied for "
                             f"{prediction.symbol}")))
            continue
        out.append(mature(prediction, bars, evaluated_at=evaluated_at,
                          bar_interval_seconds=bar_interval_seconds,
                          dataset_checksum=dataset_checksum))
    return out
