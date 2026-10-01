"""
Paper broker.

Simulates execution honestly enough to be worth learning from. The point
of a paper broker is not to show what the strategy *could* have made; it
is to find out what it *would* have made, which means modelling the
costs that actually erode returns.

Four things are modelled because omitting them is how a paper record
becomes flattering fiction:

  **You cross the spread.** A buy lifts the ask, a sell hits the bid.
  Filling at the mid, or at the signal price, quietly hands the strategy
  the entire spread on every round trip - which on a 0.3% spread and
  three trades a day is larger than most edges.

  **Slippage beyond the quote.** Size moves price. Configurable, applied
  on top of the spread.

  **Partial fills.** A limit order at the touch does not always fill in
  full, and a strategy that assumes it does will look better than it is.

  **Working orders reserve cash.** Two orders cannot be sized against the
  same dollar.

Fills are evaluated against a quote SUPPLIED BY THE CALLER, never
fetched here. That keeps the broker deterministic and replayable: the
same order against the same quote always produces the same fill.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

from ..observability import log_event
from .base import BrokerAdapter
from .models import (
    Account, BrokerPosition, Fill, Order, OrderRejected, OrderSide,
    OrderStatus, OrderType, Quote, RejectReason, TimeInForce, utcnow,
)


class PaperBrokerConfig:
    """Execution assumptions. All of them pessimistic by default.

    A paper broker tuned optimistically produces a record that cannot be
    compared with live results, which defeats the purpose of running one.
    """

    def __init__(self,
                 starting_cash: float = 100.00,
                 slippage_bps: float = 5.0,
                 partial_fill_probability: float = 0.25,
                 min_partial_fraction: float = 0.4,
                 allow_fractional: bool = True,
                 quantity_precision: int = 6,
                 seed: Optional[int] = None):
        self.starting_cash = starting_cash
        # Applied BEYOND the spread, in basis points of price.
        self.slippage_bps = slippage_bps
        self.partial_fill_probability = partial_fill_probability
        self.min_partial_fraction = min_partial_fraction
        self.allow_fractional = allow_fractional
        self.quantity_precision = quantity_precision
        # Seeded so a paper run is reproducible. An unseeded simulation
        # cannot be compared with itself between runs.
        self.seed = seed

    def as_dict(self) -> Dict:
        return {
            "starting_cash": self.starting_cash,
            "slippage_bps": self.slippage_bps,
            "partial_fill_probability": self.partial_fill_probability,
            "min_partial_fraction": self.min_partial_fraction,
            "allow_fractional": self.allow_fractional,
            "seed": self.seed,
        }


class PaperBroker(BrokerAdapter):
    """An in-process simulated broker.

    Market state is injected: `set_quote` supplies what the broker
    "sees", and `set_market_open` controls the session. Nothing here
    reaches a provider, so the same sequence of calls always produces
    the same result.
    """

    name = "paper"
    is_paper = True

    def __init__(self, config: Optional[PaperBrokerConfig] = None,
                 clock: Callable[[], str] = utcnow):
        self.config = config or PaperBrokerConfig()
        self._clock = clock
        self._rng = random.Random(self.config.seed)
        self._account = Account(
            account_id="paper", cash=self.config.starting_cash,
            starting_cash=self.config.starting_cash, is_paper=True)
        self._orders: Dict[str, Order] = {}
        self._client_ids: Dict[str, str] = {}
        self._positions: Dict[str, BrokerPosition] = {}
        self._quotes: Dict[str, Quote] = {}
        self._market_open = True

    # --- injected market state ------------------------------------------

    def set_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol.upper()] = quote
        position = self._positions.get(quote.symbol.upper())
        if position is not None and quote.mid is not None:
            position.current_price = quote.mid

    def set_market_open(self, is_open: bool) -> None:
        self._market_open = is_open

    # --- account and positions -------------------------------------------

    def get_account(self) -> Dict:
        return self._account.as_dict(self._positions_value())

    def _positions_value(self) -> float:
        total = 0.0
        for position in self._positions.values():
            value = position.market_value
            total += value if value is not None else position.cost_basis
        return total

    def get_positions(self) -> List[Dict]:
        return [p.as_dict() for p in self._positions.values()]

    def get_position(self, symbol: str) -> Optional[Dict]:
        position = self._positions.get(symbol.upper())
        return position.as_dict() if position else None

    def get_orders(self, status: Optional[str] = None) -> List[Dict]:
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if str(o.status) == status.upper()]
        return [o.as_dict() for o in orders]

    def get_order(self, order_id: str) -> Optional[Dict]:
        order = self._orders.get(order_id)
        return order.as_dict() if order else None

    # --- pricing ---------------------------------------------------------

    def _reference_price(self, quote: Quote, side: OrderSide) -> Optional[float]:
        """The price a marketable order would reach.

        A buy lifts the ask; a sell hits the bid. Filling at the mid
        would hand the strategy half the spread on entry and half on
        exit, which on three round trips a day is larger than most
        edges.
        """
        if side is OrderSide.BUY:
            return quote.ask if quote.ask is not None else quote.last
        return quote.bid if quote.bid is not None else quote.last

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        """Size moves price, always against you."""
        drift = price * (self.config.slippage_bps / 10_000.0)
        return price + drift if side is OrderSide.BUY else price - drift

    # --- submission ------------------------------------------------------

    def _reject_detached(self, symbol, side, quantity, client_order_id,
                         reason, detail) -> Dict:
        """A rejection for a request that never became a tracked order."""
        return {
            "order_id": None,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "status": str(OrderStatus.REJECTED),
            "filled_quantity": 0.0,
            "remaining_quantity": quantity,
            "fills": [],
            "reject_reason": str(reason),
            "reject_detail": detail,
        }

    def submit_order(self, symbol: str, side: str, quantity: float,
                     order_type: str = "MARKETABLE_LIMIT",
                     limit_price: Optional[float] = None,
                     time_in_force: str = "DAY",
                     client_order_id: Optional[str] = None,
                     hypothesis_id: Optional[str] = None,
                     risk_decision_id: Optional[str] = None,
                     intent: str = "") -> Dict:
        """Submit and attempt to fill immediately.

        Returns the order in whatever state it reached. A rejection is a
        returned order with `status=REJECTED` and a reason, not an
        exception: a rejected order is a fact worth recording, and
        raising would tempt callers to discard it.
        """
        symbol = (symbol or "").upper()
        side_enum = OrderSide(side.upper())
        type_enum = OrderType(order_type.upper())
        tif_enum = TimeInForce(time_in_force.upper())

        client_order_id = client_order_id or f"cli_{uuid.uuid4().hex[:16]}"
        order = Order(
            order_id=Order.make_id(), client_order_id=client_order_id,
            symbol=symbol, side=side_enum, order_type=type_enum,
            quantity=float(quantity), limit_price=limit_price,
            time_in_force=tif_enum, created_at=self._clock(),
            hypothesis_id=hypothesis_id, risk_decision_id=risk_decision_id,
            intent=intent)

        # Idempotency. A duplicate client id must not place a second
        # order - a retried Lambda invocation would otherwise double the
        # position.
        if client_order_id in self._client_ids:
            known = self._client_ids[client_order_id]
            existing = self._orders.get(known)
            if existing is None:
                # The id was used before but the order is gone, so the
                # state is inconsistent - most likely a partial restore.
                # REFUSE rather than place: this client id exists
                # precisely to stop a retry doubling a position, and
                # placing the order here would do the thing it prevents.
                log_event("order_rejected", symbol=symbol, side=side,
                          reason=str(RejectReason.DUPLICATE_CLIENT_ORDER_ID),
                          detail=(f"client_order_id {client_order_id} maps to "
                                  f"unknown order {known}; refusing rather "
                                  "than risk a duplicate position"))
                return self._reject_detached(
                    symbol, side, quantity, client_order_id,
                    RejectReason.DUPLICATE_CLIENT_ORDER_ID,
                    f"client_order_id {client_order_id} is known but its "
                    f"order {known} is missing from this broker's state")
            log_event("order_duplicate_suppressed", symbol=symbol,
                      client_order_id=client_order_id,
                      existing_order_id=existing.order_id)
            return existing.as_dict()

        rejection = self._validate(order)
        if rejection is not None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = rejection[0]
            order.reject_detail = rejection[1]
            order.updated_at = self._clock()
            self._orders[order.order_id] = order
            self._client_ids[client_order_id] = order.order_id
            log_event("order_rejected", symbol=symbol, side=str(side_enum),
                      reason=str(rejection[0]), detail=rejection[1])
            return order.as_dict()

        order.status = OrderStatus.SUBMITTED
        order.submitted_at = self._clock()
        self._orders[order.order_id] = order
        self._client_ids[client_order_id] = order.order_id

        if side_enum is OrderSide.BUY:
            self._reserve_for(order)

        log_event("order_submitted", symbol=symbol, side=str(side_enum),
                  order_id=order.order_id, quantity=round(order.quantity, 6),
                  order_type=str(type_enum), intent=intent)

        self._try_fill(order)
        return order.as_dict()

    def _validate(self, order: Order):
        if not self._market_open:
            return (RejectReason.MARKET_CLOSED,
                    "the market is closed")
        if order.quantity <= 0:
            return (RejectReason.INVALID_QUANTITY,
                    f"quantity {order.quantity} must be positive")
        if not self.config.allow_fractional and order.quantity % 1 != 0:
            return (RejectReason.INVALID_QUANTITY,
                    "fractional quantities are not enabled")
        if order.order_type is OrderType.LIMIT and not order.limit_price:
            return (RejectReason.INVALID_PRICE,
                    "a limit order requires a limit price")

        quote = self._quotes.get(order.symbol)
        if quote is None or self._reference_price(quote, order.side) is None:
            return (RejectReason.NO_QUOTE,
                    f"no quote available for {order.symbol}")

        if order.side is OrderSide.SELL:
            position = self._positions.get(order.symbol)
            held = position.quantity if position else 0.0
            if held <= 0:
                # Long only. Selling what is not held is shorting.
                return (RejectReason.SHORTING_NOT_PERMITTED,
                        f"no long position in {order.symbol} to sell")
            if order.quantity > held + 1e-9:
                return (RejectReason.INSUFFICIENT_POSITION,
                        f"{order.quantity} requested, {held} held")
        else:
            reference = self._reference_price(quote, order.side)
            required = order.quantity * self._apply_slippage(reference,
                                                             order.side)
            if required > self._account.buying_power + 1e-9:
                return (RejectReason.INSUFFICIENT_BUYING_POWER,
                        f"{required:.2f} required, "
                        f"{self._account.buying_power:.2f} available")
        return None

    def _reserve_for(self, order: Order) -> None:
        quote = self._quotes[order.symbol]
        reference = self._reference_price(quote, order.side)
        self._account.reserved_cash += (
            order.remaining_quantity * self._apply_slippage(reference,
                                                            order.side))

    def _release_reservation(self, order: Order, quantity: float) -> None:
        if order.side is not OrderSide.BUY:
            return
        quote = self._quotes.get(order.symbol)
        if quote is None:
            return
        reference = self._reference_price(quote, order.side)
        if reference is None:
            return
        amount = quantity * self._apply_slippage(reference, order.side)
        self._account.reserved_cash = max(0.0,
                                          self._account.reserved_cash - amount)

    # --- filling ---------------------------------------------------------

    def _try_fill(self, order: Order) -> None:
        quote = self._quotes.get(order.symbol)
        if quote is None:
            return
        reference = self._reference_price(quote, order.side)
        if reference is None:
            return

        fill_price = self._apply_slippage(reference, order.side)

        # A resting limit only fills if the market reaches it.
        if order.order_type is OrderType.LIMIT:
            if order.side is OrderSide.BUY and fill_price > order.limit_price:
                return
            if order.side is OrderSide.SELL and fill_price < order.limit_price:
                return
            fill_price = order.limit_price

        quantity = order.remaining_quantity
        partial = (self._rng.random() < self.config.partial_fill_probability
                   and quantity > 0)
        if partial:
            fraction = self._rng.uniform(self.config.min_partial_fraction, 0.95)
            quantity = round(quantity * fraction,
                             self.config.quantity_precision)
            if quantity <= 0:
                return

        # A fill-or-kill that cannot be filled in full is killed.
        if order.time_in_force is TimeInForce.FOK and partial:
            order.status = OrderStatus.CANCELLED
            order.updated_at = self._clock()
            self._release_reservation(order, order.remaining_quantity)
            log_event("order_cancelled", symbol=order.symbol,
                      order_id=order.order_id,
                      reason="fill-or-kill could not be filled in full")
            return

        self._record_fill(order, quantity, fill_price, reference)

        if order.remaining_quantity <= 1e-9:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            if order.time_in_force is TimeInForce.IOC:
                # The unfilled remainder of an IOC is cancelled.
                self._release_reservation(order, order.remaining_quantity)
                order.status = OrderStatus.CANCELLED
        order.updated_at = self._clock()

    def _record_fill(self, order: Order, quantity: float, price: float,
                     reference: float) -> None:
        slippage_bps = ((price - reference) / reference * 10_000.0
                        if reference else 0.0)
        if order.side is OrderSide.SELL:
            slippage_bps = -slippage_bps

        fill = Fill(fill_id=f"fill_{uuid.uuid4().hex[:12]}",
                    order_id=order.order_id, symbol=order.symbol,
                    side=order.side, quantity=quantity, price=price,
                    filled_at=self._clock(), slippage_bps=slippage_bps)
        order.fills.append(fill)

        previous_value = (order.filled_quantity
                          * (order.average_fill_price or 0.0))
        order.filled_quantity = round(order.filled_quantity + quantity,
                                      self.config.quantity_precision)
        order.average_fill_price = (
            (previous_value + fill.value) / order.filled_quantity
            if order.filled_quantity else None)

        self._release_reservation(order, quantity)
        self._apply_to_account(order, fill)

        log_event("order_filled", symbol=order.symbol, order_id=order.order_id,
                  side=str(order.side), quantity=round(quantity, 6),
                  price=round(price, 4),
                  slippage_bps=round(slippage_bps, 2),
                  remaining=round(order.remaining_quantity, 6))

    def _apply_to_account(self, order: Order, fill: Fill) -> None:
        symbol = order.symbol
        if order.side is OrderSide.BUY:
            self._account.cash -= fill.value
            position = self._positions.get(symbol)
            if position is None:
                self._positions[symbol] = BrokerPosition(
                    symbol=symbol, quantity=fill.quantity,
                    average_entry_price=fill.price,
                    opened_at=fill.filled_at, current_price=fill.price)
            else:
                total_cost = position.cost_basis + fill.value
                position.quantity = round(position.quantity + fill.quantity,
                                          self.config.quantity_precision)
                position.average_entry_price = (total_cost / position.quantity
                                                if position.quantity else 0.0)
                position.current_price = fill.price
        else:
            position = self._positions.get(symbol)
            if position is None:
                return
            realized = (fill.price - position.average_entry_price) * fill.quantity
            self._account.cash += fill.value
            self._account.realized_pnl += realized
            position.quantity = round(position.quantity - fill.quantity,
                                      self.config.quantity_precision)
            position.current_price = fill.price
            if position.quantity <= 1e-9:
                del self._positions[symbol]
            log_event("position_closed" if symbol not in self._positions
                      else "position_reduced",
                      symbol=symbol, realized_pnl=round(realized, 4))

    # --- lifecycle -------------------------------------------------------

    def cancel_order(self, order_id: str) -> Dict:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(RejectReason.UNKNOWN_ORDER, order_id)
        if order.status.is_terminal:
            raise OrderRejected(RejectReason.ORDER_NOT_CANCELLABLE,
                                f"order is {order.status}")
        self._release_reservation(order, order.remaining_quantity)
        order.status = OrderStatus.CANCELLED
        order.updated_at = self._clock()
        log_event("order_cancelled", symbol=order.symbol, order_id=order_id,
                  reason="requested")
        return order.as_dict()

    def replace_order(self, order_id: str, quantity: Optional[float] = None,
                      limit_price: Optional[float] = None) -> Dict:
        """Cancel and replace.

        Implemented as cancel-then-new rather than an in-place edit, so
        the audit trail shows two orders instead of one order that
        changed shape.
        """
        order = self._orders.get(order_id)
        if order is None:
            raise OrderRejected(RejectReason.UNKNOWN_ORDER, order_id)
        if order.status.is_terminal:
            raise OrderRejected(RejectReason.ORDER_NOT_CANCELLABLE,
                                f"order is {order.status}")
        self.cancel_order(order_id)
        return self.submit_order(
            symbol=order.symbol, side=str(order.side),
            quantity=quantity if quantity is not None
            else order.remaining_quantity,
            order_type=str(order.order_type),
            limit_price=limit_price if limit_price is not None
            else order.limit_price,
            time_in_force=str(order.time_in_force),
            hypothesis_id=order.hypothesis_id,
            risk_decision_id=order.risk_decision_id,
            intent=order.intent or "REPLACE")

    def close_position(self, symbol: str, intent: str = "EXIT") -> Dict:
        """Sell the whole position at market-crossing prices."""
        symbol = symbol.upper()
        position = self._positions.get(symbol)
        if position is None:
            raise OrderRejected(RejectReason.INSUFFICIENT_POSITION,
                                f"no position in {symbol}")
        return self.submit_order(
            symbol=symbol, side="SELL", quantity=position.quantity,
            order_type="MARKETABLE_LIMIT", intent=intent)

    def expire_day_orders(self) -> List[Dict]:
        """End of session: DAY orders that never filled are expired."""
        expired = []
        for order in self._orders.values():
            if order.is_open and order.time_in_force is TimeInForce.DAY:
                self._release_reservation(order, order.remaining_quantity)
                order.status = OrderStatus.EXPIRED
                order.updated_at = self._clock()
                expired.append(order.as_dict())
                log_event("order_expired", symbol=order.symbol,
                          order_id=order.order_id)
        return expired

    # --- introspection ---------------------------------------------------

    def capabilities(self) -> Dict:
        return {
            "name": self.name,
            "is_paper": True,
            "fractional_shares": self.config.allow_fractional,
            # Derived from the enum, never a hand-kept literal. A
            # hardcoded list can drift from what submit_order will
            # actually accept, and a capability declaration that
            # understates what the broker does is a lie the risk
            # model would rely on.
            "order_types": [t.value for t in OrderType],
            "time_in_force": ["DAY", "IOC", "FOK"],
            "shorting": False,
            "margin": False,
            "options": False,
            "models_spread": True,
            "models_slippage": True,
            "models_partial_fills": True,
            "config": self.config.as_dict(),
        }
