"""Prediction records, outcomes, and the baseline predictors.

RESEARCH ONLY. Nothing here can place an order: the package may not
import the broker, risk, hypothesis, position, scanner or orchestration
modules, and a test reads the source to prove it.

TWO RECORDS, NOT ONE

A PredictionRecord is what was claimed, and it is frozen. A
PredictionOutcome is what happened, and it arrives later. They are
separate objects because a single mutable row would let the claim be
edited once the answer was known - which is the only way a prediction
system can lie to itself.

DETERMINISTIC MODELS HAVE NO PROBABILITY

A rule that says UP says UP. Attaching "58%" to it would be inventing a
number that no part of the rule computed, and it would then be charted
beside a logistic model's real 58% as though they meant the same thing.
`probability` is None for every deterministic baseline, and the
calibration metrics skip them rather than scoring a fabrication.

NO_SKILL IS A FIRST-CLASS MODEL

Without it there is nothing to beat. It is seeded per feature hash so
it is reproducible: an unseeded coin flip would give a different answer
every time the same prediction was recomputed, and "better than random"
would be unfalsifiable.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .features import FeatureRecord
from .targets import (DIRECTION, DOWN, FLAT, FORWARD_RETURN,
                      HORIZONS_MINUTES, UP)

MODEL_SCHEMA_VERSION = "models-v1.0.0"

# Model identities. Strings, because they are stored and compared across
# process boundaries and a renamed enum member would stop matching
# historical rows.
NO_SKILL = "NO_SKILL"
# Dataset-aware baselines.
#
# NO_SKILL alone is the WRONG bar, and measurement proved it. The
# direction target has three classes - on 3,312 resolved AAPL-and-peers
# targets: UP 0.4472, DOWN 0.3928, FLAT 0.1600 - while NO_SKILL only
# ever says UP or DOWN. A uniform two-class flip on a three-class target
# expects (P(UP)+P(DOWN))/2 = 0.4200, and NO_SKILL measured 0.4182. It
# was behaving correctly; 0.50 was never its expectation.
#
# ALWAYS_UP scores 0.4472 on the same data, which beats every model
# tried so far. A model that cannot beat "always say UP" has not
# demonstrated anything, and against an assumed 0.50 it would have
# looked like a near miss instead.
MAJORITY_CLASS = "MAJORITY_CLASS"
RANDOM_CLASS_PRIOR = "RANDOM_CLASS_PRIOR"
ALWAYS_UP = "ALWAYS_UP"
ALWAYS_DOWN = "ALWAYS_DOWN"
PREVIOUS_RETURN_SIGN = "PREVIOUS_RETURN_SIGN"
MOMENTUM = "MOMENTUM"
MEAN_REVERSION = "MEAN_REVERSION"
CURRENT_SIGNAL_ENGINE = "CURRENT_SIGNAL_ENGINE"
LOGISTIC_DIRECTION = "LOGISTIC_DIRECTION"
RIDGE_FORWARD_RETURN = "RIDGE_FORWARD_RETURN"

DETERMINISTIC_MODELS = (NO_SKILL, PREVIOUS_RETURN_SIGN, MOMENTUM,
                        MEAN_REVERSION, CURRENT_SIGNAL_ENGINE,
                        MAJORITY_CLASS, RANDOM_CLASS_PRIOR, ALWAYS_UP,
                        ALWAYS_DOWN)
# The bar a model must clear. Reported together, because clearing the
# coin flip while losing to "always say UP" is not an edge.
REFERENCE_BASELINES = (NO_SKILL, MAJORITY_CLASS, RANDOM_CLASS_PRIOR,
                       ALWAYS_UP, ALWAYS_DOWN)
PROBABILISTIC_MODELS = (LOGISTIC_DIRECTION,)
REGRESSION_MODELS = (RIDGE_FORWARD_RETURN,)
BASELINE_MODELS = DETERMINISTIC_MODELS

# Prediction lifecycle.
PENDING = "PENDING"
MATURED = "MATURED"
VOID = "VOID"
NOT_GENERATED = "NOT_GENERATED"

# A model that cannot produce an answer says ABSTAIN. Not FLAT: "I have
# no view" and "I expect no movement" are different claims, and
# scoring an abstention as a flat call would credit or blame a model
# for a prediction it declined to make.
ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class PredictionRecord:
    """An immutable claim about the future.

    Frozen on purpose. Once an outcome is known, a mutable record could
    be adjusted to match it, and no later metric would reveal that.
    """
    prediction_id: str
    model: str
    model_version: str
    symbol: str
    target: str
    horizon_minutes: int
    prediction: Any
    probability: Optional[float]
    prediction_feature_id: str
    feature_hash: str
    feature_time: str
    feature_schema_version: str
    generated_at: str
    dataset_id: Optional[str] = None
    dataset_checksum: Optional[str] = None
    regime_source: str = "NONE"
    regime_state: Optional[str] = None
    training_window: Optional[Dict] = None
    mode: str = "RESEARCH_ONLY"
    detail: Optional[str] = None

    @property
    def is_abstention(self) -> bool:
        return self.prediction == ABSTAIN

    @property
    def deterministic(self) -> bool:
        return self.model in DETERMINISTIC_MODELS

    def to_dict(self) -> Dict:
        return {
            "prediction_id": self.prediction_id,
            "model": self.model,
            "model_version": self.model_version,
            "symbol": self.symbol,
            "target": self.target,
            "horizon_minutes": self.horizon_minutes,
            "prediction": self.prediction,
            "probability": self.probability,
            "deterministic": self.deterministic,
            "abstained": self.is_abstention,
            "prediction_feature_id": self.prediction_feature_id,
            "feature_hash": self.feature_hash,
            "feature_time": self.feature_time,
            "feature_schema_version": self.feature_schema_version,
            "generated_at": self.generated_at,
            "dataset_id": self.dataset_id,
            "dataset_checksum": self.dataset_checksum,
            "regime_source": self.regime_source,
            "regime_state": self.regime_state,
            "training_window": self.training_window,
            "mode": self.mode,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PredictionOutcome:
    """What actually happened. Written once the horizon has elapsed.

    Carries `prediction_id` rather than embedding the prediction, so the
    original claim is referenced and never rewritten.
    """
    prediction_id: str
    status: str
    evaluated_at: Optional[str] = None
    realized_return_pct: Optional[float] = None
    realized_direction: Optional[str] = None
    max_favorable_excursion_pct: Optional[float] = None
    max_adverse_excursion_pct: Optional[float] = None
    stop_target_outcome: Optional[str] = None
    correct: Optional[bool] = None
    error: Optional[float] = None
    void_reason: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "prediction_id": self.prediction_id,
            "status": self.status,
            "evaluated_at": self.evaluated_at,
            "realized_return_pct": self.realized_return_pct,
            "realized_direction": self.realized_direction,
            "max_favorable_excursion_pct":
                self.max_favorable_excursion_pct,
            "max_adverse_excursion_pct": self.max_adverse_excursion_pct,
            "stop_target_outcome": self.stop_target_outcome,
            "correct": self.correct,
            "error": self.error,
            "void_reason": self.void_reason,
        }


def _prediction_id(model: str, feature_hash: str, target: str,
                   horizon: int) -> str:
    """Deterministic, so the same claim cannot be recorded twice.

    Derived from the model, the exact feature vector, the target and the
    horizon. Re-running generation over the same history produces the
    same ids rather than a second set of apparently independent
    predictions - which would double every sample count.
    """
    blob = f"{model}:{feature_hash}:{target}:{horizon}"
    return "pred_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


# ------------------------------------------------------------ baselines
#
# Each returns (prediction, probability, detail). `probability` is None
# for every deterministic rule.

def _no_skill(record: FeatureRecord, horizon: int):
    """A reproducible coin flip. The thing every model must beat.

    Seeded from the feature hash and the horizon, so the same input
    always gives the same answer. An unseeded flip would differ on every
    recomputation and "better than random" would be unfalsifiable.
    """
    seed = int(hashlib.sha256(
        f"{record.feature_hash}:{horizon}".encode()).hexdigest()[:8], 16)
    return (UP if seed % 2 == 0 else DOWN), None, "seeded coin flip"


def _previous_return_sign(record: FeatureRecord, horizon: int):
    """Tomorrow looks like yesterday."""
    prior = record.values.get("return_1b_pct")
    if prior is None:
        return ABSTAIN, None, "return_1b_pct unavailable"
    if prior == 0:
        # A genuinely unchanged bar gives this rule no sign to copy.
        # FLAT is the honest answer, not an arbitrary UP.
        return FLAT, None, "previous bar was unchanged"
    return (UP if prior > 0 else DOWN), None, f"previous return {prior}%"


def _momentum(record: FeatureRecord, horizon: int):
    """Trend continues: price above its averages keeps rising.

    Uses the 10-vs-50 relationship AND price's position against the 20,
    and ABSTAINS when they disagree. Picking one and ignoring the other
    would manufacture a confident call out of a contradiction.
    """
    fast_slow = record.values.get("sma_10_vs_50_pct")
    price_vs_20 = record.values.get("price_vs_sma_20_pct")
    if fast_slow is None or price_vs_20 is None:
        return ABSTAIN, None, "moving averages unavailable"
    up = fast_slow > 0 and price_vs_20 > 0
    down = fast_slow < 0 and price_vs_20 < 0
    if up:
        return UP, None, f"sma10>sma50 by {fast_slow}%, price above sma20"
    if down:
        return DOWN, None, f"sma10<sma50 by {fast_slow}%, price below sma20"
    return ABSTAIN, None, "trend indicators disagree"


def _mean_reversion(record: FeatureRecord, horizon: int):
    """Extremes revert. Deliberately the opposite of momentum.

    Kept as its own model rather than a negated momentum, because the
    two use different inputs - RSI and band position, not moving
    averages - and a negation would only ever be momentum's mirror.
    """
    rsi = record.values.get("rsi_14")
    band = record.values.get("bollinger_position")
    if rsi is None:
        return ABSTAIN, None, "rsi_14 unavailable"
    if rsi >= 70 or (band is not None and band >= 1.0):
        return DOWN, None, f"overbought: rsi {rsi}, band {band}"
    if rsi <= 30 or (band is not None and band <= 0.0):
        return UP, None, f"oversold: rsi {rsi}, band {band}"
    return ABSTAIN, None, f"rsi {rsi} is not extreme"


def _current_signal_engine(record: FeatureRecord, horizon: int):
    """What the live signal engine already said, as a prediction.

    The honest baseline for "does the existing system predict anything".
    Read from the feature record, which the caller populated as-of
    feature_time - this module does not call the signal engine, because
    importing it would put a research module on the decision path.

    signal_agreement is NOT used as a probability. Agreement between
    independent groups is not a calibrated likelihood of being right,
    and presenting it as one beside a logistic model's output would be a
    category error.
    """
    direction = record.values.get("signal_direction")
    if direction is None:
        return ABSTAIN, None, "no signal direction in the feature record"
    mapping = {"BUY": UP, "SELL": DOWN, "NEUTRAL": FLAT,
               "UP": UP, "DOWN": DOWN, "FLAT": FLAT}
    mapped = mapping.get(str(direction).upper())
    if mapped is None:
        return ABSTAIN, None, f"unmapped signal direction {direction!r}"
    agreement = record.values.get("signal_agreement")
    return mapped, None, (f"signal engine said {direction}"
                          + (f" (agreement {agreement}, NOT a probability)"
                             if agreement is not None else ""))


def _always(label: str):
    """A constant predictor. Trivial, and the hardest bar to clear.

    Reported because a constant can beat every rule in a market with a
    directional drift, and a model that loses to one has measured the
    drift rather than learned anything.
    """
    def fn(record: FeatureRecord, horizon: int):              # noqa: ARG001
        return label, None, f"always predicts {label}"
    return fn


def _majority_class(record: FeatureRecord, horizon: int):
    """The most frequent class IN THE TRAINING DATA.

    Not computed here: this module has no training set, and inferring
    "majority" from the single record in front of it would be a
    different and meaningless predictor. The class prior is supplied by
    the caller through the feature record so the choice stays
    attributable, and the model ABSTAINS rather than guessing when it
    is absent.
    """
    prior = record.values.get("class_prior_majority")
    if prior is None:
        return (ABSTAIN, None,
                "no class prior supplied; the majority class is a "
                "property of the training set, not of one observation")
    return str(prior), None, f"training-set majority class is {prior}"


def _random_class_prior(record: FeatureRecord, horizon: int):
    """A draw from the training class distribution, seeded.

    Harder to beat than a uniform flip, because it already knows how
    often each class occurs. Seeded from the feature hash for the same
    reason NO_SKILL is: an unseeded draw makes "better than chance"
    unfalsifiable.
    """
    prior = record.values.get("class_prior_distribution")
    if not isinstance(prior, dict) or not prior:
        return (ABSTAIN, None,
                "no class prior distribution supplied")
    seed = int(hashlib.sha256(
        f"prior:{record.feature_hash}:{horizon}".encode()
    ).hexdigest()[:8], 16)
    # Deterministic inverse-CDF draw over a SORTED distribution, so the
    # same prior always yields the same class for the same input.
    total = sum(float(v) for v in prior.values())
    if total <= 0:
        return ABSTAIN, None, "class prior sums to zero"
    point = (seed % 10_000_000) / 10_000_000.0 * total
    running = 0.0
    for label in sorted(prior):
        running += float(prior[label])
        if point <= running:
            return label, None, f"drawn from prior {prior}"
    return sorted(prior)[-1], None, f"drawn from prior {prior}"


BASELINE_FUNCTIONS = {
    MAJORITY_CLASS: _majority_class,
    RANDOM_CLASS_PRIOR: _random_class_prior,
    ALWAYS_UP: _always(UP),
    ALWAYS_DOWN: _always(DOWN),
    NO_SKILL: _no_skill,
    PREVIOUS_RETURN_SIGN: _previous_return_sign,
    MOMENTUM: _momentum,
    MEAN_REVERSION: _mean_reversion,
    CURRENT_SIGNAL_ENGINE: _current_signal_engine,
}


def predict_baseline(model: str, record: FeatureRecord, *,
                     horizon_minutes: int, generated_at: str,
                     target: str = DIRECTION) -> PredictionRecord:
    """One baseline prediction. Raises for an unknown model.

    Raises rather than abstaining: an unknown model name is a
    programming error, and silently recording an abstention would hide
    a typo as a model with no opinions.
    """
    fn = BASELINE_FUNCTIONS.get(model)
    if fn is None:
        raise ValueError(
            f"unknown baseline model {model!r}; known: "
            f"{sorted(BASELINE_FUNCTIONS)}")
    prediction, probability, detail = fn(record, horizon_minutes)
    if probability is not None and model in DETERMINISTIC_MODELS:
        # Defence against a future edit: a deterministic rule computed
        # no likelihood, and a number here would be charted beside a
        # real one.
        raise AssertionError(
            f"{model} is deterministic and must not carry a probability")
    return PredictionRecord(
        prediction_id=_prediction_id(model, record.feature_hash, target,
                                     horizon_minutes),
        model=model,
        model_version=MODEL_SCHEMA_VERSION,
        symbol=record.symbol,
        target=target,
        horizon_minutes=horizon_minutes,
        prediction=prediction,
        probability=probability,
        prediction_feature_id=record.prediction_feature_id,
        feature_hash=record.feature_hash,
        feature_time=record.feature_time,
        feature_schema_version=record.feature_schema_version,
        generated_at=generated_at,
        dataset_id=record.dataset_id,
        dataset_checksum=record.dataset_checksum,
        regime_source=record.regime_source,
        regime_state=record.regime_state,
        detail=detail,
    )


def predict_all_baselines(record: FeatureRecord, *, generated_at: str,
                          horizons=HORIZONS_MINUTES,
                          models=BASELINE_MODELS
                          ) -> List[PredictionRecord]:
    """Every baseline at every horizon, abstentions included.

    Abstentions are RETURNED rather than dropped. A model that declined
    is different from a model nobody asked, and a list that omits them
    makes a selective predictor look like a complete one.
    """
    out: List[PredictionRecord] = []
    for model in models:
        for horizon in horizons:
            out.append(predict_baseline(model, record,
                                        horizon_minutes=horizon,
                                        generated_at=generated_at))
    return out
