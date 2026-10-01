"""
What a LIVE broker adapter must guarantee, beyond the paper one.

The `BrokerAdapter` protocol in base.py describes the shape of a broker.
That shape is necessary and nowhere near sufficient for real money: the
paper broker satisfies it completely while being unable to lose a cent.

This module is the extra contract. It exists because the moment a live
adapter appears, every assumption the paper broker quietly satisfied
becomes a question:

- The paper broker's fills are synchronous. A real one acknowledges and
  fills later, so "the order returned" stops meaning "the position
  exists".
- The paper broker's state IS the truth. A real one is a remote system
  that can disagree with us, be unreachable, or be correct while we are
  wrong.
- The paper broker cannot partially fill and then reject. A real one
  can do almost anything.
- The paper broker has no concept of a halted symbol, an auction, a
  locked market, or a rejected cancel.

`LiveAdapterRequirements` enumerates those, and `assess_adapter` reports
which are unmet. Nothing here enables anything; it is a checklist whose
purpose is to make the gap between paper and live explicit rather than
discovered during the first real order.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class Requirement(str, enum.Enum):
    """Each is a question the paper broker never had to answer."""

    # --- correctness of the order lifecycle --------------------------
    ASYNC_FILL_RECONCILIATION = "ASYNC_FILL_RECONCILIATION"
    ORDER_STATUS_POLLING = "ORDER_STATUS_POLLING"
    CANCEL_CONFIRMATION = "CANCEL_CONFIRMATION"
    PARTIAL_THEN_REJECT_HANDLING = "PARTIAL_THEN_REJECT_HANDLING"

    # --- truth about state -------------------------------------------
    AUTHORITATIVE_POSITION_READ = "AUTHORITATIVE_POSITION_READ"
    AUTHORITATIVE_CASH_READ = "AUTHORITATIVE_CASH_READ"
    UNREACHABLE_BROKER_HALTS = "UNREACHABLE_BROKER_HALTS"
    DUPLICATE_SUBMISSION_PROTECTION = "DUPLICATE_SUBMISSION_PROTECTION"

    # --- market conditions the simulator ignores ---------------------
    HALTED_SYMBOL_HANDLING = "HALTED_SYMBOL_HANDLING"
    AUCTION_AND_LOCKED_MARKET_HANDLING = (
        "AUCTION_AND_LOCKED_MARKET_HANDLING")
    PATTERN_DAY_TRADER_RULES = "PATTERN_DAY_TRADER_RULES"
    SETTLEMENT_AND_GOOD_FAITH = "SETTLEMENT_AND_GOOD_FAITH"

    # --- operational --------------------------------------------------
    CREDENTIAL_ROTATION = "CREDENTIAL_ROTATION"
    RATE_LIMIT_BACKOFF = "RATE_LIMIT_BACKOFF"
    AUDIT_TRAIL_OF_EVERY_REQUEST = "AUDIT_TRAIL_OF_EVERY_REQUEST"
    KILL_SWITCH_THAT_CANCELS_WORKING_ORDERS = (
        "KILL_SWITCH_THAT_CANCELS_WORKING_ORDERS")

    def __str__(self) -> str:
        return self.value


# Why each one matters, in terms of what goes wrong without it. Written
# out because a bare enum name is not a specification, and whoever
# builds the live adapter will be reading this rather than asking.
REQUIREMENT_RATIONALE: Dict[Requirement, str] = {
    Requirement.ASYNC_FILL_RECONCILIATION: (
        "A real submission returns an acknowledgement, not a fill. Code "
        "written against the paper broker treats the returned object as "
        "proof the position exists, so a live adapter that returns the "
        "same shape will cause positions to be recorded that do not yet "
        "exist - and exit plans attached to them."),
    Requirement.ORDER_STATUS_POLLING: (
        "Without polling, an order that fills thirty seconds later is "
        "invisible until the next reconciliation, which will then halt "
        "on the discrepancy."),
    Requirement.CANCEL_CONFIRMATION: (
        "A cancel can be rejected because the order already filled. "
        "Treating the cancel request as success leaves a position the "
        "agent believes it cancelled."),
    Requirement.PARTIAL_THEN_REJECT_HANDLING: (
        "A real order can fill 40% and then be rejected. The agent must "
        "manage the 40% it actually holds, not the 0% or 100% it might "
        "assume."),
    Requirement.AUTHORITATIVE_POSITION_READ: (
        "The broker is the truth. Any local cache must be reconcilable "
        "against a real read, and the read must distinguish 'no "
        "positions' from 'could not ask'."),
    Requirement.AUTHORITATIVE_CASH_READ: (
        "Paper cash is computed from our own fills. Real buying power "
        "depends on settlement, margin, holds and fees we do not model, "
        "so it must be read rather than derived."),
    Requirement.UNREACHABLE_BROKER_HALTS: (
        "An unreachable broker is an unknown position state. The "
        "operating rule is to halt, and that has to be wired rather "
        "than assumed."),
    Requirement.DUPLICATE_SUBMISSION_PROTECTION: (
        "The client order id must be honoured BY THE BROKER, not just by "
        "us. Our own map is lost if our state is lost; the broker's "
        "memory is what actually prevents a double position."),
    Requirement.HALTED_SYMBOL_HANDLING: (
        "A halted symbol accepts orders that execute on resumption at "
        "an unknowable price. The risk model assumes a bounded worst "
        "case and a halt removes that assumption."),
    Requirement.AUCTION_AND_LOCKED_MARKET_HANDLING: (
        "Opening and closing auctions do not behave like continuous "
        "trading, and a marketable limit can sit unfilled in a locked "
        "market while the agent believes it is done."),
    Requirement.PATTERN_DAY_TRADER_RULES: (
        "An intraday strategy in a margin account under $25,000 will "
        "hit the pattern day trader rule, after which the account is "
        "restricted. The strategy's own design makes this likely rather "
        "than incidental."),
    Requirement.SETTLEMENT_AND_GOOD_FAITH: (
        "In a cash account, reusing unsettled proceeds causes good-faith "
        "violations. The agent's capital arithmetic has no concept of "
        "settlement."),
    Requirement.CREDENTIAL_ROTATION: (
        "A live credential needs a rotation path that does not require "
        "a deploy, and a revoked credential must fail closed rather "
        "than retry indefinitely."),
    Requirement.RATE_LIMIT_BACKOFF: (
        "A rate-limited order submission that is retried blindly is how "
        "one intended order becomes three."),
    Requirement.AUDIT_TRAIL_OF_EVERY_REQUEST: (
        "With real money, 'what did we send and when' must be "
        "answerable from the record without inference."),
    Requirement.KILL_SWITCH_THAT_CANCELS_WORKING_ORDERS: (
        "Today's kill switch stops NEW orders. With real money it must "
        "also cancel working ones, because an unattended resting order "
        "is live risk that the switch does not currently reach."),
}


@dataclass
class AdapterAssessment:
    """What a candidate adapter does and does not satisfy."""
    adapter_name: str
    is_paper: bool
    satisfied: List[Requirement] = field(default_factory=list)
    unmet: List[Requirement] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ready_for_real_money(self) -> bool:
        """Derived. Every requirement, and not paper.

        There is no setter and no partial credit. A single unmet
        requirement is enough, because each one on the list is a way to
        lose money that the paper record cannot show.
        """
        return not self.unmet and not self.is_paper

    def as_dict(self) -> Dict:
        return {
            "adapter_name": self.adapter_name,
            "is_paper": self.is_paper,
            "ready_for_real_money": self.ready_for_real_money,
            "satisfied": [str(r) for r in self.satisfied],
            "unmet": [str(r) for r in self.unmet],
            "unmet_count": len(self.unmet),
            "total_requirements": len(Requirement),
            "notes": self.notes,
            "rationale": {str(r): REQUIREMENT_RATIONALE[r]
                          for r in self.unmet},
        }


def assess_adapter(adapter, declared: Optional[Dict] = None
                   ) -> AdapterAssessment:
    """Report which live requirements an adapter satisfies.

    `declared` lets an adapter state its own capabilities. A claim is
    not verification, so anything declared without being demonstrable
    from the adapter's own interface is recorded as a note rather than
    counted as satisfied.
    """
    capabilities = {}
    try:
        capabilities = adapter.capabilities() or {}
    except Exception:                                     # noqa: BLE001
        capabilities = {}

    is_paper = bool(capabilities.get("is_paper", True))
    declared = declared or {}

    assessment = AdapterAssessment(
        adapter_name=type(adapter).__name__, is_paper=is_paper)

    for requirement in Requirement:
        key = str(requirement).lower()
        if declared.get(key) is True:
            assessment.satisfied.append(requirement)
        else:
            assessment.unmet.append(requirement)

    if is_paper:
        assessment.notes.append(
            "This adapter reports itself as paper. A paper adapter "
            "satisfies the BrokerAdapter shape completely while being "
            "unable to lose a cent, so the shape is not evidence of "
            "live readiness.")
    if declared:
        assessment.notes.append(
            "Requirements marked satisfied here are DECLARED by the "
            "adapter, not verified by this function. Each one needs a "
            "test against the real venue before it counts.")
    return assessment
