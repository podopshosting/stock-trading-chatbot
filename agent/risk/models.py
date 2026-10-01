"""
Risk Governor types.

This is the safety boundary. Everything upstream produces arguments;
this is the only component that can approve anything, and its rejection
is absolute.

Three properties are structural rather than conventional:

**A rejection cannot be overridden.** `RiskDecision.approved` is set
once at construction from the reason codes. There is no setter, no
"force" flag, and no path by which a language model, a prompt, a config
value or a caller can turn a rejection into an approval. The only way to
get an approval is to present a hypothesis that violates no rule.

**Every rejection is inspectable.** A decision carries the codes, the
numbers that triggered them, and the limits they were measured against.
"Rejected" with no reason would be indistinguishable from a bug.

**Cash is a valid outcome.** Nothing here tries to deploy the daily
allocation. The limit is a ceiling, not a target, and a day with zero
trades is a correct result.
"""
from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class RejectionCode(str, enum.Enum):
    """Every way the governor can say no.

    A closed set: a rejection that did not appear here could not be
    filtered on, alerted on, or counted in the journal.
    """
    # global halts
    EMERGENCY_STOP = "EMERGENCY_STOP"
    TRADING_DISABLED = "TRADING_DISABLED"
    EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"
    DAILY_RISK_LOCK = "DAILY_RISK_LOCK"
    MARKET_CLOSED = "MARKET_CLOSED"

    # capital
    DAILY_CAPITAL_EXCEEDED = "DAILY_CAPITAL_EXCEEDED"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    POSITION_SIZE_EXCEEDED = "POSITION_SIZE_EXCEEDED"
    PER_TRADE_RISK_EXCEEDED = "PER_TRADE_RISK_EXCEEDED"

    # exposure
    MAX_POSITIONS_REACHED = "MAX_POSITIONS_REACHED"
    MAX_NEW_POSITIONS_REACHED = "MAX_NEW_POSITIONS_REACHED"
    ALREADY_HOLDING = "ALREADY_HOLDING"
    AVERAGING_DOWN_PROHIBITED = "AVERAGING_DOWN_PROHIBITED"

    # market quality
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    PRICE_OUT_OF_RANGE = "PRICE_OUT_OF_RANGE"

    # instrument policy
    NOT_LONG_ONLY = "NOT_LONG_ONLY"
    INSTRUMENT_NOT_PERMITTED = "INSTRUMENT_NOT_PERMITTED"

    # hypothesis quality
    HYPOTHESIS_NOT_ACTIONABLE = "HYPOTHESIS_NOT_ACTIONABLE"
    HYPOTHESIS_TOO_WEAK = "HYPOTHESIS_TOO_WEAK"
    BLOCKING_CONTRADICTION = "BLOCKING_CONTRADICTION"

    # events
    IMMINENT_BINARY_EVENT = "IMMINENT_BINARY_EVENT"

    # session
    TOO_LATE_IN_SESSION = "TOO_LATE_IN_SESSION"

    def __str__(self) -> str:
        return self.value


# Codes that mean "stop everything", not merely "not this trade".
HALT_CODES = frozenset({
    RejectionCode.EMERGENCY_STOP,
    RejectionCode.DAILY_RISK_LOCK,
    RejectionCode.TRADING_DISABLED,
})


class HaltScope(str, enum.Enum):
    """How far a halt reaches, and how long it lasts.

    GLOBAL is durable across session rollover. That distinction is the
    whole point: a halt that tomorrow's session silently clears is not a
    halt, it is a pause.
    """
    GLOBAL = "GLOBAL"       # persists until explicitly cleared
    SESSION = "SESSION"     # clears at the next session

    def __str__(self) -> str:
        return self.value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


@dataclass(frozen=True)
class RiskLimits:
    """Every configured limit, in one immutable place.

    Frozen on purpose: a limit that could be mutated at runtime by the
    code it constrains is not a limit. Changing one means constructing a
    new RiskLimits and bumping `version`, which is stamped onto every
    decision so a later review can reproduce what was in force.
    """
    version: str = "risk-v1.0.0"

    # --- capital ---
    daily_capital_limit: float = 50.00        # ceiling, never a target
    max_daily_capital: float = 100.00         # highest permitted configuration
    max_position_pct_of_daily: float = 0.60   # single-name concentration cap
    min_position_value: float = 5.00          # below this, costs dominate

    # --- loss ---
    daily_loss_limit: float = 5.00            # then DAILY_RISK_LOCK
    max_trade_risk: float = 2.00              # most one trade may lose

    # --- exposure ---
    max_concurrent_positions: int = 2
    # 3 chosen from the 3-5 range: with 2 concurrent positions and a $50
    # ceiling, more than 3 entries a day means churning the same capital,
    # and each round trip pays the spread twice.
    max_new_positions_per_day: int = 3

    # --- market quality ---
    max_spread_pct: float = 0.50
    min_dollar_volume: float = 20_000_000.0
    max_quote_age_seconds: float = 120.0      # intraday decisions need current prices
    min_price: float = 5.00
    max_price: float = 2000.00

    # --- hypothesis quality ---
    min_hypothesis_strength: float = 0.35

    # --- session ---
    # No new entries in the last stretch of the session: a position
    # opened at 15:55 must be flattened by 16:00, which is not a trade,
    # it is a round trip paid for with two spreads.
    no_new_entries_within_minutes_of_close: float = 30.0

    # --- instrument policy ---
    long_only: bool = True
    allow_options: bool = False
    allow_margin: bool = False
    allow_leverage: bool = False
    allow_shorting: bool = False
    allow_crypto: bool = False
    allow_otc: bool = False
    allow_averaging_down: bool = False

    def validate(self) -> None:
        if self.daily_capital_limit > self.max_daily_capital:
            raise ValueError(
                f"daily_capital_limit {self.daily_capital_limit} exceeds the "
                f"permitted maximum {self.max_daily_capital}")
        if self.daily_capital_limit <= 0:
            raise ValueError("daily_capital_limit must be positive")
        if not 0 < self.max_position_pct_of_daily <= 1.0:
            raise ValueError("max_position_pct_of_daily must be in (0, 1]")
        if self.max_trade_risk > self.daily_loss_limit:
            raise ValueError(
                "a single trade may not be permitted to lose more than the "
                "whole day's loss limit")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be at least 1")
        if self.max_new_positions_per_day < self.max_concurrent_positions:
            raise ValueError(
                "max_new_positions_per_day cannot be below "
                "max_concurrent_positions")
        # The instrument policy is not configurable upward in this build.
        for name in ("allow_options", "allow_margin", "allow_leverage",
                     "allow_shorting", "allow_crypto", "allow_otc",
                     "allow_averaging_down"):
            if getattr(self, name):
                raise ValueError(
                    f"{name} is not permitted in this build; enabling it "
                    f"requires a separate reviewed change")
        if not self.long_only:
            raise ValueError("this system is long-only")

    @property
    def max_position_value(self) -> float:
        return self.daily_capital_limit * self.max_position_pct_of_daily

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["max_position_value"] = _round(self.max_position_value, 2)
        return d


@dataclass
class RiskContext:
    """The world the decision is made in.

    Supplied by the caller rather than fetched here, so the governor is
    a pure function of its inputs and can be replayed exactly.
    """
    session_date: str
    market_session: str = "UNKNOWN"
    minutes_to_close: Optional[float] = None

    trading_enabled: bool = False
    execution_available: bool = False
    emergency_stop: bool = False
    emergency_stop_reason: str = ""
    daily_risk_lock: bool = False

    capital_deployed_today: float = 0.0
    realized_pnl_today: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: int = 0
    positions_opened_today: int = 0
    held_symbols: List[str] = field(default_factory=list)

    # Market quality for the symbol under evaluation.
    price: Optional[float] = None
    spread_pct: Optional[float] = None
    dollar_volume: Optional[float] = None
    quote_age_seconds: Optional[float] = None
    # Age of the MARKET DATA, from the provider's own timestamp. The
    # field above is how long since WE fetched, which on a delayed feed
    # reads as seconds for data that is minutes old. The freshness check
    # uses this one; None is unknown and refuses.
    source_age_seconds: Optional[float] = None
    feed_quality: Optional[str] = None

    security_type: str = "equity"
    exchange: str = ""
    is_otc: bool = False
    is_leveraged: bool = False

    imminent_binary_event: bool = False
    imminent_event_detail: str = ""

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class RiskDecision:
    """The verdict. Approval is derived, never assigned.

    There is deliberately no way to construct an approved decision that
    carries rejection codes, and no way to clear the codes afterwards.
    """
    decision_id: str
    symbol: str
    hypothesis_id: Optional[str]
    decided_at: str = field(default_factory=utcnow)

    reason_codes: List[RejectionCode] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    capital_required: float = 0.0
    capital_available: float = 0.0
    max_loss: float = 0.0
    position_value: float = 0.0
    stop_distance_pct: Optional[float] = None

    limits_version: str = ""
    limits_snapshot: Dict = field(default_factory=dict)
    context_snapshot: Dict = field(default_factory=dict)

    warnings: List[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        """Derived from the codes. There is no override.

        Making this a property rather than a field is the mechanism:
        nothing can set `approved = True` on a decision that has
        reasons to reject, because `approved` is not a thing that can
        be set at all.
        """
        return not self.reason_codes

    @property
    def requires_halt(self) -> bool:
        return any(c in HALT_CODES for c in self.reason_codes)

    def as_dict(self) -> Dict:
        return {
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "hypothesis_id": self.hypothesis_id,
            "decided_at": self.decided_at,
            "approved": self.approved,
            "requires_halt": self.requires_halt,
            "reason_codes": [str(c) for c in self.reason_codes],
            "reasons": list(self.reasons),
            "capital_required": _round(self.capital_required, 2),
            "capital_available": _round(self.capital_available, 2),
            "max_loss": _round(self.max_loss, 2),
            "position_value": _round(self.position_value, 2),
            "stop_distance_pct": _round(self.stop_distance_pct),
            "limits_version": self.limits_version,
            "limits": dict(self.limits_snapshot),
            "context": dict(self.context_snapshot),
            "warnings": list(self.warnings),
        }

    @staticmethod
    def make_id(symbol: str, decided_at: str) -> str:
        nonce = uuid.uuid4().hex[:8]
        digest = hashlib.sha256(
            f"{symbol}:{decided_at}:{nonce}".encode()).hexdigest()[:12]
        return f"risk_{digest}"


@dataclass
class GlobalHaltState:
    """Durable halt state, deliberately separate from session state.

    Milestone 3 carried a known deferral: the emergency stop lived on the
    daily session record, so a new session began with a blank one and the
    halt silently evaporated overnight. A stop that expires by itself is
    not a stop.

    This record has no session date. It persists until something
    explicitly clears it, and clearing requires naming who did it and
    why.
    """
    halted: bool = False
    reason: str = ""
    scope: HaltScope = HaltScope.GLOBAL
    engaged_at: Optional[str] = None
    engaged_by: str = ""
    cleared_at: Optional[str] = None
    cleared_by: str = ""
    revision: int = 0

    def as_dict(self) -> Dict:
        return {
            "halted": self.halted,
            "reason": self.reason,
            "scope": str(self.scope),
            "engaged_at": self.engaged_at,
            "engaged_by": self.engaged_by,
            "cleared_at": self.cleared_at,
            "cleared_by": self.cleared_by,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, row: Dict) -> "GlobalHaltState":
        return cls(
            halted=bool(row.get("halted")),
            reason=row.get("reason", ""),
            scope=HaltScope(row.get("scope", "GLOBAL")),
            engaged_at=row.get("engaged_at"),
            engaged_by=row.get("engaged_by", ""),
            cleared_at=row.get("cleared_at"),
            cleared_by=row.get("cleared_by", ""),
            revision=int(row.get("revision") or 0),
        )
