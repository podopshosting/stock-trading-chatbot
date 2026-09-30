"""
Broker-level types: orders, fills, positions, accounts.

These describe what a BROKER knows, which is deliberately narrower than
what the agent believes. The broker is authoritative for orders, fills,
positions and cash; the agent's own view is a cache of that truth and
must be reconciled against it.

Nothing here knows about strategies, hypotheses or signals. An order is
an instruction and a fill is a fact.
"""
from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    def __str__(self) -> str:
        return self.value


class OrderType(str, enum.Enum):
    """Market orders are deliberately absent.

    A market order on a thin name is an instruction to accept any price,
    and this system's whole risk model rests on knowing the worst case
    before committing. A marketable limit gets the same immediacy with a
    bound on how bad the fill may be.
    """
    LIMIT = "LIMIT"
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"

    def __str__(self) -> str:
        return self.value


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    def __str__(self) -> str:
        return self.value

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                        OrderStatus.REJECTED, OrderStatus.EXPIRED)

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.SUBMITTED,
                        OrderStatus.PARTIALLY_FILLED)


class TimeInForce(str, enum.Enum):
    DAY = "DAY"
    IOC = "IOC"          # immediate or cancel
    FOK = "FOK"          # fill or kill

    def __str__(self) -> str:
        return self.value


class RejectReason(str, enum.Enum):
    MARKET_CLOSED = "MARKET_CLOSED"
    INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    NO_QUOTE = "NO_QUOTE"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_PRICE = "INVALID_PRICE"
    DUPLICATE_CLIENT_ORDER_ID = "DUPLICATE_CLIENT_ORDER_ID"
    SHORTING_NOT_PERMITTED = "SHORTING_NOT_PERMITTED"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    ORDER_NOT_CANCELLABLE = "ORDER_NOT_CANCELLABLE"

    def __str__(self) -> str:
        return self.value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 6):
    return None if value is None else round(value, digits)


@dataclass
class Fill:
    """A fact: this much traded at this price at this moment."""
    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    filled_at: str = field(default_factory=utcnow)
    # What the fill cost relative to the reference price, in basis
    # points. Recorded so execution quality can be measured rather than
    # assumed.
    slippage_bps: Optional[float] = None

    @property
    def value(self) -> float:
        return self.quantity * self.price

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["side"] = str(self.side)
        d["quantity"] = _round(self.quantity)
        d["price"] = _round(self.price, 4)
        d["value"] = _round(self.value, 4)
        d["slippage_bps"] = _round(self.slippage_bps, 2)
        return d


@dataclass
class Order:
    """An instruction and its lifecycle."""
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    limit_price: Optional[float] = None
    time_in_force: TimeInForce = TimeInForce.DAY

    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: float = 0.0
    average_fill_price: Optional[float] = None
    fills: List[Fill] = field(default_factory=list)

    created_at: str = field(default_factory=utcnow)
    submitted_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None

    reject_reason: Optional[RejectReason] = None
    reject_detail: str = ""

    # Provenance back to the decision that produced it. Without this a
    # fill in the journal cannot be traced to the reasoning behind it.
    hypothesis_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    intent: str = ""              # ENTRY / EXIT / FLATTEN

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    @property
    def notional(self) -> Optional[float]:
        if self.average_fill_price is None:
            return None
        return self.filled_quantity * self.average_fill_price

    @staticmethod
    def make_id() -> str:
        return f"ord_{uuid.uuid4().hex[:16]}"

    def as_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": str(self.side),
            "order_type": str(self.order_type),
            "quantity": _round(self.quantity),
            "limit_price": _round(self.limit_price, 4),
            "time_in_force": str(self.time_in_force),
            "status": str(self.status),
            "filled_quantity": _round(self.filled_quantity),
            "remaining_quantity": _round(self.remaining_quantity),
            "average_fill_price": _round(self.average_fill_price, 4),
            "notional": _round(self.notional, 4),
            "fills": [f.as_dict() for f in self.fills],
            "created_at": self.created_at,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "reject_reason": (str(self.reject_reason)
                              if self.reject_reason else None),
            "reject_detail": self.reject_detail,
            "hypothesis_id": self.hypothesis_id,
            "risk_decision_id": self.risk_decision_id,
            "intent": self.intent,
        }


@dataclass
class BrokerPosition:
    """What the broker says is held. Authoritative."""
    symbol: str
    quantity: float
    average_entry_price: float
    opened_at: str = field(default_factory=utcnow)
    current_price: Optional[float] = None

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.average_entry_price

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

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "quantity": _round(self.quantity),
            "average_entry_price": _round(self.average_entry_price, 4),
            "cost_basis": _round(self.cost_basis, 2),
            "current_price": _round(self.current_price, 4),
            "market_value": _round(self.market_value, 2),
            "unrealized_pnl": _round(self.unrealized_pnl, 2),
            "unrealized_pnl_pct": _round(self.unrealized_pnl_pct, 3),
            "opened_at": self.opened_at,
        }


@dataclass
class Account:
    """Cash and buying power.

    `cash` moves on fills only. `buying_power` additionally reserves the
    notional of working buy orders, so two orders cannot each be sized
    against the same dollar - the classic way a paper broker flatters
    itself relative to a real one.
    """
    account_id: str = "paper"
    cash: float = 0.0
    starting_cash: float = 0.0
    reserved_cash: float = 0.0
    realized_pnl: float = 0.0
    is_paper: bool = True
    currency: str = "USD"

    @property
    def buying_power(self) -> float:
        # No margin. Buying power is cash that is not already promised.
        return max(0.0, self.cash - self.reserved_cash)

    def as_dict(self, positions_value: Optional[float] = None) -> Dict:
        equity = None
        if positions_value is not None:
            equity = self.cash + positions_value
        return {
            "account_id": self.account_id,
            "cash": _round(self.cash, 2),
            "starting_cash": _round(self.starting_cash, 2),
            "reserved_cash": _round(self.reserved_cash, 2),
            "buying_power": _round(self.buying_power, 2),
            "realized_pnl": _round(self.realized_pnl, 2),
            "positions_value": _round(positions_value, 2),
            "equity": _round(equity, 2),
            "is_paper": self.is_paper,
            "currency": self.currency,
        }


@dataclass
class Quote:
    """The market state an order is evaluated against."""
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    as_of: Optional[str] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return self.last
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Optional[float]:
        s, m = self.spread, self.mid
        if s is None or not m:
            return None
        return (s / m) * 100.0


class BrokerError(Exception):
    pass


class OrderRejected(BrokerError):
    def __init__(self, reason: RejectReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")
