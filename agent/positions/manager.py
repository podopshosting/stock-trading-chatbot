"""
The position manager.

Owns the lifecycle: open a managed position from a filled entry order,
reconcile against the broker, evaluate exits, submit them.

The broker is authoritative. This class holds a cache of what the broker
said, and any disagreement is treated as a reason to stop trading rather
than something to paper over. Both ways a disagreement arises - an order
that filled without being recorded, and a position closed outside the
agent - mean the agent's risk arithmetic is wrong, and risk arithmetic
that is wrong in an unknown direction cannot be corrected by guessing.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..observability import log_event

# Mirrors agent/broker/execution.MAX_EXIT_ATTEMPTS. Duplicated
# rather than imported because this module may not reach the
# submission path - see exit_submitter. Pinned by a test.
MAX_EXIT_ATTEMPTS = 3
from . import exits
from .models import (
    ExitIntent, ExitPlan, ExitReason, ManagedPosition, PositionState,
    ReconciliationResult, StopMechanism,
)

QUANTITY_TOLERANCE = 1e-6


class PositionManagerError(Exception):
    pass


class PositionManager:
    """Tracks managed positions and drives their exits."""

    def __init__(self, broker=None, execution_available: bool = False,
                 order_ledger=None, session_date: Optional[str] = None,
                 cohort: Optional[str] = None, exit_submitter=None):
        self.broker = broker
        self.execution_available = execution_available
        # Supplied where the venue is external, so an exit order is
        # recorded before it is sent. See docs/INTENT-BEFORE-SUBMIT.md.
        self.order_ledger = order_ledger
        self.session_date = session_date
        self.cohort = cohort
        # INJECTED, never imported. agent/broker/execution.py is the
        # submission path, and the read API must not be able to reach it
        # - a test asserts that importing the API handler loads no broker
        # and no orchestrator. Importing submit_exit here broke that, and
        # a lazy import would still leave it reachable. The orchestrator
        # supplies it; the API never does, so the API cannot submit.
        self.exit_submitter = exit_submitter
        self._positions: Dict[str, ManagedPosition] = {}   # by symbol
        self._closed: List[ManagedPosition] = []
        self._halted: bool = False
        self._halt_detail: str = ""

    # --- opening ---------------------------------------------------------

    def open_from_order(self, order: Dict, plan: ExitPlan,
                        hypothesis_id: Optional[str] = None,
                        risk_decision_id: Optional[str] = None,
                        config_version: str = "") -> ManagedPosition:
        """Record a managed position from a filled entry order."""
        if order is None:
            raise PositionManagerError("no order supplied")
        symbol = order.get("symbol")
        filled = order.get("filled_quantity") or 0.0
        price = order.get("average_fill_price")

        if filled <= 0 or price is None:
            raise PositionManagerError(
                f"cannot manage an unfilled order for {symbol}")
        if symbol in self._positions:
            # Averaging down is forbidden by the risk limits, and a
            # second entry in the same name would also break the
            # one-plan-per-position model the exit engine assumes.
            raise PositionManagerError(
                f"a managed position in {symbol} already exists")
        if plan.stop_price >= price:
            raise PositionManagerError(
                f"the stop {plan.stop_price} is not below the entry {price}")

        # A partially filled entry leaves a remainder still working. If
        # that remainder fills later the broker's quantity diverges from
        # the agent's and reconciliation halts everything over what is a
        # routine partial fill - and open_from_order refuses a second
        # position in the same name, so the remainder could never be
        # managed anyway. Cancel it, so the managed position is exactly
        # what is held.
        remaining = order.get("remaining_quantity") or 0.0
        if remaining > 0 and order.get("order_id"):
            if self.broker is None:
                raise PositionManagerError(
                    f"{symbol} entry is partially filled with {remaining} "
                    "still working and there is no broker to cancel it")
            try:
                self.broker.cancel_order(order["order_id"])
                log_event("entry_remainder_cancelled", symbol=symbol,
                          order_id=order["order_id"],
                          cancelled_quantity=round(remaining, 6),
                          filled_quantity=round(filled, 6))
            except Exception as exc:                    # noqa: BLE001
                # An uncancellable remainder is an unknown future
                # position. Fail closed rather than manage a position
                # whose size may change underneath us.
                raise PositionManagerError(
                    f"could not cancel the unfilled remainder of the "
                    f"{symbol} entry ({remaining}): {exc}") from exc

        position = ManagedPosition(
            position_id=ManagedPosition.make_id(),
            symbol=symbol,
            quantity=filled,
            entry_price=price,
            plan=plan,
            high_water_price=price,
            current_price=price,
            hypothesis_id=hypothesis_id,
            risk_decision_id=risk_decision_id,
            entry_order_id=order.get("order_id"),
            config_version=config_version,
        )
        self._positions[symbol] = position
        log_event("position_opened", symbol=symbol,
                  position_id=position.position_id,
                  quantity=round(filled, 6), entry=round(price, 4),
                  stop=round(plan.stop_price, 4),
                  risk_per_share=round(position.risk_per_share, 4),
                  stop_mechanism=str(plan.stop_mechanism),
                  hypothesis_id=hypothesis_id)
        return position

    # --- reading ---------------------------------------------------------

    def get(self, symbol: str) -> Optional[ManagedPosition]:
        return self._positions.get(symbol)

    def open_positions(self) -> List[ManagedPosition]:
        return [p for p in self._positions.values()
                if p.state is not PositionState.CLOSED]

    def closed_positions(self) -> List[ManagedPosition]:
        return list(self._closed)

    @property
    def open_count(self) -> int:
        return len(self.open_positions())

    @property
    def halted(self) -> bool:
        return self._halted

    def total_open_risk(self) -> Optional[float]:
        """Sum of what every open position can still lose.

        None if any position cannot be priced, because a total that
        silently omits an unpriceable position understates risk, and an
        understated risk total is worse than no total.
        """
        total = 0.0
        for position in self.open_positions():
            risk = position.open_risk
            if risk is None:
                return None
            total += risk
        return total

    def total_unrealized_pnl(self) -> Optional[float]:
        total = 0.0
        for position in self.open_positions():
            pnl = position.unrealized_pnl
            if pnl is None:
                return None
            total += pnl
        return total

    # --- reconciliation --------------------------------------------------

    def reconcile(self) -> ReconciliationResult:
        """Compare the agent's view with the broker's.

        Divergence halts the manager. It is not repaired automatically:
        adopting the broker's numbers would discard the plan attached to
        the position, and adopting the agent's would ignore reality.
        """
        if self.broker is None:
            result = ReconciliationResult(
                matched=False, unreadable=True,
                detail="no broker attached; brokerage state is unknown")
            self._engage_halt(result.detail)
            return result

        try:
            broker_positions = {p["symbol"]: p
                                for p in self.broker.get_positions()}
        except Exception as exc:                        # noqa: BLE001
            # An unreadable broker is an unknown brokerage state, which
            # the operating rules treat as a halt condition.
            result = ReconciliationResult(
                matched=False, unreadable=True,
                detail=f"could not read broker positions: {exc}")
            self._engage_halt(result.detail)
            return result

        agent_symbols = {p.symbol for p in self.open_positions()}
        broker_symbols = set(broker_positions)

        agent_only = sorted(agent_symbols - broker_symbols)
        broker_only = sorted(broker_symbols - agent_symbols)
        mismatches = []
        for symbol in sorted(agent_symbols & broker_symbols):
            mine = self._positions[symbol].quantity
            theirs = broker_positions[symbol]["quantity"]
            if abs(mine - theirs) > QUANTITY_TOLERANCE:
                mismatches.append({"symbol": symbol, "agent": mine,
                                   "broker": theirs})

        matched = not (agent_only or broker_only or mismatches)
        detail = ""
        if not matched:
            parts = []
            if agent_only:
                parts.append(f"agent holds {agent_only} the broker does not")
            if broker_only:
                parts.append(f"broker holds {broker_only} the agent does not")
            if mismatches:
                parts.append(f"quantity mismatches: {mismatches}")
            detail = "; ".join(parts)
            for symbol in agent_only:
                self._positions[symbol].state = PositionState.UNKNOWN
            self._engage_halt(detail)

        result = ReconciliationResult(
            matched=matched, agent_only=agent_only, broker_only=broker_only,
            quantity_mismatches=mismatches, detail=detail)
        log_event("reconciliation", matched=matched,
                  agent_only=agent_only, broker_only=broker_only,
                  mismatches=len(mismatches), detail=detail)
        return result

    def _engage_halt(self, detail: str) -> None:
        self._halted = True
        self._halt_detail = detail
        log_event("position_manager_halted", detail=detail)

    # --- exits -----------------------------------------------------------

    def evaluate_exits(self, contexts: Dict[str, exits.ExitContext]
                       ) -> List[ExitIntent]:
        """Evaluate every open position. Returns the intents raised.

        A position with no context supplied is evaluated with an empty
        one, which the exit engine treats as an unpriceable position and
        therefore an exit. Skipping it instead would let a position with
        no data quietly escape its stop.
        """
        intents = []
        for position in self.open_positions():
            context = contexts.get(position.symbol)
            if context is None:
                context = exits.ExitContext()
            if self._halted:
                context.broker_divergence = True
            intent = exits.evaluate(position, context)
            if intent is not None:
                position.state = PositionState.EXITING
                intents.append(intent)
        return intents

    def submit_exit(self, intent: ExitIntent) -> Dict:
        """Send an exit to the broker.

        `execution_available` gates this, as it gates entries - but an
        open position that cannot be closed is a distinct and more
        serious condition than an entry that cannot be opened, so it is
        logged as an alert rather than a routine refusal.
        """
        position = self._positions.get(intent.symbol)
        if position is None:
            raise PositionManagerError(
                f"no managed position in {intent.symbol}")

        if not self.execution_available:
            log_event("exit_blocked_execution_unavailable",
                      symbol=intent.symbol,
                      position_id=position.position_id,
                      primary_reason=str(intent.primary_reason),
                      protective=intent.protective,
                      alert=("an open position cannot be closed; risk is "
                             "live and unmanaged"))
            raise PositionManagerError(
                "execution_available is false; the exit cannot be submitted "
                "and the position remains at risk")

        if self.broker is None:
            raise PositionManagerError("no broker attached")

        order = self._submit_exit_order(position, intent)
        position.exit_order_id = order.get("order_id")
        cli = order.get("client_order_id")
        if cli and cli not in position.exit_client_order_ids:
            # Counted only once per logical order. A retry that the
            # venue suppressed returns the SAME client order id, so it
            # must not advance the attempt number - advancing it would
            # mint a new id on the next try and defeat the suppression.
            position.exit_client_order_ids.append(cli)
            position.exit_attempts += 1
        if (order.get("status") == "FILLED"
                and order.get("filled_quantity", 0) > 0):
            self._close(position, intent)
        log_event("exit_submitted", symbol=intent.symbol,
                  position_id=position.position_id,
                  order_id=order.get("order_id"),
                  status=order.get("status"),
                  primary_reason=str(intent.primary_reason))
        return order

    def _exit_attempt_for(self, position: ManagedPosition) -> int:
        """Which exit attempt this submission belongs to.

        Reuses the CURRENT attempt by default, because a resend is a
        retry and a retry must carry the same deterministic id or the
        venue cannot suppress it. Advancing unconditionally - which is
        what this did first - meant a retry was never a retry: every
        resend derived a new id, the venue accepted it as a second
        order, and the mechanism built to prevent a double sell would
        have been what caused one. The 218-mutation gate found it.

        It advances only on evidence: the previous attempt's ledger row
        is terminal with a known outcome AND the position still holds
        quantity, so there is a genuine remainder that the suppressed
        id can never close. Without a readable ledger it does NOT
        advance, because an unverifiable remainder is not a reason to
        put a second live order into the market.
        """
        attempts = position.exit_attempts or 0
        if attempts < 1:
            return 1
        if self.order_ledger is None or not position.exit_client_order_ids:
            return attempts
        try:
            row = self.order_ledger.get(position.exit_client_order_ids[-1],
                                        session_date=self.session_date)
        except Exception:                                 # noqa: BLE001
            return attempts          # cannot establish it; do not advance
        if row is None:
            return attempts
        if not (getattr(row, "is_terminal", False)
                and getattr(row, "submission_outcome_known", False)):
            return attempts
        remaining = (position.quantity or 0.0) - (row.filled_quantity or 0.0)
        if remaining <= 0:
            return attempts
        return min(attempts + 1, MAX_EXIT_ATTEMPTS)

    def _submit_exit_order(self, position: ManagedPosition,
                           intent: ExitIntent) -> Dict:
        """Send the exit, durably where the venue is external.

        An external exit goes through submit_order with a deterministic
        client order id rather than DELETE /v2/positions, which accepts
        no client order id and so could never be idempotent: a lost
        response left no way to tell whether the position had been
        closed, and the only safe options were to leave it open or risk
        closing it twice.

        The in-process simulator keeps close_position. Its orders are
        serialised with its own state, so the durability the ledger adds
        is already there.
        """
        external = bool(getattr(self.broker, "is_external_venue", False))
        if (not external or self.order_ledger is None
                or self.exit_submitter is None):
            return self.broker.close_position(
                intent.symbol, intent=str(intent.primary_reason))

        reference = intent.reference_price or position.entry_price
        return self.exit_submitter(
            self.broker, symbol=intent.symbol,
            quantity=intent.quantity or position.quantity,
            reference_price=reference,
            risk_decision_id=position.risk_decision_id,
            ledger=self.order_ledger, session_date=self.session_date,
            cohort=self.cohort, attempt=self._exit_attempt_for(position),
            hypothesis_id=position.hypothesis_id)

    def _close(self, position: ManagedPosition,
               intent: Optional[ExitIntent] = None) -> None:
        position.state = PositionState.CLOSED
        if intent is not None:
            position.exit_reasons = intent.all_reasons
        self._closed.append(position)
        self._positions.pop(position.symbol, None)
        log_event("position_closed_managed", symbol=position.symbol,
                  position_id=position.position_id,
                  reasons=[str(r) for r in position.exit_reasons])

    def flatten_all(self, reason: ExitReason = ExitReason.MANUAL,
                    detail: str = "") -> List[ExitIntent]:
        """Close everything. Used at the close and on a halt.

        Builds intents unconditionally rather than evaluating rules: a
        flatten is an instruction, not a judgement.
        """
        intents = []
        for position in self.open_positions():
            intent = ExitIntent(
                intent_id=ExitIntent.make_id(),
                position_id=position.position_id,
                symbol=position.symbol,
                quantity=position.quantity,
                primary_reason=reason,
                all_reasons=[reason],
                detail=detail or "flatten requested",
                reference_price=position.current_price,
                protective=True,
                config_version=exits.CONFIG_VERSION,
            )
            position.state = PositionState.EXITING
            intents.append(intent)
        log_event("flatten_all", count=len(intents), reason=str(reason))
        return intents

    def snapshot(self) -> Dict:
        return {
            "open_count": self.open_count,
            "halted": self._halted,
            "halt_detail": self._halt_detail,
            "execution_available": self.execution_available,
            "total_open_risk": self.total_open_risk(),
            "total_unrealized_pnl": self.total_unrealized_pnl(),
            "positions": [p.as_dict() for p in self.open_positions()],
            "closed_today": len(self._closed),
        }
