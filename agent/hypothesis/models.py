"""
Trade hypothesis types.

A hypothesis is a structured, inspectable ARGUMENT that a trade might be
worth making. It is not permission to make one. Nothing here carries an
order, a quantity, a broker or a fill; the Risk Governor is the only
component that can approve anything, and it comes after this.

The design rule that shapes the whole module: **the quantitative engine
and the evidence engine may disagree, and that disagreement is
information.** Three cases must all be expressible:

    quant BUY + no evidence          -> a momentum hypothesis, honestly
                                        labelled as having no catalyst
    quant BUY + material NEGATIVE    -> a substantial contradiction, and
                                        usually no hypothesis at all
    quant NEUTRAL + evidence POSITIVE-> NOT automatically a trade; good
                                        news on a stock that is not
                                        moving is a story, not a setup

`hypothesis_strength` is a combination, but it is never a mysterious
score: every input that produced it is carried alongside it, the
contributions are itemised in `strength_components`, and the
contradictions that reduced it are listed. A reader can always ask why.
"""
from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class Strategy(str, enum.Enum):
    """A deliberately small taxonomy.

    Dozens of strategies would be unfalsifiable: each would have too few
    trades to evaluate, and the set would encode curve-fitting rather
    than a thesis. Each of these makes a claim that can be tested
    separately in the journal.
    """
    MOMENTUM = "MOMENTUM"
    MOMENTUM_CATALYST = "MOMENTUM_CATALYST"
    BREAKOUT = "BREAKOUT"
    MEAN_REVERSION = "MEAN_REVERSION"
    CATALYST_CONTINUATION = "CATALYST_CONTINUATION"
    NO_VALID_STRATEGY = "NO_VALID_STRATEGY"

    def __str__(self) -> str:
        return self.value


class HypothesisDirection(str, enum.Enum):
    """LONG only.

    SHORT exists in the vocabulary so the engine can *say* the evidence
    leans down without that being executable. The system is long-only
    operationally and no code path turns SHORT into an order.
    """
    LONG = "LONG"
    NONE = "NONE"

    def __str__(self) -> str:
        return self.value


class HypothesisStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"           # generated, not yet risk-evaluated
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    NOT_GENERATED = "NOT_GENERATED"  # no valid strategy applied

    def __str__(self) -> str:
        return self.value


class ContradictionSeverity(str, enum.Enum):
    BLOCKING = "BLOCKING"   # no hypothesis may be generated
    MAJOR = "MAJOR"         # strength heavily reduced
    MINOR = "MINOR"         # noted, small reduction

    def __str__(self) -> str:
        return self.value


STRENGTH_MEANING = (
    "hypothesis_strength combines how strongly the independent signal "
    "groups agree, how emphatic those readings are, whether published "
    "evidence supports or contradicts them, and whether the market "
    "regime is hospitable. It is NOT a probability of profit, not an "
    "expected return, and not a recommendation. Its components are "
    "itemised in strength_components so the number can always be taken "
    "apart."
)

DISCLAIMER = (
    "A hypothesis is an argument that a trade might be worth "
    "considering. It is not approval to trade, not investment advice, "
    "and carries no order, quantity or price. Only the Risk Governor "
    "can approve anything, and no execution path exists in this build."
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


@dataclass
class Contradiction:
    """Something that argues against the hypothesis.

    Kept as first-class records rather than folded into the score, so a
    reader sees what the engine was worried about even when it proceeded
    anyway.
    """
    code: str
    severity: ContradictionSeverity
    detail: str
    penalty: float = 0.0

    def as_dict(self) -> Dict:
        return {"code": self.code, "severity": str(self.severity),
                "detail": self.detail, "penalty": _round(self.penalty)}


@dataclass
class QuantitativeView:
    """What the signal engine said, copied verbatim."""
    direction: str = "NO_SIGNAL"
    agreement: float = 0.0
    magnitude: float = 0.0
    regime_adjusted_magnitude: float = 0.0
    strength_band: str = "NONE"
    buy_groups: int = 0
    sell_groups: int = 0
    opinionated_groups: int = 0
    freshness: str = "UNKNOWN"

    def as_dict(self) -> Dict:
        d = asdict(self)
        for key in ("agreement", "magnitude", "regime_adjusted_magnitude"):
            d[key] = _round(d[key])
        return d


@dataclass
class EvidenceView:
    """What the evidence engine said, copied verbatim."""
    active_catalyst: bool = False
    direction: str = "NEUTRAL"
    materiality: float = 0.0
    novelty: float = 0.0
    evidence_score: float = 0.0
    catalyst_type: Optional[str] = None
    window: Optional[str] = None
    independent_sources: int = 0
    primary_sources: int = 0
    conflicting: bool = False
    collected: bool = False          # False = we never looked

    def as_dict(self) -> Dict:
        d = asdict(self)
        for key in ("materiality", "novelty", "evidence_score"):
            d[key] = _round(d[key])
        return d


@dataclass
class MarketView:
    regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    risk_posture: str = "NO_NEW_TRADES"
    session: str = "UNKNOWN"

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["regime_confidence"] = _round(d["regime_confidence"])
        return d


@dataclass
class TradeHypothesis:
    """A structured argument, not an instruction.

    Deliberately carries NO order fields. `reference_price` and
    `suggested_stop_distance_pct` exist because the Risk Governor cannot
    evaluate risk without a notion of how far wrong the idea could go -
    but they are abstract inputs to that evaluation, not an order, and
    nothing here becomes a quantity.
    """
    hypothesis_id: str
    symbol: str
    generated_at: str = field(default_factory=utcnow)

    direction: HypothesisDirection = HypothesisDirection.NONE
    strategy: Strategy = Strategy.NO_VALID_STRATEGY
    status: HypothesisStatus = HypothesisStatus.PROPOSED

    quantitative: QuantitativeView = field(default_factory=QuantitativeView)
    evidence: EvidenceView = field(default_factory=EvidenceView)
    market: MarketView = field(default_factory=MarketView)

    hypothesis_strength: float = 0.0
    strength_components: Dict[str, float] = field(default_factory=dict)

    supporting_reasons: List[str] = field(default_factory=list)
    contradictions: List[Contradiction] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Abstract risk inputs. Not an order.
    reference_price: Optional[float] = None
    suggested_stop_distance_pct: Optional[float] = None
    reference_volatility_pct: Optional[float] = None

    # Provenance: which code and configuration produced this. Without
    # it, a later review cannot reproduce why the hypothesis existed.
    config_version: str = ""
    scanner_run_id: Optional[str] = None
    signal_run_id: Optional[str] = None
    evidence_run_id: Optional[str] = None

    execution_available: bool = False

    @staticmethod
    def make_id(symbol: str, generated_at: str) -> str:
        nonce = uuid.uuid4().hex[:8]
        digest = hashlib.sha256(
            f"{symbol}:{generated_at}:{nonce}".encode()).hexdigest()[:12]
        return f"hyp_{digest}"

    @property
    def is_actionable(self) -> bool:
        """Whether this is even a candidate for risk evaluation.

        Not "should we trade it" - that is the Risk Governor's question
        and it can still say no for a dozen reasons.
        """
        return (self.direction is HypothesisDirection.LONG
                and self.strategy is not Strategy.NO_VALID_STRATEGY)

    @property
    def blocking_contradictions(self) -> List[Contradiction]:
        return [c for c in self.contradictions
                if c.severity is ContradictionSeverity.BLOCKING]

    def as_dict(self) -> Dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "symbol": self.symbol,
            "generated_at": self.generated_at,
            "direction": str(self.direction),
            "strategy": str(self.strategy),
            "status": str(self.status),
            "quantitative": self.quantitative.as_dict(),
            "evidence": self.evidence.as_dict(),
            "market": self.market.as_dict(),
            "hypothesis_strength": _round(self.hypothesis_strength),
            "strength_components": {k: _round(v) for k, v
                                    in self.strength_components.items()},
            "strength_meaning": STRENGTH_MEANING,
            "supporting_reasons": list(self.supporting_reasons),
            "contradictions": [c.as_dict() for c in self.contradictions],
            "warnings": list(self.warnings),
            "reference_price": _round(self.reference_price),
            "suggested_stop_distance_pct":
                _round(self.suggested_stop_distance_pct),
            "reference_volatility_pct": _round(self.reference_volatility_pct),
            "config_version": self.config_version,
            "scanner_run_id": self.scanner_run_id,
            "signal_run_id": self.signal_run_id,
            "evidence_run_id": self.evidence_run_id,
            "is_actionable": self.is_actionable,
            "execution_available": self.execution_available,
            "disclaimer": DISCLAIMER,
        }


@dataclass
class HypothesisRun:
    """A batch of hypothesis generations."""
    hypothesis_run_id: str
    session_date: str
    started_at: str
    completed_at: Optional[str] = None
    scanner_run_id: Optional[str] = None
    signal_run_id: Optional[str] = None
    evidence_run_id: Optional[str] = None
    config_version: str = ""

    considered_count: int = 0
    generated_count: int = 0
    rejected_count: int = 0
    strategy_counts: Dict[str, int] = field(default_factory=dict)
    duration_seconds: Optional[float] = None

    hypotheses: List[TradeHypothesis] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    execution_available: bool = False

    @staticmethod
    def make_id(session_date: str, started_at: str) -> str:
        nonce = uuid.uuid4().hex[:8]
        digest = hashlib.sha256(
            f"{session_date}:{started_at}:{nonce}".encode()).hexdigest()[:12]
        return f"hyprun_{digest}"

    def as_dict(self, include_hypotheses: bool = True) -> Dict:
        d = {
            "hypothesis_run_id": self.hypothesis_run_id,
            "session_date": self.session_date,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "scanner_run_id": self.scanner_run_id,
            "signal_run_id": self.signal_run_id,
            "evidence_run_id": self.evidence_run_id,
            "config_version": self.config_version,
            "considered_count": self.considered_count,
            "generated_count": self.generated_count,
            "rejected_count": self.rejected_count,
            "strategy_counts": dict(self.strategy_counts),
            "duration_seconds": _round(self.duration_seconds, 3),
            "warnings": list(self.warnings),
            "execution_available": self.execution_available,
        }
        if include_hypotheses:
            d["hypotheses"] = [h.as_dict() for h in self.hypotheses]
        return d
