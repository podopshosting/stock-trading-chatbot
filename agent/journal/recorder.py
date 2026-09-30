"""
Turning a closed position into a journal entry.

This is the seam where the trading system hands off to the measurement
system, and the place where provenance is either preserved or lost. Once
a position is gone, anything not copied here is unrecoverable.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..observability import log_event
from .models import TradeCosts, TradeRecord, utcnow


class RecorderError(Exception):
    pass


def costs_from_orders(entry_order: Optional[Dict],
                      exit_order: Optional[Dict],
                      quantity: float,
                      reference_entry: Optional[float] = None,
                      reference_exit: Optional[float] = None) -> TradeCosts:
    """Reconstruct execution costs from the fills.

    Slippage is taken from what the broker recorded on each fill rather
    than recomputed, so the number in the journal is the number the
    execution actually produced.
    """
    costs = TradeCosts()

    def slippage_cost(order, ref):
        if not order:
            return 0.0
        total = 0.0
        for fill in order.get("fills") or []:
            bps = fill.get("slippage_bps")
            if bps is None:
                continue
            total += abs(fill.get("price", 0.0)
                         * (bps / 10_000.0) * fill.get("quantity", 0.0))
        return total

    costs.entry_slippage = slippage_cost(entry_order, reference_entry)
    costs.exit_slippage = slippage_cost(exit_order, reference_exit)

    # The spread is paid once on each side: the difference between the
    # mid and where the order actually transacted.
    if reference_entry is not None and entry_order:
        price = entry_order.get("average_fill_price")
        if price is not None:
            costs.spread_paid += abs(price - reference_entry) * quantity
    if reference_exit is not None and exit_order:
        price = exit_order.get("average_fill_price")
        if price is not None:
            costs.spread_paid += abs(reference_exit - price) * quantity

    # Slippage and spread as measured here overlap: the fill price
    # already contains both, so the spread component is reduced by the
    # slippage already counted. Otherwise the breakdown would exceed the
    # actual cost and the report would overstate what execution cost.
    overlap = min(costs.spread_paid,
                  costs.entry_slippage + costs.exit_slippage)
    costs.spread_paid = max(0.0, costs.spread_paid - overlap)
    return costs


def record_closed_position(store, position, exit_order: Dict,
                           entry_order: Optional[Dict] = None,
                           intent=None,
                           session_date: str = "",
                           config_versions: Optional[Dict[str, str]] = None,
                           reference_entry: Optional[float] = None,
                           reference_exit: Optional[float] = None,
                           is_paper: bool = True) -> TradeRecord:
    """Write a completed trade to the journal."""
    if position is None:
        raise RecorderError("no position supplied")
    if not exit_order or not exit_order.get("average_fill_price"):
        raise RecorderError(
            f"cannot journal {getattr(position, 'symbol', '?')}: the exit "
            "order has no fill price, so the result is unknown")

    exit_price = exit_order["average_fill_price"]
    quantity = exit_order.get("filled_quantity") or position.quantity

    # The stop AS PLANNED AT ENTRY, not as it ended up. R-multiples must
    # be measured against the risk that was actually accepted when the
    # trade was taken; using the trailed stop would rewrite history and
    # make every trailing exit look like a 0R scratch.
    planned_stop = position.plan.stop_price
    for move in (position.stop_history or []):
        if move.get("from") is not None:
            planned_stop = move["from"]
            break

    reasons = [str(r) for r in (position.exit_reasons or [])]
    primary = (str(intent.primary_reason) if intent is not None
               else (reasons[0] if reasons else ""))

    trade = TradeRecord(
        trade_id=TradeRecord.make_id(),
        symbol=position.symbol,
        quantity=quantity,
        entry_price=position.entry_price,
        exit_price=exit_price,
        opened_at=position.opened_at,
        closed_at=utcnow(),
        planned_stop=planned_stop,
        planned_target=position.plan.target_price,
        strategy=getattr(position, "strategy", "") or "",
        hypothesis_id=position.hypothesis_id,
        risk_decision_id=position.risk_decision_id,
        exit_reason=primary,
        all_exit_reasons=reasons,
        entry_order_id=position.entry_order_id,
        exit_order_id=exit_order.get("order_id"),
        position_id=position.position_id,
        costs=costs_from_orders(entry_order, exit_order, quantity,
                                reference_entry, reference_exit),
        stop_history=list(position.stop_history or []),
        max_favourable_price=position.high_water_price,
        max_adverse_price=getattr(position, "low_water_price", None),
        config_versions=dict(config_versions or {}),
        is_paper=is_paper,
        session_date=session_date,
    )

    if trade.exceeded_planned_risk:
        # Not an error, but the most important thing the journal can
        # tell you, so it is surfaced rather than left to be noticed in
        # a report later.
        log_event("stop_breach_recorded", symbol=trade.symbol,
                  trade_id=trade.trade_id,
                  r_multiple=round(trade.r_multiple, 4),
                  planned_stop=round(trade.planned_stop, 4),
                  exit_price=round(trade.exit_price, 4),
                  alert=("the loss exceeded the planned risk; the stop did "
                         "not hold"))

    return store.record(trade)
