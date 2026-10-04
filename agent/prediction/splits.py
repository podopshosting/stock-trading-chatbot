"""Chronological splits, class priors, and train-only scaling.

NO RANDOM SHUFFLE, EVER

A shuffled split lets a model see tomorrow while being scored on
yesterday. On price data that is not a small bias: adjacent bars are
strongly correlated, so a shuffled holdout is almost a copy of the
training set and every metric improves for the wrong reason.

TRAIN-ONLY STATISTICS

Means, standard deviations, imputation values and class priors are
computed on TRAINING ROWS ONLY. Computing a mean over train+validation+
holdout leaks the future into every scaled feature, and the leak is
invisible: the numbers look reasonable and the model is simply better
than it should be.

THE HOLDOUT IS CONSUMED BY BEING LOOKED AT

Once a decision is made after inspecting holdout results, that holdout
is no longer a holdout. `consumed` records it, because an honest
accounting of how many times the final set was used is the only defence
against selecting the best of many attempts.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

TRAIN = "TRAIN"
VALIDATION = "VALIDATION"
HOLDOUT = "HOLDOUT"

DEFAULT_FRACTIONS = (0.6, 0.2, 0.2)


@dataclass(frozen=True)
class ChronologicalSplit:
    """Time boundaries, stored exactly rather than as fractions.

    Fractions are reproducible only against an identical row count. The
    boundaries are the facts a later run must match.
    """
    train_start: str
    train_end: str
    validation_start: Optional[str]
    validation_end: Optional[str]
    holdout_start: Optional[str]
    holdout_end: Optional[str]
    train_rows: int
    validation_rows: int
    holdout_rows: int
    shuffled: bool = False
    holdout_consumed: bool = False
    note: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "train": {"start": self.train_start, "end": self.train_end,
                      "rows": self.train_rows},
            "validation": {"start": self.validation_start,
                           "end": self.validation_end,
                           "rows": self.validation_rows},
            "holdout": {"start": self.holdout_start,
                        "end": self.holdout_end,
                        "rows": self.holdout_rows},
            "shuffled": self.shuffled,
            "holdout_consumed": self.holdout_consumed,
            "note": self.note,
        }

    def assigned(self, feature_time: str) -> Optional[str]:
        """Which split a timestamp falls in, or None if outside them."""
        if self.train_start <= feature_time <= self.train_end:
            return TRAIN
        if (self.validation_start and self.validation_end
                and self.validation_start <= feature_time
                <= self.validation_end):
            return VALIDATION
        if (self.holdout_start and self.holdout_end
                and self.holdout_start <= feature_time
                <= self.holdout_end):
            return HOLDOUT
        return None


def chronological_split(feature_times: Sequence[str],
                        fractions: Tuple[float, float, float]
                        = DEFAULT_FRACTIONS) -> Optional[ChronologicalSplit]:
    """Split a sorted timeline into train, validation and holdout.

    Returns None when there are too few rows for three non-empty
    splits. None rather than a degenerate split with an empty holdout:
    a holdout of zero rows reports no error and would read as a perfect
    score.
    """
    times = sorted(set(feature_times))
    n = len(times)
    if n < 30:
        return None
    train_n = int(n * fractions[0])
    val_n = int(n * fractions[1])
    if train_n < 10 or val_n < 5 or (n - train_n - val_n) < 5:
        return None
    train = times[:train_n]
    validation = times[train_n:train_n + val_n]
    holdout = times[train_n + val_n:]
    return ChronologicalSplit(
        train_start=train[0], train_end=train[-1],
        validation_start=validation[0] if validation else None,
        validation_end=validation[-1] if validation else None,
        holdout_start=holdout[0] if holdout else None,
        holdout_end=holdout[-1] if holdout else None,
        train_rows=len(train), validation_rows=len(validation),
        holdout_rows=len(holdout), shuffled=False,
        note=("boundaries are stored exactly; fractions reproduce only "
              "against an identical row count"))


def class_prior(labels: Sequence[str]) -> Dict:
    """The class distribution of the TRAINING labels.

    This is the bar a classifier must clear, and measuring it is the
    only way to know what that bar is. An assumed 0.50 for a
    three-class target is simply wrong: on real data the direction
    target was UP 0.4472, DOWN 0.3928, FLAT 0.1600, so a two-class coin
    flip expects 0.4200 and "always UP" expects 0.4472.
    """
    counts: Dict[str, int] = {}
    for label in labels:
        if label is None:
            continue
        counts[str(label)] = counts.get(str(label), 0) + 1
    total = sum(counts.values())
    if total == 0:
        return {"counts": {}, "distribution": {}, "majority": None,
                "total": 0,
                "note": "no labels supplied; there is no prior to report"}
    distribution = {k: round(v / total, 6) for k, v in counts.items()}
    majority = max(counts.items(), key=lambda kv: kv[1])[0]
    return {
        "counts": counts,
        "distribution": distribution,
        "majority": majority,
        "majority_rate": distribution[majority],
        "total": total,
        # What each trivial baseline is EXPECTED to score, so a model's
        # number can be read against the right bar rather than 0.50.
        "expected_accuracy": {
            "ALWAYS_" + majority: distribution[majority],
            "UNIFORM_TWO_CLASS_FLIP": round(
                sum(v for k, v in distribution.items()
                    if k in ("UP", "DOWN")) / 2.0, 6),
            "RANDOM_CLASS_PRIOR": round(
                sum(v * v for v in distribution.values()), 6),
        },
        "note": ("priors are computed on TRAINING labels only; using all "
                 "labels would leak the holdout's class balance into "
                 "every baseline"),
    }


@dataclass(frozen=True)
class Scaler:
    """Per-feature mean and standard deviation, fitted on TRAIN only.

    Frozen, and carries the row count it was fitted on so a later
    reader can tell whether it was fitted on a sample worth trusting.
    """
    names: Tuple[str, ...]
    means: Tuple[float, ...]
    stdevs: Tuple[float, ...]
    fitted_rows: int
    fitted_on: str = TRAIN

    def transform(self, vector: Sequence[float]) -> Optional[List[float]]:
        """Scale one vector, or None if it is the wrong shape.

        None rather than a padded or truncated vector: a silently
        reshaped row would be scored against the wrong coefficients.
        """
        if len(vector) != len(self.names):
            return None
        out = []
        for value, mean, sd in zip(vector, self.means, self.stdevs):
            # A zero-variance feature contributes nothing and must not
            # divide. 0.0 is the correct scaled value for "this feature
            # was constant in training".
            out.append(0.0 if sd == 0 else (value - mean) / sd)
        return out

    def to_dict(self) -> Dict:
        return {"names": list(self.names), "means": list(self.means),
                "stdevs": list(self.stdevs), "fitted_rows": self.fitted_rows,
                "fitted_on": self.fitted_on}


def fit_scaler(vectors: Sequence[Sequence[float]],
               names: Sequence[str]) -> Optional[Scaler]:
    """Fit on TRAINING vectors only.

    The caller is responsible for passing training rows. That cannot be
    enforced from here, so `fitted_on` records the claim and
    tests/test_prediction_splits.py proves that a future extreme value
    cannot change the fitted statistics.
    """
    if not vectors:
        return None
    width = len(names)
    rows = [v for v in vectors if len(v) == width]
    if not rows:
        return None
    means, stdevs = [], []
    for i in range(width):
        column = [row[i] for row in rows]
        mean = sum(column) / len(column)
        if len(column) < 2:
            sd = 0.0
        else:
            sd = (sum((c - mean) ** 2 for c in column)
                  / (len(column) - 1)) ** 0.5
        means.append(mean)
        stdevs.append(sd)
    return Scaler(names=tuple(names), means=tuple(means),
                  stdevs=tuple(stdevs), fitted_rows=len(rows))


def dataset_fingerprint(feature_hashes: Sequence[str]) -> str:
    """An identity for a training set, for the model artifact.

    Over the feature hashes rather than the raw rows, so it changes if
    any input changed and stays stable if only the order did.
    """
    blob = json.dumps(sorted(feature_hashes), separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]
