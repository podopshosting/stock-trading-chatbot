"""
Scanner data model.

The scanner answers one question: **which liquid securities deserve
deeper analysis right now, and why?** It does not answer "what should I
buy". Two consequences run through this module:

* A `Candidate` carries no `side`, `entry`, `target` or `stop`. Those
  belong to a trade hypothesis, behind the Risk Governor, and a candidate
  that could be read as an instruction would be a step toward execution
  by accident.
* `scanner_score` ranks *research interest*, not expected return. A 90
  means "look here first", never "this will go up".

Rejected symbols are first-class records. Without them there is no way to
tell a scanner that is being appropriately selective from one that is
quietly broken.
"""
from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class RejectionReason(str, enum.Enum):
    INACTIVE = "INACTIVE"
    NOT_TRADABLE = "NOT_TRADABLE"
    UNSUPPORTED_ASSET_CLASS = "UNSUPPORTED_ASSET_CLASS"
    UNSUPPORTED_EXCHANGE = "UNSUPPORTED_EXCHANGE"
    OTC_EXCLUDED = "OTC_EXCLUDED"
    PENNY_STOCK_EXCLUDED = "PENNY_STOCK_EXCLUDED"
    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    PRICE_ABOVE_MAXIMUM = "PRICE_ABOVE_MAXIMUM"
    VOLUME_BELOW_MINIMUM = "VOLUME_BELOW_MINIMUM"
    DOLLAR_VOLUME_BELOW_MINIMUM = "DOLLAR_VOLUME_BELOW_MINIMUM"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    SPREAD_UNKNOWN = "SPREAD_UNKNOWN"
    STALE_QUOTE = "STALE_QUOTE"
    MISSING_QUOTE = "MISSING_QUOTE"
    MISSING_BAR_DATA = "MISSING_BAR_DATA"
    LEVERAGED_ETF_EXCLUDED = "LEVERAGED_ETF_EXCLUDED"
    ETF_EXCLUDED = "ETF_EXCLUDED"
    EQUITY_EXCLUDED = "EQUITY_EXCLUDED"
    NOT_FRACTIONABLE = "NOT_FRACTIONABLE"
    UNKNOWN_SECURITY_TYPE = "UNKNOWN_SECURITY_TYPE"
    BELOW_REGIME_THRESHOLD = "BELOW_REGIME_THRESHOLD"
    UNIVERSE_CAP_REACHED = "UNIVERSE_CAP_REACHED"

    def __str__(self) -> str:
        return self.value


class ScanStatus(str, enum.Enum):
    COMPLETE = "COMPLETE"
    MARKET_CLOSED = "MARKET_CLOSED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    REGIME_UNAVAILABLE = "REGIME_UNAVAILABLE"
    EMPTY_UNIVERSE = "EMPTY_UNIVERSE"
    SCAN_DISABLED = "SCAN_DISABLED"
    FAILED = "FAILED"

    def __str__(self) -> str:
        return self.value


class DataFreshness(str, enum.Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"

    def __str__(self) -> str:
        return self.value


class SecurityType(str, enum.Enum):
    EQUITY = "equity"
    ETF = "etf"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


@dataclass(frozen=True)
class UniverseSecurity:
    """Normalised reference data for one security.

    Provider-agnostic on purpose: eligibility rules must never be written
    against a raw provider payload, or swapping providers rewrites the
    policy.
    """
    symbol: str
    name: str = ""
    exchange: str = ""
    asset_class: str = "us_equity"
    security_type: SecurityType = SecurityType.UNKNOWN
    tradable: bool = False
    fractionable: bool = False
    shortable: bool = False
    status: str = "active"
    leveraged: bool = False
    attributes: tuple = ()

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["security_type"] = str(self.security_type)
        d["attributes"] = list(self.attributes)
        return d


@dataclass
class ScannerSnapshot:
    """Normalised market state for one symbol at scan time.

    `as_of` is the PROVIDER's event time; `retrieved_at` is when we
    fetched it. Collapsing the two would make stale data look current,
    which is the failure this project has already been bitten by.
    """
    symbol: str
    as_of: Optional[str] = None
    retrieved_at: Optional[float] = None
    age_seconds: Optional[float] = None
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None
    avg_daily_volume: Optional[float] = None
    session_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    previous_close: Optional[float] = None
    previous_volume: Optional[int] = None
    vwap: Optional[float] = None
    provider: str = ""
    freshness: DataFreshness = DataFreshness.MISSING
    error: Optional[str] = None

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Optional[float]:
        """Spread over the midpoint, or None when bid/ask are unknown.

        None rather than 0.0: an unknown spread must never look tight to a
        liquidity filter.
        """
        s = self.spread
        if s is None:
            return None
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return None
        return (s / mid) * 100

    @property
    def dollar_volume(self) -> Optional[float]:
        if self.price is None or self.volume is None:
            return None
        return self.price * self.volume

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["freshness"] = str(self.freshness)
        d["spread_pct"] = _round(self.spread_pct, 5)
        d["dollar_volume"] = _round(self.dollar_volume, 2)
        return d


@dataclass
class ScannerFeatures:
    """Ranking features. Deliberately few: this is a funnel, not the
    Signal Engine (Milestone 5)."""
    symbol: str
    session_change_pct: Optional[float] = None
    return_5m: Optional[float] = None
    return_15m: Optional[float] = None
    return_30m: Optional[float] = None
    relative_volume: Optional[float] = None
    relative_volume_basis: str = ""
    distance_from_vwap_pct: Optional[float] = None
    above_vwap: Optional[bool] = None
    range_position: Optional[float] = None          # 0 = low of day, 1 = high
    distance_from_high_pct: Optional[float] = None
    distance_from_low_pct: Optional[float] = None
    market_relative_strength: Optional[float] = None
    benchmark_symbol: str = ""
    intraday_bars_used: int = 0

    def as_dict(self) -> Dict:
        d = asdict(self)
        for k in ("session_change_pct", "return_5m", "return_15m", "return_30m",
                  "relative_volume", "distance_from_vwap_pct", "range_position",
                  "distance_from_high_pct", "distance_from_low_pct",
                  "market_relative_strength"):
            d[k] = _round(d[k], 5)
        return d


@dataclass
class Candidate:
    """A symbol worth deeper analysis, with the arithmetic that put it
    there.

    Carries no side, entry, target or stop. Those are trade-hypothesis
    concepts and live behind the Risk Governor.
    """
    candidate_id: str
    scanner_run_id: str
    symbol: str
    timestamp: str
    rank: int = 0
    price: Optional[float] = None
    spread_pct: Optional[float] = None
    dollar_volume: Optional[float] = None
    freshness: DataFreshness = DataFreshness.MISSING
    features: Optional[ScannerFeatures] = None
    component_scores: Dict[str, float] = field(default_factory=dict)
    scanner_score: float = 0.0
    score_meaning: str = (
        "research priority only: how much this symbol merits a closer look. "
        "NOT an expected return, a forecast, or a recommendation."
    )
    market_regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    risk_posture: str = "NO_NEW_TRADES"
    eligible: bool = True
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    provider: str = ""

    @staticmethod
    def make_id(run_id: str, symbol: str) -> str:
        digest = hashlib.sha256(f"{run_id}:{symbol}".encode()).hexdigest()[:12]
        return f"cand_{digest}"

    def as_dict(self) -> Dict:
        return {
            "candidate_id": self.candidate_id,
            "scanner_run_id": self.scanner_run_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "rank": self.rank,
            "price": _round(self.price, 4),
            "spread_pct": _round(self.spread_pct, 5),
            "dollar_volume": _round(self.dollar_volume, 2),
            "freshness": str(self.freshness),
            "features": self.features.as_dict() if self.features else {},
            "component_scores": {k: round(v, 4)
                                 for k, v in self.component_scores.items()},
            "scanner_score": round(self.scanner_score, 2),
            "score_meaning": self.score_meaning,
            "market_regime": self.market_regime,
            "regime_confidence": round(self.regime_confidence, 4),
            "risk_posture": self.risk_posture,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "provider": self.provider,
        }


@dataclass
class Rejection:
    symbol: str
    reasons: List[str] = field(default_factory=list)
    stage: str = "static"

    def as_dict(self) -> Dict:
        return {"symbol": self.symbol, "reasons": list(self.reasons),
                "stage": self.stage}


@dataclass
class ScannerRun:
    scanner_run_id: str
    session_date: str
    started_at: str
    completed_at: Optional[str] = None
    status: ScanStatus = ScanStatus.FAILED
    market_session: str = "UNKNOWN"
    market_regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    risk_posture: str = "NO_NEW_TRADES"
    regime_gate: Dict = field(default_factory=dict)

    universe_count: int = 0
    static_eligible_count: int = 0
    shortlist_count: int = 0
    dynamic_eligible_count: int = 0
    candidate_count: int = 0
    rejected_count: int = 0
    stale_count: int = 0
    missing_count: int = 0

    rejection_reason_counts: Dict[str, int] = field(default_factory=dict)
    rejection_samples: List[Dict] = field(default_factory=list)
    score_distribution: Dict = field(default_factory=dict)

    provider_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    provider_errors: int = 0
    duration_seconds: Optional[float] = None

    candidates: List[Candidate] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    # There is no broker adapter in this build. Serialised so a consumer
    # cannot mistake a ranking for something actionable.
    execution_available: bool = False

    @staticmethod
    def make_id(session_date: str, started_at: str) -> str:
        """Unique per run, not per second.

        Hashing only date+timestamp collided: `started_at` has second
        resolution, so two scans in the same second produced the same id
        and the second silently overwrote the first. A retry or a manual
        re-run is enough to hit that.
        """
        nonce = uuid.uuid4().hex[:8]
        digest = hashlib.sha256(
            f"{session_date}:{started_at}:{nonce}".encode()
        ).hexdigest()[:12]
        return f"scan_{digest}"

    def as_dict(self, include_candidates: bool = True) -> Dict:
        d = {
            "scanner_run_id": self.scanner_run_id,
            "session_date": self.session_date,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": str(self.status),
            "market_session": self.market_session,
            "market_regime": self.market_regime,
            "regime_confidence": round(self.regime_confidence, 4),
            "risk_posture": self.risk_posture,
            "regime_gate": self.regime_gate,
            "universe_count": self.universe_count,
            "static_eligible_count": self.static_eligible_count,
            "shortlist_count": self.shortlist_count,
            "dynamic_eligible_count": self.dynamic_eligible_count,
            "candidate_count": self.candidate_count,
            "rejected_count": self.rejected_count,
            "stale_count": self.stale_count,
            "missing_count": self.missing_count,
            "rejection_reason_counts": dict(self.rejection_reason_counts),
            "rejection_samples": list(self.rejection_samples),
            "score_distribution": dict(self.score_distribution),
            "provider_calls": self.provider_calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "provider_errors": self.provider_errors,
            "duration_seconds": _round(self.duration_seconds, 3),
            "warnings": list(self.warnings),
            "error": self.error,
            "execution_available": self.execution_available,
        }
        if include_candidates:
            d["candidates"] = [c.as_dict() for c in self.candidates]
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "ScannerRun":
        d = dict(d)
        d["status"] = ScanStatus(d.get("status", "FAILED"))
        raw_candidates = d.pop("candidates", [])
        known = {f for f in cls.__dataclass_fields__}
        run = cls(**{k: v for k, v in d.items() if k in known})
        run.candidates = [candidate_from_dict(c) for c in raw_candidates]
        return run


def candidate_from_dict(d: Dict) -> Candidate:
    d = dict(d)
    features = d.pop("features", None) or None
    d.pop("score_meaning", None)
    known = {f for f in Candidate.__dataclass_fields__}
    cand = Candidate(**{k: v for k, v in d.items() if k in known})
    cand.freshness = DataFreshness(str(d.get("freshness", "MISSING")))
    if features:
        known_f = {f for f in ScannerFeatures.__dataclass_fields__}
        cand.features = ScannerFeatures(
            **{k: v for k, v in features.items() if k in known_f}
        )
    return cand
