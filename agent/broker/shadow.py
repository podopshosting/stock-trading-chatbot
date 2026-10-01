"""
Shadow comparison between the internal paper broker and the Alpaca one.

For the same approved trade the internal broker COMPUTES what it would
have done, and the external broker submits the real paper order. The
two are then compared. The internal result is comparison data only: it
never touches the external venue, and the external order is submitted
exactly once.

The point is to learn how far the internal simulation can be trusted. A
paper record built on an optimistic simulator is worse than none, and
the only way to know whether the simulator is optimistic is to put it
beside something that is not.

Expected fills are computed by a THROWAWAY copy of the internal broker
seeded from the same quote, so shadowing cannot disturb the real
internal account state.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..observability import log_event
from .models import Quote
from .paper import PaperBroker, PaperBrokerConfig


@dataclass
class ShadowRecord:
    """One trade, both ways."""
    client_order_id: str
    symbol: str
    requested_quantity: float
    requested_notional: Optional[float]
    intended_limit: Optional[float]
    reference_price: Optional[float]

    internal_status: Optional[str] = None
    internal_fill_price: Optional[float] = None
    internal_fill_quantity: Optional[float] = None
    internal_slippage_bps: Optional[float] = None
    internal_reject_reason: Optional[str] = None

    broker_status: Optional[str] = None
    broker_order_id: Optional[str] = None
    broker_fill_price: Optional[float] = None
    broker_fill_quantity: Optional[float] = None
    broker_fill_at: Optional[str] = None
    broker_reject_reason: Optional[str] = None

    broker_error: Optional[str] = None
    submitted_to_broker_count: int = 0

    @property
    def fill_price_difference(self) -> Optional[float]:
        """Broker minus internal. Positive means the REAL paper venue
        filled worse for a buyer than the simulation predicted."""
        if self.internal_fill_price is None or self.broker_fill_price is None:
            return None
        return self.broker_fill_price - self.internal_fill_price

    @property
    def fill_difference_bps(self) -> Optional[float]:
        d = self.fill_price_difference
        if d is None or not self.internal_fill_price:
            return None
        return d / self.internal_fill_price * 10_000.0

    @property
    def quantity_difference(self) -> Optional[float]:
        if self.internal_fill_quantity is None or \
                self.broker_fill_quantity is None:
            return None
        return self.broker_fill_quantity - self.internal_fill_quantity

    @property
    def outcomes_differ(self) -> Optional[bool]:
        """Did one side fill and the other not?

        The most important comparison: a simulator that fills what the
        real venue would have rejected is overstating the strategy.
        """
        if self.internal_status is None or self.broker_status is None:
            return None
        internal_filled = (self.internal_fill_quantity or 0) > 0
        broker_filled = (self.broker_fill_quantity or 0) > 0
        return internal_filled != broker_filled

    @property
    def exactly_one_broker_submission(self) -> bool:
        """The no-duplicate invariant, recorded per trade."""
        return self.submitted_to_broker_count == 1

    def as_dict(self) -> Dict:
        def r(v, d=4):
            return None if v is None else round(v, d)
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "requested_quantity": r(self.requested_quantity, 6),
            "requested_notional": r(self.requested_notional, 2),
            "intended_limit": r(self.intended_limit),
            "reference_price": r(self.reference_price),
            "internal": {
                "status": self.internal_status,
                "fill_price": r(self.internal_fill_price),
                "fill_quantity": r(self.internal_fill_quantity, 6),
                "slippage_bps": r(self.internal_slippage_bps, 2),
                "reject_reason": self.internal_reject_reason,
            },
            "broker": {
                "status": self.broker_status,
                "order_id": self.broker_order_id,
                "fill_price": r(self.broker_fill_price),
                "fill_quantity": r(self.broker_fill_quantity, 6),
                "fill_at": self.broker_fill_at,
                "reject_reason": self.broker_reject_reason,
                "error": self.broker_error,
            },
            "fill_price_difference": r(self.fill_price_difference),
            "fill_difference_bps": r(self.fill_difference_bps, 2),
            "quantity_difference": r(self.quantity_difference, 6),
            "outcomes_differ": self.outcomes_differ,
            "submitted_to_broker_count": self.submitted_to_broker_count,
            "exactly_one_broker_submission":
                self.exactly_one_broker_submission,
        }


def expected_internal_fill(quote: Quote, symbol: str, side: str,
                           quantity: float, limit_price: Optional[float],
                           config: Optional[PaperBrokerConfig] = None
                           ) -> Dict:
    """What the internal simulation would do, on a disposable broker.

    Never the real internal broker: shadowing must not move the
    internal account.
    """
    cfg = copy.copy(config) if config else PaperBrokerConfig()
    cfg.starting_cash = max(cfg.starting_cash, 1e9)   # never cash-limited
    cfg.partial_fill_probability = 0.0
    scratch = PaperBroker(cfg)
    scratch.set_quote(quote)
    if side.upper() == "SELL":
        # A sell needs something to sell; give the scratch broker the
        # position at the reference so the fill model is exercised.
        scratch.set_quote(quote)
        scratch.submit_order(symbol, "BUY", quantity)
    return scratch.submit_order(symbol, side, quantity,
                                limit_price=limit_price)


def submit_with_shadow(external, symbol: str, side: str, quantity: float,
                       limit_price: float, client_order_id: str,
                       quote: Optional[Quote],
                       internal_config: Optional[PaperBrokerConfig] = None,
                       **provenance) -> (Dict, ShadowRecord):
    """Submit ONE order to the external broker and record the comparison.

    The external submission happens exactly once. If it raises, the
    exception propagates after the record is built, so the caller still
    sees the failure and the shadow data is not silently lost.
    """
    record = ShadowRecord(
        client_order_id=client_order_id, symbol=symbol,
        requested_quantity=quantity,
        requested_notional=(quantity * limit_price
                            if limit_price else None),
        intended_limit=limit_price,
        reference_price=(quote.mid if quote else None))

    if quote is not None:
        try:
            internal = expected_internal_fill(
                quote, symbol, side, quantity, limit_price, internal_config)
            record.internal_status = internal.get("status")
            record.internal_fill_price = internal.get("average_fill_price")
            record.internal_fill_quantity = internal.get("filled_quantity")
            fills = internal.get("fills") or []
            if fills:
                record.internal_slippage_bps = fills[0].get("slippage_bps")
            record.internal_reject_reason = internal.get("reject_reason")
        except Exception as exc:                          # noqa: BLE001
            # A shadow failure must never block the real submission.
            record.internal_status = f"SHADOW_ERROR:{type(exc).__name__}"

    record.submitted_to_broker_count += 1
    try:
        order = external.submit_order(
            symbol, side, quantity, limit_price=limit_price,
            client_order_id=client_order_id, **provenance)
    except Exception as exc:                              # noqa: BLE001
        record.broker_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        log_event("shadow_comparison", symbol=symbol,
                  client_order_id=client_order_id,
                  broker_error=record.broker_error)
        exc.shadow_record = record                        # type: ignore
        raise

    record.broker_status = order.get("status")
    record.broker_order_id = order.get("order_id")
    record.broker_fill_price = order.get("average_fill_price")
    record.broker_fill_quantity = order.get("filled_quantity")
    fills = order.get("fills") or []
    if fills:
        record.broker_fill_at = fills[0].get("filled_at")
    record.broker_reject_reason = order.get("reject_reason")

    log_event("shadow_comparison", symbol=symbol,
              client_order_id=client_order_id,
              internal_status=record.internal_status,
              broker_status=record.broker_status,
              fill_difference_bps=(None if record.fill_difference_bps is None
                                   else round(record.fill_difference_bps, 2)),
              outcomes_differ=record.outcomes_differ)
    return order, record


def summarise(records: List[ShadowRecord]) -> Dict:
    """Aggregate comparison across trades, with the sample size.

    Reported with n and without a verdict on the simulator's quality
    until there are enough trades to say anything.
    """
    n = len(records)
    diffs = [r.fill_difference_bps for r in records
             if r.fill_difference_bps is not None]
    differ = [r for r in records if r.outcomes_differ]
    return {
        "trades_compared": n,
        "with_fill_comparison": len(diffs),
        "mean_fill_difference_bps": (sum(diffs) / len(diffs)
                                     if diffs else None),
        "worst_fill_difference_bps": max(diffs) if diffs else None,
        "outcome_disagreements": len(differ),
        "duplicate_broker_submissions": sum(
            1 for r in records if r.submitted_to_broker_count > 1),
        "adequate_for_a_conclusion": n >= 30,
        "note": ("fewer than 30 compared trades cannot say whether the "
                 "simulator is optimistic; the figures are shown, not "
                 "interpreted") if n < 30 else "",
    }
