"""LOGISTIC_DIRECTION and RIDGE_FORWARD_RETURN.

Pure Python. No numpy, no scikit-learn, no LLM. The models are
deliberately simple: the question is whether the FEATURES carry
information, and a complex model answers a different question - whether
a complex model can find something - while making leakage much harder
to rule out.

WHAT IS FITTED, AND ON WHAT

Only training rows. The scaler, the class prior and the coefficients
all come from the training split, and the artifact records the split
boundaries so a reader can check. Fitting a scaler across
train+validation+holdout leaks the future into every feature and the
leak is invisible - the numbers simply come out better.

TRANSACTION COSTS ARE NOT MODELLED HERE

These predict direction and return, not profit. Any claim about value
has to go through the counterfactual replay, which pays spread and
slippage. A regression that predicts +0.05% is not an edge once a
0.05% spread is paid, and reporting it as one would be the oldest
mistake in the subject.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .features import NUMERIC_FEATURE_NAMES
from .models import (DOWN, LOGISTIC_DIRECTION, RIDGE_FORWARD_RETURN, UP)
from .splits import Scaler, dataset_fingerprint, fit_scaler

MODEL_IMPL_VERSION = "statistical-v1.0.0"

# Training limits. Chosen to converge on this data and stated rather
# than tuned against a holdout.
# 300 did not converge on real data and 2000 did not either; 8000 does.
# A non-converged fit still returns coefficients, so the only way to
# notice was to check `converged` - which is why it is on the artifact
# and reported rather than assumed.
DEFAULT_EPOCHS = 8000
DEFAULT_LEARNING_RATE = 0.05
DEFAULT_L2 = 1.0

# Below this many training rows, a fit is not attempted. A model fitted
# on a handful of rows produces coefficients and no information, and
# returning one invites it to be read as a result.
MIN_TRAIN_ROWS = 100


@dataclass(frozen=True)
class ModelArtifact:
    """A trained model, identified completely enough to be re-derived.

    Frozen, and the identity includes the training window and dataset
    fingerprint. A retrained model is a NEW artifact with a new version
    - overwriting one under the same identity would silently invalidate
    every result already attributed to it.
    """
    model_name: str
    model_version: str
    impl_version: str
    trained_at: str
    training_dataset_hash: str
    training_start: str
    training_end: str
    training_rows: int
    feature_schema_version: str
    feature_names: Tuple[str, ...]
    hyperparameters: Dict[str, Any]
    code_sha: Optional[str]
    coefficients: Tuple[float, ...] = ()
    intercept: float = 0.0
    scaler: Optional[Scaler] = None
    class_prior: Optional[Dict] = None
    converged: bool = False
    note: Optional[str] = None

    @property
    def artifact_id(self) -> str:
        blob = (f"{self.model_name}:{self.model_version}:"
                f"{self.training_dataset_hash}:{self.training_start}:"
                f"{self.training_end}")
        return "mdl_" + hashlib.sha256(blob.encode()).hexdigest()[:20]

    def to_dict(self) -> Dict:
        return {
            "artifact_id": self.artifact_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "impl_version": self.impl_version,
            "trained_at": self.trained_at,
            "training_dataset_hash": self.training_dataset_hash,
            "training_period": {"start": self.training_start,
                                "end": self.training_end,
                                "rows": self.training_rows},
            "feature_schema_version": self.feature_schema_version,
            "feature_names": list(self.feature_names),
            "hyperparameters": dict(self.hyperparameters),
            "code_sha": self.code_sha,
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
            "scaler": self.scaler.to_dict() if self.scaler else None,
            "class_prior": self.class_prior,
            "converged": self.converged,
            "note": self.note,
        }


def _sigmoid(z: float) -> float:
    # Split to avoid overflow on large negative z, which would raise
    # rather than saturate.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def train_logistic(vectors: Sequence[Sequence[float]],
                   labels: Sequence[str], *,
                   feature_names: Sequence[str] = NUMERIC_FEATURE_NAMES,
                   training_start: str, training_end: str,
                   trained_at: str, dataset_hash: Optional[str] = None,
                   feature_schema_version: str = "unknown",
                   code_sha: Optional[str] = None,
                   epochs: int = DEFAULT_EPOCHS,
                   learning_rate: float = DEFAULT_LEARNING_RATE,
                   l2: float = DEFAULT_L2,
                   scaler: Optional[Scaler] = None
                   ) -> Optional[ModelArtifact]:
    """Binary logistic regression on UP versus DOWN.

    FLAT rows are EXCLUDED from training, not folded into one of the
    other classes. Calling a flat outcome "up" teaches the model that
    no movement is a rise, and the exclusion count is reported so the
    sample is not silently smaller than it looks.

    Returns None rather than a weak artifact when there is too little
    data: coefficients fitted on fifty rows are numbers, not a model.
    """
    pairs = [(v, lab) for v, lab in zip(vectors, labels)
             if lab in (UP, DOWN) and len(v) == len(feature_names)]
    excluded = len(vectors) - len(pairs)
    if len(pairs) < MIN_TRAIN_ROWS:
        return None

    scaler = scaler or fit_scaler([v for v, _ in pairs], feature_names)
    if scaler is None:
        return None
    rows = []
    for v, lab in pairs:
        scaled = scaler.transform(v)
        if scaled is None:
            continue
        rows.append((scaled, 1.0 if lab == UP else 0.0))
    if len(rows) < MIN_TRAIN_ROWS:
        return None

    width = len(feature_names)
    weights = [0.0] * width
    intercept = 0.0
    n = float(len(rows))
    last_loss, converged = None, False
    for _ in range(epochs):
        gw = [0.0] * width
        gi = 0.0
        loss = 0.0
        for scaled, y in rows:
            z = intercept + sum(w * x for w, x in zip(weights, scaled))
            p = _sigmoid(z)
            err = p - y
            gi += err
            for i, x in enumerate(scaled):
                gw[i] += err * x
            eps = 1e-15
            loss += -(y * math.log(max(p, eps))
                      + (1 - y) * math.log(max(1 - p, eps)))
        loss /= n
        intercept -= learning_rate * (gi / n)
        for i in range(width):
            # L2 on the weights only. Penalising the intercept would
            # bias the model away from the class prior, which is the one
            # thing it should be allowed to learn for free.
            weights[i] -= learning_rate * ((gw[i] / n)
                                           + (l2 / n) * weights[i])
        if last_loss is not None and abs(last_loss - loss) < 1e-7:
            converged = True
            break
        last_loss = loss

    up = sum(1 for _, y in rows if y == 1.0)
    return ModelArtifact(
        model_name=LOGISTIC_DIRECTION,
        model_version=f"{MODEL_IMPL_VERSION}+l2={l2}",
        impl_version=MODEL_IMPL_VERSION,
        trained_at=trained_at,
        training_dataset_hash=dataset_hash or "unknown",
        training_start=training_start, training_end=training_end,
        training_rows=len(rows),
        feature_schema_version=feature_schema_version,
        feature_names=tuple(feature_names),
        hyperparameters={"epochs": epochs, "learning_rate": learning_rate,
                         "l2": l2, "flat_rows_excluded": excluded},
        code_sha=code_sha, coefficients=tuple(weights),
        intercept=intercept, scaler=scaler,
        class_prior={"UP": round(up / len(rows), 6),
                     "DOWN": round(1 - up / len(rows), 6)},
        converged=converged,
        note=("binary UP-vs-DOWN; FLAT rows excluded from training "
              "rather than folded into a direction. A probability near "
              "the training class prior means the features added "
              "nothing, which is a result and not a bug."))


def predict_logistic(artifact: ModelArtifact,
                     vector: Sequence[float]) -> Optional[Tuple[str, float]]:
    """(direction, probability of UP), or None if unusable.

    The probability is the model's own output. It is NOT called
    confidence: that word implies calibrated semantics, and calibration
    is measured separately and may well be poor.
    """
    if artifact.scaler is None:
        return None
    scaled = artifact.scaler.transform(vector)
    if scaled is None:
        return None
    z = artifact.intercept + sum(
        w * x for w, x in zip(artifact.coefficients, scaled))
    p_up = _sigmoid(z)
    return (UP if p_up >= 0.5 else DOWN), round(p_up, 6)


def train_ridge(vectors: Sequence[Sequence[float]],
                values: Sequence[float], *,
                feature_names: Sequence[str] = NUMERIC_FEATURE_NAMES,
                training_start: str, training_end: str, trained_at: str,
                dataset_hash: Optional[str] = None,
                feature_schema_version: str = "unknown",
                code_sha: Optional[str] = None,
                l2: float = DEFAULT_L2,
                scaler: Optional[Scaler] = None
                ) -> Optional[ModelArtifact]:
    """Ridge regression on the forward return, by normal equations.

    Closed form rather than gradient descent: with sixteen features the
    matrix is tiny, and an exact solution removes convergence as a
    source of doubt about the result.
    """
    pairs = [(v, float(y)) for v, y in zip(vectors, values)
             if y is not None and len(v) == len(feature_names)]
    if len(pairs) < MIN_TRAIN_ROWS:
        return None
    scaler = scaler or fit_scaler([v for v, _ in pairs], feature_names)
    if scaler is None:
        return None
    rows = []
    for v, y in pairs:
        scaled = scaler.transform(v)
        if scaled is not None:
            rows.append(([1.0] + scaled, y))
    if len(rows) < MIN_TRAIN_ROWS:
        return None

    width = len(feature_names) + 1          # + intercept column
    # X'X + lambda*I, with the intercept NOT penalised.
    xtx = [[0.0] * width for _ in range(width)]
    xty = [0.0] * width
    for x, y in rows:
        for i in range(width):
            xty[i] += x[i] * y
            for j in range(width):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, width):
        xtx[i][i] += l2

    solution = _solve(xtx, xty)
    if solution is None:
        # Singular even with the ridge term. None rather than a
        # pseudo-inverse: an unsolvable system is a fact about the
        # features, and papering over it hides collinearity.
        return None

    return ModelArtifact(
        model_name=RIDGE_FORWARD_RETURN,
        model_version=f"{MODEL_IMPL_VERSION}+l2={l2}",
        impl_version=MODEL_IMPL_VERSION,
        trained_at=trained_at,
        training_dataset_hash=dataset_hash or "unknown",
        training_start=training_start, training_end=training_end,
        training_rows=len(rows),
        feature_schema_version=feature_schema_version,
        feature_names=tuple(feature_names),
        hyperparameters={"l2": l2, "solver": "normal_equations"},
        code_sha=code_sha,
        coefficients=tuple(solution[1:]), intercept=solution[0],
        scaler=scaler, converged=True,
        note=("predicts the forward return in PERCENT. It does not "
              "predict profit: a +0.05% forecast is not an edge once a "
              "0.05% spread is paid, so value must be measured through "
              "the counterfactual replay."))


def _solve(matrix: List[List[float]],
           rhs: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting, or None if singular."""
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(col + 1, n):
            factor = a[row][col] / a[col][col]
            for k in range(col, n + 1):
                a[row][k] -= factor * a[col][k]
    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = a[row][n] - sum(a[row][k] * out[k] for k in range(row + 1, n))
        out[row] = total / a[row][row]
    return out


def predict_ridge(artifact: ModelArtifact,
                  vector: Sequence[float]) -> Optional[float]:
    if artifact.scaler is None:
        return None
    scaled = artifact.scaler.transform(vector)
    if scaled is None:
        return None
    return round(artifact.intercept + sum(
        w * x for w, x in zip(artifact.coefficients, scaled)), 6)
