"""
The trade journal.

A `TradeRecord` is the complete history of one trade: the hypothesis
that proposed it, the risk decision that approved it, the orders that
executed it, the exit that ended it, and the configuration versions of
every component involved. It is written once, when the trade closes.

The design goal is reconstructability. A number in a performance report
is only worth anything if you can walk back from it to the reasoning
that produced the trade, and check whether that reasoning was sound.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class TradeOutcome(str, enum.Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    SCRATCH = "SCRATCH"        # closed within noise of the entry
    UNKNOWN = "UNKNOWN"        # could not be determined; never counted

    def __str__(self) -> str:
        return self.value


# A move smaller than this fraction of the planned risk is not a result,
# it is noise. Counting scratches as wins is a cheap way to inflate a
# win rate.
SCRATCH_THRESHOLD_R = 0.1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


@dataclass
class TradeCosts:
    """What the trade paid to exist.

    Kept separate from P&L so gross and net edge are both visible. A
    strategy with a real gross edge that is entirely eaten by spread is
    a different problem from one with no edge, and the two need
    different responses.
    """
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0
    spread_paid: float = 0.0
    commission: float = 0.0

    @property
    def total(self) -> float:
        return (self.entry_slippage + self.exit_slippage
                + self.spread_paid + self.commission)

    def as_dict(self) -> Dict:
        d = {k: _round(v, 4) for k, v in asdict(self).items()}
        d["total"] = _round(self.total, 4)
        return d


@dataclass
class TradeRecord:
    """One completed trade, with everything needed to re-examine it."""
    trade_id: str
    symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    opened_at: str
    closed_at: str

    # The plan as it stood at entry. Used for R-multiples, which are the
    # only fair way to compare trades of different sizes.
    planned_stop: float
    planned_target: Optional[float] = None

    # Why the trade was taken and why it ended.
    strategy: str = ""
    hypothesis_id: Optional[str] = None
    hypothesis_strength: Optional[float] = None
    risk_decision_id: Optional[str] = None
    exit_reason: str = ""
    all_exit_reasons: List[str] = field(default_factory=list)

    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    position_id: Optional[str] = None

    costs: TradeCosts = field(default_factory=TradeCosts)
    stop_history: List[Dict] = field(default_factory=list)
    max_favourable_price: Optional[float] = None
    max_adverse_price: Optional[float] = None

    # Provenance. Without these the record cannot be tied to the
    # behaviour of a specific version of the system, and performance
    # across a configuration change becomes meaningless.
    config_versions: Dict[str, str] = field(default_factory=dict)
    is_paper: bool = True
    session_date: str = ""
    recorded_at: str = field(default_factory=utcnow)

    @staticmethod
    def make_id() -> str:
        return f"trade_{uuid.uuid4().hex[:16]}"

    # --- results ---------------------------------------------------------

    @property
    def gross_pnl(self) -> float:
        return self.quantity * (self.exit_price - self.entry_price)

    @property
    def net_pnl(self) -> float:
        """What actually landed in the account.

        The paper broker's fills already include spread and slippage, so
        `costs` is a breakdown of what is already in gross_pnl rather
        than a further deduction. Subtracting it again would double-count
        and understate the result.
        """
        return self.gross_pnl - self.costs.commission

    @property
    def planned_risk_per_share(self) -> float:
        return max(0.0, self.entry_price - self.planned_stop)

    @property
    def planned_risk(self) -> float:
        """What the trade was supposed to be able to lose - one R."""
        return self.quantity * self.planned_risk_per_share

    @property
    def r_multiple(self) -> Optional[float]:
        """Result as a multiple of the risk taken.

        The only fair way to compare trades of different sizes. A $10
        win on $5 of risk and a $100 win on $50 of risk are the same
        trade; raw P&L says otherwise.
        """
        risk = self.planned_risk
        if risk <= 0:
            return None
        return self.net_pnl / risk

    @property
    def outcome(self) -> TradeOutcome:
        r = self.r_multiple
        if r is None:
            return TradeOutcome.UNKNOWN
        if abs(r) < SCRATCH_THRESHOLD_R:
            return TradeOutcome.SCRATCH
        return TradeOutcome.WIN if r > 0 else TradeOutcome.LOSS

    @property
    def hold_minutes(self) -> Optional[float]:
        try:
            opened = datetime.fromisoformat(self.opened_at)
            closed = datetime.fromisoformat(self.closed_at)
        except (TypeError, ValueError):
            return None
        return (closed - opened).total_seconds() / 60.0

    @property
    def exceeded_planned_risk(self) -> Optional[bool]:
        """Did the loss come in worse than the stop allowed for?

        This is the number that tells you whether the stops are real. A
        polled stop that is regularly overshot is not protecting what it
        claims to protect, and the strategy's risk model is wrong.
        """
        r = self.r_multiple
        if r is None:
            return None
        return r < -1.0

    @property
    def max_favourable_excursion_r(self) -> Optional[float]:
        """How far the trade went in your favour before it ended."""
        if self.max_favourable_price is None:
            return None
        risk = self.planned_risk_per_share
        if risk <= 0:
            return None
        return (self.max_favourable_price - self.entry_price) / risk

    @property
    def max_adverse_excursion_r(self) -> Optional[float]:
        """How far it went against you before it ended.

        A winner that spent time well below its stop level did not win
        because the plan worked; it won because the stop was not
        enforced. Worth being able to see.
        """
        if self.max_adverse_price is None:
            return None
        risk = self.planned_risk_per_share
        if risk <= 0:
            return None
        return (self.max_adverse_price - self.entry_price) / risk

    def as_dict(self) -> Dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "quantity": _round(self.quantity, 6),
            "entry_price": _round(self.entry_price),
            "exit_price": _round(self.exit_price),
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "hold_minutes": _round(self.hold_minutes, 2),
            "planned_stop": _round(self.planned_stop),
            "planned_target": _round(self.planned_target),
            "planned_risk": _round(self.planned_risk, 4),
            "gross_pnl": _round(self.gross_pnl, 4),
            "net_pnl": _round(self.net_pnl, 4),
            "r_multiple": _round(self.r_multiple, 4),
            "outcome": str(self.outcome),
            "exceeded_planned_risk": self.exceeded_planned_risk,
            "max_favourable_excursion_r": _round(
                self.max_favourable_excursion_r, 4),
            "max_adverse_excursion_r": _round(
                self.max_adverse_excursion_r, 4),
            "strategy": self.strategy,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_strength": _round(self.hypothesis_strength, 4),
            "risk_decision_id": self.risk_decision_id,
            "exit_reason": self.exit_reason,
            "all_exit_reasons": self.all_exit_reasons,
            "entry_order_id": self.entry_order_id,
            "exit_order_id": self.exit_order_id,
            "position_id": self.position_id,
            "costs": self.costs.as_dict(),
            "stop_history": self.stop_history,
            "config_versions": self.config_versions,
            "is_paper": self.is_paper,
            "session_date": self.session_date,
            "recorded_at": self.recorded_at,
        }
