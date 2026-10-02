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
from datetime import datetime, timezone
from typing import Dict, Optional

from ..observability import log_event
from .order_ledger import ExternalOrderRecord, OrderLedgerError


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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _recover_or_record_intent(broker, ledger, proposal, session_date,
                              cohort, external, stamp):
    """Make the order durable, or recover one already sent.

    Returns a broker order dict if the order turns out to exist already,
    in which case the caller must NOT submit. Returns None when it is
    safe to submit.

    Every failure path here raises rather than returning None. An
    unreadable or unwritable ledger means the agent cannot tell whether
    an order exists, and submitting under that uncertainty is exactly how
    one order becomes two.
    """
    try:
        existing = ledger.get(proposal.client_order_id)
    except Exception as exc:                              # noqa: BLE001
        # Not "assume absent". An absent record and an unreadable one
        # look identical from here and mean opposite things.
        raise ExecutionRefused(
            f"the order ledger could not be read for "
            f"{proposal.client_order_id}, so it cannot be established "
            f"whether this order has already been sent: {exc}") from exc

    if existing is not None:
        lookup = getattr(broker, "find_by_client_order_id", None)
        if callable(lookup):
            try:
                found = lookup(proposal.client_order_id)
            except Exception as exc:                      # noqa: BLE001
                raise ExecutionRefused(
                    f"an intent already exists for "
                    f"{proposal.client_order_id} and the venue could not "
                    f"be queried to find out whether it was placed: "
                    f"{exc}") from exc
            if found is not None:
                # The order exists. This is the lost-response case: the
                # intent was written, the submission went out, and the
                # answer never arrived. Adopting the venue's order is
                # the whole purpose of a deterministic client id.
                log_event("order_recovered_by_client_id",
                          symbol=proposal.symbol,
                          client_order_id=proposal.client_order_id,
                          status=found.get("status"),
                          detail="intent existed; the venue already has "
                                 "this order, so it was not sent again")
                _record_submission(ledger, proposal, found, stamp,
                                   required=False)
                return found
            # `found is None` only where the adapter has CONFIRMED
            # absence - AlpacaPaperBroker cross-checks a 404 against the
            # order list for this exact reason. A confirmed absence means
            # the earlier attempt never reached the venue, so the same
            # id may be sent. No second logical order is created.
            log_event("order_intent_unsent",
                      symbol=proposal.symbol,
                      client_order_id=proposal.client_order_id,
                      detail="an intent existed but the venue confirms no "
                             "such order; resending the same id")
            return None
        if existing.submission_outcome_known:
            # No way to ask the venue, and the ledger says this order's
            # outcome was already observed. Submitting again would be a
            # duplicate on the strength of a record we already hold.
            raise ExecutionRefused(
                f"{proposal.client_order_id} has already been submitted "
                f"and observed (status {existing.status}); this broker "
                f"offers no client-id lookup, so resubmission is refused "
                f"rather than risked")
        return None

    record = ExternalOrderRecord(
        client_order_id=proposal.client_order_id,
        session_date=session_date or "",
        symbol=proposal.symbol,
        side=proposal.side,
        requested_quantity=proposal.quantity,
        requested_notional=proposal.notional,
        intent=proposal.intent,
        broker_environment=(getattr(broker, "base_url", None)
                            or getattr(broker, "name", None)),
        hypothesis_id=proposal.hypothesis_id,
        risk_decision_id=proposal.risk_decision_id,
        cohort=cohort,
        intent_at=stamp,
    )
    try:
        ledger.record_intent(record)
    except Exception as exc:                              # noqa: BLE001
        # THE RULE. The intent is written before the order is sent, and
        # if it cannot be written the order is not sent. Submitting
        # first and recording afterwards leaves a window in which a
        # crash produces a filled position that nothing in this system
        # has any record of asking for - which is precisely the orphan
        # this module exists to prevent.
        raise ExecutionRefused(
            f"the order intent for {proposal.client_order_id} could not "
            f"be recorded, so the order was NOT submitted: {exc}") from exc

    log_event("order_intent_recorded", symbol=proposal.symbol,
              client_order_id=proposal.client_order_id,
              session_date=session_date, external=external)
    return None


def _record_submission(ledger, proposal, order, stamp, required=False):
    """Fold the venue's answer into the record.

    Deliberately NOT fatal by default. By the time this runs the order
    has been accepted by the venue, so raising here would report a
    failure for an order that exists - and the caller's error handling
    would be about the wrong thing. The intent record is already durable
    and carries the client order id, so a lost observation is recoverable
    by polling. It is logged loudly because an unrecorded observation is
    a real integrity gap, just not one that unsends an order.
    """
    try:
        ledger.record_observation(proposal.client_order_id, order, stamp)
    except Exception as exc:                              # noqa: BLE001
        log_event("order_observation_not_recorded",
                  symbol=proposal.symbol,
                  client_order_id=proposal.client_order_id,
                  broker_order_id=order.get("order_id"),
                  status=order.get("status"),
                  error=str(exc),
                  detail="the order WAS submitted; its observation was "
                         "not stored and must be recovered by polling")
        if required:
            raise OrderLedgerError(
                f"could not record the observation for "
                f"{proposal.client_order_id}") from exc


EXIT_INTENT = "EXIT"

# Exit attempts beyond the first get a distinguishable intent, because a
# remainder left by a partial fill is a NEW logical order rather than a
# retry of the old one - and reusing the first id for it would be
# suppressed by the venue as a duplicate, leaving the remainder open.
MAX_EXIT_ATTEMPTS = 3


def exit_intent_name(attempt: int = 1) -> str:
    return EXIT_INTENT if attempt <= 1 else f"{EXIT_INTENT}#{attempt}"


def exit_client_order_id(risk_decision_id: str, attempt: int = 1) -> str:
    """The deterministic id for closing the position that decision opened.

    Derived the same way as the entry id, from the same decision, with
    the intent as the discriminator. A retry of the same attempt
    therefore produces the same id and the venue suppresses the
    duplicate, which is the whole reason exits move off
    DELETE /v2/positions - that endpoint accepts no client order id at
    all, so an exit sent through it could never be made idempotent and a
    lost response left no way to tell whether the position had been
    closed.
    """
    if not risk_decision_id:
        raise ExecutionRefused(
            "an exit through an external venue needs the risk decision id "
            "that opened the position, because the client order id is "
            "derived from it and without one the exit cannot be made "
            "idempotent")
    intent = exit_intent_name(attempt)
    digest = hashlib.sha256(
        f"{risk_decision_id}:{intent}".encode()).hexdigest()[:20]
    return f"cli_{digest}"


def build_exit_proposal(symbol: str, quantity: float, reference_price: float,
                        risk_decision_id: str, attempt: int = 1,
                        hypothesis_id=None) -> OrderProposal:
    """A marketable limit SELL that closes a position.

    The limit reaches DOWN through the spread by the same buffer an
    entry reaches up by: an exit that does not fill is worse than an
    exit that pays a little, because the position stays live.
    """
    if quantity is None or quantity <= 0:
        raise ExecutionRefused("an exit needs a positive quantity")
    if reference_price is None or reference_price <= 0:
        raise ExecutionRefused("an exit needs a reference price")
    if attempt < 1 or attempt > MAX_EXIT_ATTEMPTS:
        raise ExecutionRefused(
            f"exit attempt {attempt} is outside 1..{MAX_EXIT_ATTEMPTS}; "
            f"a position that cannot be closed in that many attempts is a "
            f"state for a human to look at, not one to keep retrying")
    limit = reference_price * (1 - MARKETABLE_LIMIT_BUFFER_PCT / 100.0)
    return OrderProposal(
        symbol=symbol, side="SELL", notional=quantity * reference_price,
        quantity=quantity, reference_price=reference_price,
        order_type="MARKETABLE_LIMIT", limit_price=round(limit, 4),
        hypothesis_id=hypothesis_id, risk_decision_id=risk_decision_id,
        intent=exit_intent_name(attempt),
        client_order_id=exit_client_order_id(risk_decision_id, attempt))


def submit_exit(broker, *, symbol: str, quantity: float,
                reference_price: float, risk_decision_id: str,
                ledger=None, session_date: Optional[str] = None,
                cohort: Optional[str] = None, attempt: int = 1,
                hypothesis_id=None, now: Optional[str] = None) -> Dict:
    """Close a position through the same discipline as opening one.

    Deliberately NOT gated on execution_available or on any halt: exits
    are always permitted, and a position that cannot be closed is
    unmanaged risk. The only things refused here are an exit that cannot
    be made durable or idempotent.
    """
    proposal = build_exit_proposal(symbol, quantity, reference_price,
                                   risk_decision_id, attempt, hypothesis_id)
    external = bool(getattr(broker, "is_external_venue", False))
    if external and ledger is None:
        raise ExecutionRefused(
            "this broker submits to an external venue and no order ledger "
            "was supplied; an exit order that is not durably recorded "
            "before it is sent cannot be recovered")
    if external and not session_date:
        raise ExecutionRefused(
            "a session_date is required to record an external exit order")

    log_event("exit_order_proposed", symbol=proposal.symbol,
              quantity=round(proposal.quantity, 6),
              limit_price=proposal.limit_price,
              client_order_id=proposal.client_order_id,
              intent=proposal.intent, attempt=attempt)

    stamp = now or _utcnow()
    if ledger is not None:
        already = _recover_or_record_intent(
            broker, ledger, proposal, session_date, cohort, external, stamp)
        if already is not None:
            return already

    order = broker.submit_order(
        symbol=proposal.symbol, side=proposal.side,
        quantity=proposal.quantity, order_type=proposal.order_type,
        limit_price=proposal.limit_price,
        time_in_force=proposal.time_in_force,
        client_order_id=proposal.client_order_id,
        hypothesis_id=proposal.hypothesis_id,
        risk_decision_id=proposal.risk_decision_id,
        intent=proposal.intent)

    if ledger is not None and isinstance(order, dict):
        _record_submission(ledger, proposal, order, stamp)
    return order


def submit_approved(broker, decision, hypothesis, reference_price: float,
                    execution_available: bool = False,
                    intent: str = "ENTRY",
                    ledger=None,
                    session_date: Optional[str] = None,
                    cohort: Optional[str] = None,
                    now: Optional[str] = None) -> Dict:
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

    external = bool(getattr(broker, "is_external_venue", False))
    if external and ledger is None:
        # An order sent over the network and recorded nowhere is an
        # orphan waiting for a crash. The simulator is exempt because
        # its orders are serialised with its own state.
        raise ExecutionRefused(
            "this broker submits to an external venue and no order "
            "ledger was supplied; an order that is not durably recorded "
            "before it is sent cannot be recovered")
    if external and not session_date:
        raise ExecutionRefused(
            "a session_date is required to record an external order")

    stamp = now or _utcnow()
    if ledger is not None:
        already = _recover_or_record_intent(
            broker, ledger, proposal, session_date, cohort, external, stamp)
        if already is not None:
            return already

    # Intent is durable from here. Anything that goes wrong below leaves
    # a record whose outcome is unknown, which is the state polling and
    # reconciliation are built to resolve.
    order = broker.submit_order(
        symbol=proposal.symbol, side=proposal.side,
        quantity=proposal.quantity, order_type=proposal.order_type,
        limit_price=proposal.limit_price,
        time_in_force=proposal.time_in_force,
        client_order_id=proposal.client_order_id,
        hypothesis_id=proposal.hypothesis_id,
        risk_decision_id=proposal.risk_decision_id,
        intent=proposal.intent)

    if ledger is not None and isinstance(order, dict):
        _record_submission(ledger, proposal, order, stamp)
    return order
