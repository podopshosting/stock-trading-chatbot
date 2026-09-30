"""
Canonical quantitative signal types.

Every indicator in this system returns the same shape. Downstream code -
the review UX, the future Evidence Engine, the future Trade Hypothesis
Engine - must never need to understand an indicator-specific payload,
because the moment it does, adding an indicator means editing every
consumer.

Three things are kept deliberately separate, and must not be collapsed
into a single "confidence" number:

  direction   BUY / SELL / NEUTRAL / NO_SIGNAL - which way the evidence
              points, if anywhere.

  agreement   how independently aligned the correlation groups are. A
              function of structure, not of magnitude.

  magnitude   how far the raw indicator sits beyond its own neutral
              threshold, normalised so a $1 move on a $10 stock and on a
              $1,000 stock are not treated alike.

A result can be BUY with high agreement and weak magnitude (three groups
all barely past their thresholds), or BUY with moderate agreement and
strong magnitude (one group, but emphatically). Those are different
pieces of evidence and a single percentage cannot express either.

NEUTRAL and NO_SIGNAL are also different claims:

  NEUTRAL     the indicator computed and reports no direction. RSI 52 is
              a measurement.
  NO_SIGNAL   the indicator could not compute - not enough history, or
              the data was unusable. It is an absence of evidence, not
              evidence of balance.

Counting a NO_SIGNAL as a neutral vote would let missing data look like
considered agnosticism.
"""
from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class SignalDirection(str, enum.Enum):
    """Directional vocabulary.

    STRONG_BUY / STRONG_SELL are deliberately absent. The engine has no
    evidence basis for a fifth and sixth level: strength is measured
    separately and continuously, so baking two more discrete levels into
    the direction would invent granularity the data does not support.
    """
    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"
    NO_SIGNAL = "NO_SIGNAL"

    def __str__(self) -> str:
        return self.value

    @property
    def is_directional(self) -> bool:
        return self in (SignalDirection.BUY, SignalDirection.SELL)

    @property
    def vote(self) -> int:
        return {"BUY": 1, "SELL": -1}.get(self.value, 0)


class GroupDirection(str, enum.Enum):
    """A group's net reading.

    MIXED is distinct from NEUTRAL: MIXED means the group's own members
    contradict each other, which is information. NEUTRAL means the
    members agreed there is no direction.
    """
    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"
    NO_SIGNAL = "NO_SIGNAL"

    def __str__(self) -> str:
        return self.value

    @property
    def is_directional(self) -> bool:
        return self in (GroupDirection.BUY, GroupDirection.SELL)


class CorrelationGroup(str, enum.Enum):
    """Indicators that measure substantially the same thing.

    Membership was set from measured pairwise correlation of directional
    votes over random-walk series, not from intuition. See
    docs/QUANTITATIVE-SIGNAL-ENGINE.md.
    """
    TREND = "trend"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"

    def __str__(self) -> str:
        return self.value


class DataFreshness(str, enum.Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


class StrengthBand(str, enum.Enum):
    """Words for a continuous magnitude, for display only.

    The number is the source of truth; the band exists because "0.41" is
    not something a reader can act on.
    """
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    MIXED = "MIXED"
    NONE = "NONE"

    def __str__(self) -> str:
        return self.value


# Which group each indicator belongs to. Single source of truth: the
# engine reads membership from here, so an indicator cannot be added
# without a group being chosen for it.
INDICATOR_GROUPS: Dict[str, CorrelationGroup] = {
    "ma_crossover": CorrelationGroup.TREND,
    "golden_cross": CorrelationGroup.TREND,
    "rsi": CorrelationGroup.MOMENTUM,
    "macd": CorrelationGroup.MOMENTUM,
    "momentum_10d": CorrelationGroup.MOMENTUM,
    "bollinger": CorrelationGroup.MEAN_REVERSION,
}

INDICATOR_LABELS: Dict[str, str] = {
    "ma_crossover": "MA crossover (20/50)",
    "golden_cross": "Golden/death cross (50/200)",
    "rsi": "RSI (14)",
    "macd": "MACD (12/26/9)",
    "momentum_10d": "10-day momentum",
    "bollinger": "Bollinger Bands (20, 2σ)",
}

GROUP_LABELS: Dict[str, str] = {
    "trend": "Trend",
    "momentum": "Momentum",
    "mean_reversion": "Mean Reversion",
}

GROUP_DESCRIPTIONS: Dict[str, str] = {
    "trend": "Where price sits relative to its moving averages",
    "momentum": "Rate and direction of recent price change",
    "mean_reversion": "Whether price is stretched from its recent range",
}

ALL_GROUPS = (CorrelationGroup.TREND, CorrelationGroup.MOMENTUM,
              CorrelationGroup.MEAN_REVERSION)

AGREEMENT_MEANING = (
    "Signal agreement reflects how strongly independent analysis groups "
    "agree with each other. It is NOT the probability of a price move, "
    "not a forecast, and not the chance of a profitable trade."
)

MAGNITUDE_MEANING = (
    "Signal magnitude is how far the underlying indicators sit beyond "
    "their own neutral thresholds, normalised for the security's own "
    "volatility. It says how emphatic the reading is, not how likely it "
    "is to be right."
)

DISCLAIMER = (
    "This is technical analysis of past price data, not investment "
    "advice. It does not predict future prices. No orders can be placed "
    "from this system."
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


@dataclass
class SignalResult:
    """One indicator's normalised opinion.

    `raw_values` carries the numbers the reading was derived from, so a
    consumer can show the arithmetic instead of asking the reader to
    trust it. `thresholds` carries the cutoffs that were applied, so a
    reading can be re-derived from the record later even if the rule
    changes.
    """
    indicator: str
    group: CorrelationGroup
    direction: SignalDirection
    strength: float = 0.0                       # [0, 1]; 0 when not directional
    raw_values: Dict[str, Optional[float]] = field(default_factory=dict)
    thresholds: Dict[str, Optional[float]] = field(default_factory=dict)
    reason: str = ""
    freshness: DataFreshness = DataFreshness.UNKNOWN
    timestamp: str = field(default_factory=utcnow)
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.direction.is_directional and self.strength:
            # A non-directional signal with a magnitude would be read as
            # "weakly bullish" by any consumer that sorted on strength.
            self.strength = 0.0
        self.strength = max(0.0, min(1.0, float(self.strength or 0.0)))

    @property
    def label(self) -> str:
        return INDICATOR_LABELS.get(self.indicator, self.indicator)

    @property
    def counted(self) -> bool:
        """Whether this contributes a directional vote to its group."""
        return self.direction.is_directional

    def as_dict(self) -> Dict:
        return {
            "indicator": self.indicator,
            "label": self.label,
            "group": str(self.group),
            "direction": str(self.direction),
            "strength": _round(self.strength, 4),
            "raw_values": {k: _round(v, 6) if isinstance(v, float) else v
                           for k, v in self.raw_values.items()},
            "thresholds": {k: _round(v, 6) if isinstance(v, float) else v
                           for k, v in self.thresholds.items()},
            "reason": self.reason,
            "freshness": str(self.freshness),
            "timestamp": self.timestamp,
            "warnings": list(self.warnings),
        }


@dataclass
class SignalGroupResult:
    """One correlation group's netted opinion.

    A group contributes at most ONE independent vote however many of its
    members fired. That is the whole reason groups exist: three momentum
    indicators agreeing is one observation seen three times, not three
    observations.
    """
    group: CorrelationGroup
    direction: GroupDirection = GroupDirection.NO_SIGNAL
    weight: float = 0.0                 # [0, 1]; the group's influence
    magnitude: float = 0.0              # [0, 1]; strength before the penalty
    net: float = 0.0                    # signed sum of member votes
    internal_agreement: float = 1.0     # 1.0 aligned, 0.5 conflicted
    internal_disagreement: bool = False
    member_count: int = 0
    opinionated_members: int = 0
    buy_votes: int = 0
    sell_votes: int = 0
    neutral_votes: int = 0
    no_signal_votes: int = 0
    members: List[SignalResult] = field(default_factory=list)
    reason: str = ""

    @property
    def label(self) -> str:
        return GROUP_LABELS.get(str(self.group), str(self.group))

    @property
    def counted(self) -> int:
        """One independent opinion, or none."""
        return 1 if self.direction.is_directional else 0

    def as_dict(self) -> Dict:
        return {
            "group": str(self.group),
            "label": self.label,
            "description": GROUP_DESCRIPTIONS.get(str(self.group), ""),
            "direction": str(self.direction),
            "counted": self.counted,
            "weight": _round(self.weight, 4),
            "magnitude": _round(self.magnitude, 4),
            "net": _round(self.net, 4),
            "internal_agreement": _round(self.internal_agreement, 4),
            "internal_disagreement": self.internal_disagreement,
            "member_count": self.member_count,
            "opinionated_members": self.opinionated_members,
            "buy_votes": self.buy_votes,
            "sell_votes": self.sell_votes,
            "neutral_votes": self.neutral_votes,
            "no_signal_votes": self.no_signal_votes,
            "members": [m.as_dict() for m in self.members],
            "reason": self.reason,
        }


@dataclass
class QuantitativeSignalResult:
    """The canonical per-symbol quantitative answer.

    Carries no entry, target, stop, size or expected return. Those are
    trade-hypothesis concepts and belong behind the Risk Governor; a
    field here that could be read as an instruction would be a step
    toward execution by accident.
    """
    symbol: str
    timestamp: str = field(default_factory=utcnow)

    direction: SignalDirection = SignalDirection.NO_SIGNAL
    signal_agreement: float = 0.0       # [0, 1]  structural
    signal_magnitude: float = 0.0       # [0, 1]  how emphatic
    strength_band: StrengthBand = StrengthBand.NONE

    buy_groups: int = 0
    sell_groups: int = 0
    neutral_groups: int = 0
    no_signal_groups: int = 0
    opinionated_groups: int = 0
    groups_total: int = len(ALL_GROUPS)
    indicators_evaluated: int = 0
    indicators_directional: int = 0

    group_results: List[SignalGroupResult] = field(default_factory=list)
    indicator_results: List[SignalResult] = field(default_factory=list)

    market_regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    regime_adjustment: float = 1.0
    regime_adjusted_magnitude: float = 0.0
    regime_note: str = ""

    price: Optional[float] = None
    data_quality: Dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # No broker adapter exists in this build, paper or live.
    execution_available: bool = False

    @property
    def analysis_available(self) -> bool:
        return self.indicators_evaluated > 0

    def group(self, name) -> Optional[SignalGroupResult]:
        key = str(name)
        for g in self.group_results:
            if str(g.group) == key:
                return g
        return None

    def indicator(self, name: str) -> Optional[SignalResult]:
        for s in self.indicator_results:
            if s.indicator == name:
                return s
        return None

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "price": _round(self.price, 4),

            "direction": str(self.direction),
            "signal_agreement": _round(self.signal_agreement, 4),
            "signal_magnitude": _round(self.signal_magnitude, 4),
            "strength_band": str(self.strength_band),
            "agreement_meaning": AGREEMENT_MEANING,
            "magnitude_meaning": MAGNITUDE_MEANING,

            "buy_groups": self.buy_groups,
            "sell_groups": self.sell_groups,
            "neutral_groups": self.neutral_groups,
            "no_signal_groups": self.no_signal_groups,
            "opinionated_groups": self.opinionated_groups,
            "groups_total": self.groups_total,
            "indicators_evaluated": self.indicators_evaluated,
            "indicators_directional": self.indicators_directional,

            "group_results": [g.as_dict() for g in self.group_results],
            "indicator_results": [s.as_dict() for s in self.indicator_results],

            "market_regime": self.market_regime,
            "regime_confidence": _round(self.regime_confidence, 4),
            "regime_adjustment": _round(self.regime_adjustment, 4),
            "regime_adjusted_magnitude": _round(self.regime_adjusted_magnitude, 4),
            "regime_note": self.regime_note,

            "data_quality": dict(self.data_quality),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "analysis_available": self.analysis_available,
            "execution_available": self.execution_available,
            "disclaimer": DISCLAIMER,
        }


@dataclass
class SignalRun:
    """A batch of signal evaluations, usually for one scanner run."""
    signal_run_id: str
    session_date: str
    started_at: str
    completed_at: Optional[str] = None
    scanner_run_id: Optional[str] = None
    market_regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    market_session: str = "UNKNOWN"

    requested_count: int = 0
    evaluated_count: int = 0
    skipped_count: int = 0
    error_count: int = 0

    direction_counts: Dict[str, int] = field(default_factory=dict)
    provider_calls: int = 0
    duration_seconds: Optional[float] = None

    results: List[QuantitativeSignalResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    execution_available: bool = False

    @staticmethod
    def make_id(session_date: str, started_at: str) -> str:
        # Nonce because `started_at` has second resolution and two runs
        # in the same second would otherwise share an id, silently
        # overwriting each other - the exact collision the scanner hit.
        nonce = uuid.uuid4().hex[:8]
        digest = hashlib.sha256(
            f"{session_date}:{started_at}:{nonce}".encode()
        ).hexdigest()[:12]
        return f"sig_{digest}"

    def as_dict(self, include_results: bool = True) -> Dict:
        d = {
            "signal_run_id": self.signal_run_id,
            "session_date": self.session_date,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "scanner_run_id": self.scanner_run_id,
            "market_regime": self.market_regime,
            "regime_confidence": _round(self.regime_confidence, 4),
            "market_session": self.market_session,
            "requested_count": self.requested_count,
            "evaluated_count": self.evaluated_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "direction_counts": dict(self.direction_counts),
            "provider_calls": self.provider_calls,
            "duration_seconds": _round(self.duration_seconds, 3),
            "warnings": list(self.warnings),
            "error": self.error,
            "execution_available": self.execution_available,
        }
        if include_results:
            d["results"] = [r.as_dict() for r in self.results]
        return d
