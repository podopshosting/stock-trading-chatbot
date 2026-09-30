"""
Managed positions and exit intents.

A `ManagedPosition` is the agent's view of an open trade: what the
broker holds, plus the plan that was attached to it at entry. The broker
is authoritative for quantity and cost basis; everything else here is
the agent's own bookkeeping.

The single most important invariant in this file is that a stop may
never be widened. Moving a stop away from price to avoid taking a loss
is the most destructive discretionary act in retail trading, and an
automated system should make it structurally impossible rather than
merely discouraged.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class ExitReason(str, enum.Enum):
    """Why a position should be closed.

    Ordered by urgency in EXIT_PRIORITY below. Several may be true at
    once; all of them are recorded, because collapsing them into one
    label destroys the ability to ask later which rule actually earns
    its keep.
    """
    # Risk-driven: these must never be blocked.
    GLOBAL_HALT = "GLOBAL_HALT"
    HARD_STOP = "HARD_STOP"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    BROKER_DIVERGENCE = "BROKER_DIVERGENCE"

    # Plan-driven.
    TRAILING_STOP = "TRAILING_STOP"
    PROFIT_TARGET = "PROFIT_TARGET"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    TIME_STOP = "TIME_STOP"
    END_OF_DAY = "END_OF_DAY"

    # Operator-driven.
    MANUAL = "MANUAL"

    def __str__(self) -> str:
        return self.value


# Most protective first. When several reasons fire, the winner is the
# earliest in this list, but every reason is carried on the intent.
EXIT_PRIORITY: List[ExitReason] = [
    ExitReason.GLOBAL_HALT,
    ExitReason.BROKER_DIVERGENCE,
    ExitReason.HARD_STOP,
    ExitReason.DAILY_LOSS_LIMIT,
    ExitReason.TRAILING_STOP,
    ExitReason.THESIS_INVALIDATED,
    ExitReason.PROFIT_TARGET,
    ExitReason.TIME_STOP,
    ExitReason.END_OF_DAY,
    ExitReason.MANUAL,
]

# Reasons that reduce risk. These are honoured even when the risk
# posture forbids new trades: a system that can open a position but not
# close one is far more dangerous than one that can do neither.
PROTECTIVE_REASONS = frozenset({
    ExitReason.GLOBAL_HALT,
    ExitReason.BROKER_DIVERGENCE,
    ExitReason.HARD_STOP,
    ExitReason.DAILY_LOSS_LIMIT,
    ExitReason.TRAILING_STOP,
    ExitReason.THESIS_INVALIDATED,
    ExitReason.END_OF_DAY,
})


class StopMechanism(str, enum.Enum):
    """How a stop is actually enforced.

    This distinction is recorded because it is the difference between a
    stop that works and a stop that is a hope. An ENGINE_POLLED stop is
    only evaluated when a cycle runs; between cycles the price can move
    through it freely. A backtest that treats a polled stop as though it
    filled at the stop price is flattering itself.
    """
    ENGINE_POLLED = "ENGINE_POLLED"
    BROKER_RESTING = "BROKER_RESTING"

    def __str__(self) -> str:
        return self.value


class PositionState(str, enum.Enum):
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"      # reconciliation failed; treat as unsafe

    def __str__(self) -> str:
        return self.value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


class StopWidened(Exception):
    """Raised when something tries to move a stop away from price."""


@dataclass
class ExitPlan:
    """The plan attached to a position at entry.

    Written once at entry and then only ever tightened. Every field is
    optional except the hard stop, because a position without a stop is
    an unbounded loss and this system does not take those.
    """
    stop_price: float
    target_price: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    max_hold_minutes: Optional[int] = None
    flatten_before_close_minutes: Optional[int] = 10
    stop_mechanism: StopMechanism = StopMechanism.ENGINE_POLLED
    # The cycle interval the polled stop relies on. Recorded so the gap
    # between evaluations is a known quantity rather than an assumption.
    evaluation_interval_seconds: Optional[int] = None

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["stop_price"] = _round(self.stop_price)
        d["target_price"] = _round(self.target_price)
        d["stop_mechanism"] = str(self.stop_mechanism)
        return d


@dataclass
class ManagedPosition:
    """An open trade and its plan."""
    position_id: str
    symbol: str
    quantity: float
    entry_price: float
    plan: ExitPlan
    opened_at: str = field(default_factory=utcnow)

    state: PositionState = PositionState.OPEN
    high_water_price: Optional[float] = None
    # The worst price seen while open. A winner that spent time well
    # below its stop level did not win because the plan worked - it
    # won because the stop was not enforced, and that needs to be
    # visible in the journal rather than inferred.
    low_water_price: Optional[float] = None
    current_price: Optional[float] = None
    last_evaluated_at: Optional[str] = None

    # Provenance. Without these a trade in the journal cannot be traced
    # to the reasoning that produced it.
    hypothesis_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    config_version: str = ""

    stop_history: List[Dict] = field(default_factory=list)
    exit_reasons: List[ExitReason] = field(default_factory=list)

    @staticmethod
    def make_id() -> str:
        return f"pos_{uuid.uuid4().hex[:16]}"

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.entry_price

    @property
    def market_value(self) -> Optional[float]:
        if self.current_price is None:
            return None
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> Optional[float]:
        value = self.market_value
        return None if value is None else value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> Optional[float]:
        pnl = self.unrealized_pnl
        if pnl is None or self.cost_basis == 0:
            return None
        return (pnl / self.cost_basis) * 100.0

    @property
    def risk_per_share(self) -> float:
        """Distance from entry to stop. What one share can lose."""
        return max(0.0, self.entry_price - self.plan.stop_price)

    @property
    def open_risk(self) -> Optional[float]:
        """What the position can still lose if the stop holds.

        Once price is above the stop this shrinks; once the stop is
        above entry it is negative, meaning the worst case is a profit.
        """
        if self.current_price is None:
            return None
        return self.quantity * (self.current_price - self.plan.stop_price)

    def tighten_stop(self, new_stop: float, reason: str = "") -> bool:
        """Move the stop UP only. Returns True if it moved.

        A long position's stop may only rise. Widening it converts a
        bounded loss into an unbounded one, which is exactly the
        decision a human under pressure makes and an automated system
        must not be able to.
        """
        if new_stop is None:
            raise StopWidened("a stop may not be set to None")
        if new_stop < self.plan.stop_price:
            raise StopWidened(
                f"refusing to widen the stop on {self.symbol} from "
                f"{self.plan.stop_price} to {new_stop}")
        if new_stop == self.plan.stop_price:
            return False
        self.stop_history.append({
            "from": _round(self.plan.stop_price),
            "to": _round(new_stop),
            "at": utcnow(),
            "reason": reason,
        })
        self.plan.stop_price = new_stop
        return True

    def as_dict(self) -> Dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "quantity": _round(self.quantity, 6),
            "entry_price": _round(self.entry_price),
            "current_price": _round(self.current_price),
            "high_water_price": _round(self.high_water_price),
            "low_water_price": _round(self.low_water_price),
            "cost_basis": _round(self.cost_basis, 2),
            "market_value": _round(self.market_value, 2),
            "unrealized_pnl": _round(self.unrealized_pnl, 2),
            "unrealized_pnl_pct": _round(self.unrealized_pnl_pct, 3),
            "risk_per_share": _round(self.risk_per_share),
            "open_risk": _round(self.open_risk, 2),
            "state": str(self.state),
            "plan": self.plan.as_dict(),
            "opened_at": self.opened_at,
            "last_evaluated_at": self.last_evaluated_at,
            "hypothesis_id": self.hypothesis_id,
            "risk_decision_id": self.risk_decision_id,
            "entry_order_id": self.entry_order_id,
            "exit_order_id": self.exit_order_id,
            "config_version": self.config_version,
            "stop_history": self.stop_history,
            "exit_reasons": [str(r) for r in self.exit_reasons],
        }


@dataclass
class ExitIntent:
    """A decision to close a position, and why.

    Carries every reason that fired, not only the winner, so the
    question "which exit rule actually earns its keep" stays answerable
    from the record.
    """
    intent_id: str
    position_id: str
    symbol: str
    quantity: float
    primary_reason: ExitReason
    all_reasons: List[ExitReason] = field(default_factory=list)
    detail: str = ""
    decided_at: str = field(default_factory=utcnow)

    reference_price: Optional[float] = None
    expected_pnl: Optional[float] = None
    # True when the exit reduces risk and must not be gated by the
    # checks that govern new exposure.
    protective: bool = False
    config_version: str = ""

    @staticmethod
    def make_id() -> str:
        return f"exit_{uuid.uuid4().hex[:16]}"

    @property
    def is_full_exit(self) -> bool:
        return True

    def as_dict(self) -> Dict:
        return {
            "intent_id": self.intent_id,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "quantity": _round(self.quantity, 6),
            "primary_reason": str(self.primary_reason),
            "all_reasons": [str(r) for r in self.all_reasons],
            "detail": self.detail,
            "decided_at": self.decided_at,
            "reference_price": _round(self.reference_price),
            "expected_pnl": _round(self.expected_pnl, 2),
            "protective": self.protective,
            "config_version": self.config_version,
        }


@dataclass
class ReconciliationResult:
    """Whether the agent's view matches the broker's.

    The broker is authoritative. Any divergence is treated as unsafe
    rather than reconciled silently, because the two ways it happens -
    an order that filled without being recorded, and a position that
    was closed elsewhere - both mean the agent's risk arithmetic is
    wrong.
    """
    matched: bool
    checked_at: str = field(default_factory=utcnow)
    agent_only: List[str] = field(default_factory=list)
    broker_only: List[str] = field(default_factory=list)
    quantity_mismatches: List[Dict] = field(default_factory=list)
    detail: str = ""

    @property
    def safe_to_trade(self) -> bool:
        """Derived. There is no override."""
        return self.matched

    def as_dict(self) -> Dict:
        return {
            "matched": self.matched,
            "safe_to_trade": self.safe_to_trade,
            "checked_at": self.checked_at,
            "agent_only": self.agent_only,
            "broker_only": self.broker_only,
            "quantity_mismatches": self.quantity_mismatches,
            "detail": self.detail,
        }
