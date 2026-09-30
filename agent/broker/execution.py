"""
The only path from a decision to an order.

    TradeHypothesis -> RiskDecision (APPROVED) -> OrderProposal -> Broker

There is deliberately no shortcut. `submit_approved` refuses to act on a
decision that is not approved, refuses one whose hypothesis does not
match, and refuses when execution is not available - each as a separate
check, because a single combined condition is easier to weaken by
accident.

The client order id is DERIVED from the risk decision id. A retried
Lambda invocation therefore submits the same client id, the broker
suppresses the duplicate, and a retry cannot double a position.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..observability import log_event


class ExecutionRefused(Exception):
    """The path from decision to order was not open."""


@dataclass
class OrderProposal:
    """What an approved decision implies, before it becomes an order."""
    symbol: str
    side: str
    notional: float
    quantity: float
    reference_price: float
    order_type: str = "MARKETABLE_LIMIT"
    limit_price: Optional[float] = None
    time_in_force: str = "DAY"
    hypothesis_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    intent: str = "ENTRY"
    client_order_id: str = ""

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol, "side": self.side,
            "notional": round(self.notional, 2),
            "quantity": round(self.quantity, 6),
            "reference_price": round(self.reference_price, 4),
            "order_type": self.order_type,
            "limit_price": (round(self.limit_price, 4)
                            if self.limit_price else None),
            "time_in_force": self.time_in_force,
            "hypothesis_id": self.hypothesis_id,
            "risk_decision_id": self.risk_decision_id,
            "intent": self.intent,
            "client_order_id": self.client_order_id,
        }


# How far through the spread a marketable limit is willing to reach.
# Wide enough to fill, bounded enough that a sudden gap does not become
# an unlimited price.
MARKETABLE_LIMIT_BUFFER_PCT = 0.30


def build_proposal(decision, reference_price: float,
                   intent: str = "ENTRY") -> OrderProposal:
    """Turn an approved decision into a concrete proposal.

    The client order id is a hash of the risk decision id, so the same
    decision always produces the same id and a retry cannot place a
    second order.
    """
    if not getattr(decision, "approved", False):
        raise ExecutionRefused(
            "cannot build an order proposal from an unapproved decision")
    if reference_price is None or reference_price <= 0:
        raise ExecutionRefused("a reference price is required")

    notional = decision.capital_required
    quantity = round(notional / reference_price, 6)
    limit = reference_price * (1 + MARKETABLE_LIMIT_BUFFER_PCT / 100.0)

    digest = hashlib.sha256(
        f"{decision.decision_id}:{intent}".encode()).hexdigest()[:20]
    return OrderProposal(
        symbol=decision.symbol, side="BUY", notional=notional,
        quantity=quantity, reference_price=reference_price,
        order_type="MARKETABLE_LIMIT", limit_price=round(limit, 4),
        hypothesis_id=decision.hypothesis_id,
        risk_decision_id=decision.decision_id, intent=intent,
        client_order_id=f"cli_{digest}")


def submit_approved(broker, decision, hypothesis, reference_price: float,
                    execution_available: bool = False,
                    intent: str = "ENTRY") -> Dict:
    """The gate. Every condition is checked separately and explicitly."""
    if not execution_available:
        raise ExecutionRefused(
            "execution_available is false; no order may be submitted")
    if decision is None:
        raise ExecutionRefused("no risk decision supplied")
    if not getattr(decision, "approved", False):
        # This is a SECOND layer: build_proposal below refuses an
        # unapproved decision too. Both are kept deliberately, and the
        # wording differs so the trail records which one fired.
        codes = ", ".join(str(c) for c in getattr(decision, "reason_codes", []))
        raise ExecutionRefused(
            f"the risk decision is not approved: {codes}")
    if hypothesis is not None and decision.hypothesis_id != getattr(
            hypothesis, "hypothesis_id", None):
        # An approval is for ONE hypothesis. Reusing it for another is
        # how an approval for a good setup ends up executing a bad one.
        raise ExecutionRefused(
            "the risk decision does not correspond to this hypothesis")

    proposal = build_proposal(decision, reference_price, intent)
    log_event("order_proposed", symbol=proposal.symbol,
              notional=round(proposal.notional, 2),
              quantity=round(proposal.quantity, 6),
              risk_decision_id=proposal.risk_decision_id,
              client_order_id=proposal.client_order_id)

    return broker.submit_order(
        symbol=proposal.symbol, side=proposal.side,
        quantity=proposal.quantity, order_type=proposal.order_type,
        limit_price=proposal.limit_price,
        time_in_force=proposal.time_in_force,
        client_order_id=proposal.client_order_id,
        hypothesis_id=proposal.hypothesis_id,
        risk_decision_id=proposal.risk_decision_id,
        intent=proposal.intent)
