"""Walk-forward evaluation: train on the past, predict the unseen next.

THE ONE INVARIANT

No prediction may be produced by a model whose training window extends
to or past that prediction's feature_time. Checked on every prediction,
and a violation makes the whole fold VOID rather than dropping the row:
if one row leaked, the window that produced it is wrong, and the rest of
that fold was fitted on the same window.

WHY NOT A SINGLE TRAIN/TEST SPLIT

A single split answers "did this work once". Walk-forward answers "did
it keep working", which is the only version of the question that
matters for something meant to run every day. A model that beats its
baseline in one fold and loses in five has not shown anything, and a
single split can easily land on the one fold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .splits import class_prior, dataset_fingerprint, fit_scaler

LEAKAGE_VOID = "LEAKAGE_VOID"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
COMPLETE = "COMPLETE"


@dataclass
class Fold:
    """One train-then-predict step, with its exact boundaries."""
    index: int
    train_start: str
    train_end: str
    predict_start: str
    predict_end: str
    train_rows: int
    predict_rows: int
    status: str = COMPLETE
    model_artifact_id: Optional[str] = None
    model_version: Optional[str] = None
    feature_schema_version: Optional[str] = None
    dataset_checksum: Optional[str] = None
    training_dataset_hash: Optional[str] = None
    hyperparameters: Dict[str, Any] = field(default_factory=dict)
    class_prior: Optional[Dict] = None
    predictions: List[Dict] = field(default_factory=list)
    void_reason: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "index": self.index,
            "training_range": {"start": self.train_start,
                               "end": self.train_end,
                               "rows": self.train_rows},
            "prediction_range": {"start": self.predict_start,
                                 "end": self.predict_end,
                                 "rows": self.predict_rows},
            "status": self.status,
            "model_artifact_id": self.model_artifact_id,
            "model_version": self.model_version,
            "feature_schema_version": self.feature_schema_version,
            "dataset_checksum": self.dataset_checksum,
            "training_dataset_hash": self.training_dataset_hash,
            "hyperparameters": self.hyperparameters,
            "class_prior": self.class_prior,
            "predictions": len(self.predictions),
            "void_reason": self.void_reason,
        }


def check_no_future_training(fold: Fold) -> Optional[str]:
    """None if clean, otherwise why the fold leaked.

    The check that makes walk-forward mean anything. Compared on the
    stored STRINGS, which are ISO timestamps and therefore order
    correctly as text - and comparing strings avoids a timezone
    conversion silently shifting a boundary by hours.
    """
    for row in fold.predictions:
        ft = row.get("feature_time")
        if ft is None:
            return "a prediction carries no feature_time, so it cannot "\
                   "be proven to post-date its training window"
        if ft <= fold.train_end:
            return (f"prediction at {ft} is at or before the training "
                    f"window end {fold.train_end}: the model saw this "
                    f"row, or one simultaneous with it, before "
                    f"predicting it")
    return None


def run(rows: Sequence[Dict], *, train_window: int, step: int,
        fit, predict, min_train: int = 100,
        dataset_checksum: Optional[str] = None,
        expanding: bool = True) -> Dict:
    """Walk forward through `rows`, which must be sorted by feature_time.

    `rows` each carry feature_time, vector, and whatever targets the
    caller's `fit`/`predict` need. `fit(train_rows)` returns an artifact
    or None; `predict(artifact, row)` returns a dict or None.

    `expanding` grows the training window from the start, which is what
    a system that retrains daily actually does. A rolling window is the
    alternative and is NOT the default, because discarding old data is
    a modelling choice that should be made deliberately.
    """
    ordered = sorted(rows, key=lambda r: r.get("feature_time") or "")
    if len(ordered) < min_train + step:
        return {"status": INSUFFICIENT_DATA, "folds": [],
                "rows": len(ordered),
                "detail": (f"{len(ordered)} rows is fewer than "
                           f"{min_train + step} needed for one fold"),
                "note": ("INSUFFICIENT_DATA is not a result; no fold ran "
                         "so nothing was measured")}

    # SPLIT ON TIMESTAMP BOUNDARIES, NOT ROW INDICES.
    #
    # A multi-symbol dataset has several rows per timestamp. Slicing by
    # row index puts some of a timestamp's rows in training and the rest
    # in prediction, so the model trains on AAPL at time T and then
    # predicts MSFT at time T. The leakage check caught this on its
    # first real run: all five folds VOIDED, which was correct.
    #
    # Grouping by timestamp makes every fold boundary a point in time,
    # which is the only kind of boundary that means anything here.
    groups: Dict[str, List[Dict]] = {}
    for row in ordered:
        groups.setdefault(row.get("feature_time") or "", []).append(row)
    stamps = sorted(groups)

    def rows_for(slice_of_stamps) -> List[Dict]:
        return [r for s in slice_of_stamps for r in groups[s]]

    # Window sizes are expressed in ROWS by the caller, so convert them
    # to a count of timestamps - otherwise a six-symbol dataset would
    # get a sixth of the intended history.
    per_stamp = max(1, len(ordered) // max(1, len(stamps)))
    train_stamps = max(1, train_window // per_stamp)
    step_stamps = max(1, step // per_stamp)
    min_train_stamps = max(1, min_train // per_stamp)

    folds: List[Fold] = []
    start = 0
    train_end_idx = max(train_stamps, min_train_stamps)
    index = 0
    while train_end_idx + step_stamps <= len(stamps):
        train_rows = rows_for(
            stamps[(0 if expanding else start):train_end_idx])
        predict_rows = rows_for(
            stamps[train_end_idx:train_end_idx + step_stamps])
        fold = Fold(
            index=index,
            train_start=train_rows[0]["feature_time"],
            train_end=train_rows[-1]["feature_time"],
            predict_start=predict_rows[0]["feature_time"],
            predict_end=predict_rows[-1]["feature_time"],
            train_rows=len(train_rows), predict_rows=len(predict_rows),
            dataset_checksum=dataset_checksum,
            training_dataset_hash=dataset_fingerprint(
                [r.get("feature_hash", "") for r in train_rows]),
        )
        artifact = fit(train_rows)
        if artifact is None:
            fold.status = INSUFFICIENT_DATA
            fold.void_reason = ("the model declined to fit on this "
                                "training window")
        else:
            fold.model_artifact_id = artifact.artifact_id
            fold.model_version = artifact.model_version
            fold.feature_schema_version = artifact.feature_schema_version
            fold.hyperparameters = dict(artifact.hyperparameters)
            fold.class_prior = artifact.class_prior
            for row in predict_rows:
                out = predict(artifact, row)
                if out is not None:
                    out["feature_time"] = row["feature_time"]
                    fold.predictions.append(out)
            leak = check_no_future_training(fold)
            if leak:
                # VOID the FOLD, not the row. If one prediction leaked,
                # the window that produced it is wrong and every other
                # prediction in the fold came from the same window.
                fold.status = LEAKAGE_VOID
                fold.void_reason = leak
                fold.predictions = []
        folds.append(fold)
        start += step_stamps
        train_end_idx += step_stamps
        index += 1

    complete = [f for f in folds if f.status == COMPLETE]
    voided = [f for f in folds if f.status == LEAKAGE_VOID]
    return {
        "status": COMPLETE if complete else INSUFFICIENT_DATA,
        "folds": [f.to_dict() for f in folds],
        "fold_count": len(folds),
        "complete_folds": len(complete),
        "voided_folds": len(voided),
        "insufficient_folds": len(folds) - len(complete) - len(voided),
        "predictions": sum(len(f.predictions) for f in complete),
        "expanding_window": expanding,
        "all_predictions": [p for f in complete for p in f.predictions],
        "leakage_note": (
            "Every fold is checked: no prediction may come from a model "
            "whose training window reaches its feature_time. A violation "
            "VOIDS the fold, because one leaked row means the window was "
            "wrong for all of them."),
    }


def stability(fold_scores: Sequence[Optional[float]],
              baseline: Optional[float] = None) -> Dict:
    """Did it keep working, or work once?

    Reports the spread and how many folds beat the baseline. A mean
    alone hides a model that wins hugely in one fold and loses in five,
    which is the shape most likely to be mistaken for an edge.
    """
    scores = [s for s in fold_scores if s is not None]
    if not scores:
        return {"folds_scored": 0, "mean": None,
                "note": "no fold produced a score"}
    n = len(scores)
    mean = sum(scores) / n
    sd = ((sum((s - mean) ** 2 for s in scores) / (n - 1)) ** 0.5
          if n > 1 else None)
    out: Dict[str, Any] = {
        "folds_scored": n,
        "mean": round(mean, 6),
        "stdev": None if sd is None else round(sd, 6),
        "min": round(min(scores), 6),
        "max": round(max(scores), 6),
    }
    if baseline is not None:
        beat = sum(1 for s in scores if s > baseline)
        out["baseline"] = baseline
        out["folds_beating_baseline"] = beat
        out["fraction_beating_baseline"] = round(beat / n, 6)
        out["verdict"] = (
            "CONSISTENT" if beat == n else
            "NEVER" if beat == 0 else
            f"MIXED - beat the baseline in {beat} of {n} folds, which is "
            f"not stability")
    return out
