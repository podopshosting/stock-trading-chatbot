"""
The market-day orchestrator.

One cycle of work, designed to be invoked repeatedly by a scheduler and
to be safe when that scheduler misbehaves - which it will. EventBridge
delivers at-least-once, Lambdas time out halfway through, and two
invocations can overlap.

The ordering rule is the whole design:

    reconcile -> exits -> entries

Reconciliation first, because acting on a position set that disagrees
with the broker is acting on fiction. Exits second, because if the
process dies at any point after that, risk has already been reduced.
Entries last, because they are the only step that ADDS risk, and they
are the only step that is safe to skip entirely.

Every failure path in this file leads to the same place: exits continue,
entries stop. There is no failure mode in which the agent keeps opening
positions while something is wrong.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence

from ..broker.execution import ExecutionRefused, submit_approved
from ..journal import record_closed_position
from ..observability import log_event
from ..positions import ExitContext, ExitPlan, ExitReason, StopMechanism
from ..risk import RiskContext, RiskLimits, evaluate as evaluate_risk
from .models import (
    CycleOutcome, CyclePhase, CycleResult, HaltReason,
)

CONFIG_VERSION = "orchestration-v1.0.0"

# The opening minutes are excluded from entries: spreads are widest and
# the first print is often not a price anyone could have traded.
OPENING_MINUTES = 5
# Entries stop this long before the close, because a position opened
# later cannot be given time to work before it must be flattened.
PRE_CLOSE_MINUTES = 30


def resolve_phase(market_status: str,
                  minutes_since_open: Optional[float] = None,
                  minutes_to_close: Optional[float] = None,
                  opening_minutes: int = OPENING_MINUTES,
                  pre_close_minutes: int = PRE_CLOSE_MINUTES) -> CyclePhase:
    """Map market status and clock position onto a phase.

    An unknown market status is never treated as open. A provider that
    cannot tell us whether the market is trading is not evidence that it
    is.
    """
    status = (market_status or "UNKNOWN").upper()
    if status == "PRE_MARKET":
        return CyclePhase.PRE_MARKET
    if status in ("CLOSED", "AFTER_HOURS"):
        return CyclePhase.CLOSED
    if status != "OPEN":
        return CyclePhase.UNKNOWN

    # Open. Where in the session?
    if minutes_to_close is None:
        # Open but we cannot tell how long is left. Exits are safe;
        # entries are not, because the flatten window cannot be
        # respected.
        return CyclePhase.PRE_CLOSE
    if minutes_to_close <= pre_close_minutes:
        return CyclePhase.PRE_CLOSE
    if minutes_since_open is not None and minutes_since_open < opening_minutes:
        return CyclePhase.OPENING
    return CyclePhase.INTRADAY


class MarketDayOrchestrator:
    """Runs one cycle of the trading day.

    Dependencies are injected rather than constructed, so a cycle can be
    driven over historical data by the replay harness and over live data
    by the scheduler using the same code.
    """

    def __init__(self, *, broker, position_manager, journal,
                 halt_store=None, limits: Optional[RiskLimits] = None,
                 trading_enabled: bool = False,
                 execution_available: bool = False,
                 cycle_lock=None):
        self.broker = broker
        self.positions = position_manager
        self.journal = journal
        self.halt_store = halt_store
        self.limits = limits or RiskLimits()
        self.trading_enabled = trading_enabled
        self.execution_available = execution_available
        self.cycle_lock = cycle_lock
        self._entry_orders: Dict[str, Dict] = {}

    # --- the cycle -------------------------------------------------------

    def run_cycle(self, *, session_date: str, phase: CyclePhase,
                  candidates: Optional[Sequence[str]] = None,
                  quote_for: Optional[Callable] = None,
                  hypothesis_for: Optional[Callable] = None,
                  minutes_to_close: Optional[float] = None,
                  capital_deployed: float = 0.0,
                  realized_pnl_today: float = 0.0,
                  positions_opened_today: int = 0,
                  cycle_id: Optional[str] = None) -> CycleResult:
        """Run one cycle. Never raises; failures become halts."""
        result = CycleResult(
            cycle_id=cycle_id or CycleResult.make_id(),
            session_date=session_date, phase=phase,
            outcome=CycleOutcome.COMPLETED,
            config_version=CONFIG_VERSION)

        # --- 0. do not run twice at once ---------------------------------
        if not self._acquire_lock(result):
            result.outcome = CycleOutcome.SKIPPED_DUPLICATE
            result.halt(HaltReason.CONCURRENT_CYCLE,
                        "another cycle holds the lock")
            result.finished_at = _now()
            log_event("cycle_skipped_duplicate",
                      cycle_id=result.cycle_id, session_date=session_date)
            return result

        try:
            self._check_switches(result)
            self._check_global_halt(result)

            # --- 1. reconcile --------------------------------------------
            self._reconcile(result)

            # --- 2. exits, ALWAYS before entries -------------------------
            self._manage_exits(result, phase, quote_for, minutes_to_close,
                               session_date)

            # --- 3. entries, only if everything is in order --------------
            if not result.new_exposure_permitted:
                result.add_step(
                    "consider_entries", ok=True, skipped=True,
                    skip_reason=(
                        f"phase {phase} permits no new exposure"
                        if not phase.permits_new_exposure
                        else "halted: " + ", ".join(
                            str(r) for r in result.halt_reasons)))
            else:
                self._consider_entries(
                    result, candidates or [], quote_for, hypothesis_for,
                    session_date, capital_deployed, realized_pnl_today,
                    positions_opened_today, minutes_to_close)

        except Exception as exc:                          # noqa: BLE001
            # An unhandled error must not leave the agent believing it
            # may trade. Exits above have already run.
            result.halt(HaltReason.UNHANDLED_ERROR, str(exc))
            result.outcome = CycleOutcome.ABORTED
            result.errors.append(f"unhandled: {exc}")
            log_event("cycle_aborted", cycle_id=result.cycle_id,
                      detail=str(exc))
        finally:
            self._release_lock(result)

        result.open_positions = self.positions.open_count
        result.capital_deployed = capital_deployed
        result.finished_at = _now()

        if result.outcome is CycleOutcome.COMPLETED:
            if result.halted:
                result.outcome = CycleOutcome.HALTED
            elif result.errors:
                result.outcome = CycleOutcome.COMPLETED_WITH_ERRORS

        log_event("cycle_complete", cycle_id=result.cycle_id,
                  session_date=session_date, phase=str(phase),
                  outcome=str(result.outcome),
                  risk_was_managed=result.risk_was_managed,
                  exits_submitted=result.exits_submitted,
                  entries_submitted=result.entries_submitted,
                  halt_reasons=[str(r) for r in result.halt_reasons])
        return result

    # --- steps -----------------------------------------------------------

    def _acquire_lock(self, result: CycleResult) -> bool:
        """Prevent two overlapping cycles from double-entering.

        Absence of a lock is permitted but recorded: a single-threaded
        scheduler does not need one, and requiring it would block the
        replay harness for no safety gain.
        """
        if self.cycle_lock is None:
            result.add_step("acquire_lock", ok=True, skipped=True,
                            skip_reason="no cycle lock configured")
            return True
        try:
            acquired = self.cycle_lock.acquire(result.cycle_id)
        except Exception as exc:                          # noqa: BLE001
            # Cannot tell whether another cycle is running. Two cycles
            # entering the same position is worse than skipping one.
            result.add_step("acquire_lock", ok=False,
                            detail=f"lock unreadable: {exc}")
            return False
        result.add_step("acquire_lock", ok=acquired,
                        detail="" if acquired else "already held")
        return acquired

    def _release_lock(self, result: CycleResult) -> None:
        if self.cycle_lock is None:
            return
        try:
            self.cycle_lock.release(result.cycle_id)
        except Exception as exc:                          # noqa: BLE001
            result.errors.append(f"lock release failed: {exc}")

    def _check_switches(self, result: CycleResult) -> None:
        if not self.trading_enabled:
            result.halt(HaltReason.TRADING_DISABLED,
                        "trading_enabled is false")
        if not self.execution_available:
            result.halt(HaltReason.EXECUTION_UNAVAILABLE,
                        "execution_available is false")
        result.add_step("check_switches", ok=True,
                        detail=(f"trading_enabled={self.trading_enabled} "
                                f"execution_available="
                                f"{self.execution_available}"))

    def _check_global_halt(self, result: CycleResult) -> None:
        if self.halt_store is None:
            result.add_step("check_global_halt", ok=True, skipped=True,
                            skip_reason="no halt store configured")
            return
        try:
            state = self.halt_store.get()
        except Exception as exc:                          # noqa: BLE001
            # An operator may have halted trading and we cannot see it.
            result.halt(HaltReason.HALT_STATE_UNREADABLE, str(exc))
            result.add_step("check_global_halt", ok=False,
                            detail=f"unreadable, assuming halted: {exc}")
            return
        halted = bool(getattr(state, "halted", False))
        if halted:
            result.halt(HaltReason.GLOBAL_HALT,
                        getattr(state, "reason", "") or "global halt set")
        result.add_step("check_global_halt", ok=True,
                        detail=f"halted={halted}")

    def _reconcile(self, result: CycleResult) -> None:
        """Acting on a position set that disagrees with the broker is
        acting on fiction, so this runs before anything else."""
        try:
            reconciliation = self.positions.reconcile()
        except Exception as exc:                          # noqa: BLE001
            result.halt(HaltReason.BROKER_DIVERGENCE,
                        f"reconciliation failed: {exc}")
            result.add_step("reconcile", ok=False, detail=str(exc))
            return
        result.positions_reconciled = reconciliation.matched
        if not reconciliation.matched:
            result.halt(HaltReason.BROKER_DIVERGENCE, reconciliation.detail)
        result.add_step("reconcile", ok=reconciliation.matched,
                        detail=reconciliation.detail)

    def _manage_exits(self, result: CycleResult, phase: CyclePhase,
                      quote_for, minutes_to_close, session_date) -> None:
        """Evaluate and submit exits.

        Runs even when halted, and even when entries are forbidden. The
        only thing that stops it is a phase in which the market cannot
        be reached.
        """
        if not phase.permits_exits:
            result.add_step("manage_exits", ok=True, skipped=True,
                            skip_reason=f"phase {phase} cannot reach market")
            return

        open_positions = self.positions.open_positions()
        if not open_positions:
            result.add_step("manage_exits", ok=True, skipped=True,
                            skip_reason="no open positions")
            return

        halted = result.halted
        contexts = {}
        for position in open_positions:
            quote = _safe_call(quote_for, position.symbol)
            price = None if quote is None else quote.get("price")
            age = None if quote is None else quote.get("age_seconds")
            contexts[position.symbol] = ExitContext(
                price=price, quote_age_seconds=age,
                market_session="OPEN", minutes_to_close=minutes_to_close,
                global_halt=halted,
                halt_state_readable=(
                    HaltReason.HALT_STATE_UNREADABLE not in
                    result.halt_reasons),
                daily_loss_breached=(
                    HaltReason.DAILY_LOSS_LIMIT in result.halt_reasons),
                broker_divergence=(
                    HaltReason.BROKER_DIVERGENCE in result.halt_reasons),
                now=_now())

        try:
            intents = self.positions.evaluate_exits(contexts)
        except Exception as exc:                          # noqa: BLE001
            result.halt(HaltReason.UNHANDLED_ERROR,
                        f"exit evaluation failed: {exc}")
            result.add_step("manage_exits", ok=False, detail=str(exc))
            return

        if phase.requires_flatten and not intents:
            intents = self.positions.flatten_all(
                reason=ExitReason.END_OF_DAY,
                detail="pre-close flatten; no overnight gap risk")

        result.exits_evaluated = len(intents)
        failures = []
        for intent in intents:
            position = self.positions.get(intent.symbol)
            if position is None:
                continue
            try:
                order = self.positions.submit_exit(intent)
            except Exception as exc:                      # noqa: BLE001
                # An open position that cannot be closed is the most
                # serious condition this system has.
                failures.append(f"{intent.symbol}: {exc}")
                result.exits_failed += 1
                log_event("exit_failed_position_at_risk",
                          symbol=intent.symbol,
                          position_id=position.position_id,
                          detail=str(exc),
                          alert="risk is live and unmanaged")
                continue
            result.exits_submitted += 1
            if (order.get("filled_quantity") or 0) > 0:
                self._journal(result, position, order, intent, session_date)

        ok = not failures
        result.add_step("manage_exits", ok=ok,
                        detail="; ".join(failures) if failures else
                        f"{result.exits_submitted} exit(s) submitted")
        if failures:
            result.errors.extend(failures)

    def _journal(self, result, position, order, intent, session_date) -> None:
        try:
            record_closed_position(
                self.journal, position, order,
                entry_order=self._entry_orders.get(position.symbol),
                intent=intent, session_date=session_date,
                config_versions={"orchestration": CONFIG_VERSION},
                is_paper=not getattr(self.broker, "is_live", False))
            result.trades_journalled += 1
            self._entry_orders.pop(position.symbol, None)
        except Exception as exc:                          # noqa: BLE001
            # A journalling failure must not be mistaken for an exit
            # failure: the position IS closed, the record is missing.
            result.errors.append(
                f"journal failed for {position.symbol} (the position is "
                f"closed; only the record is missing): {exc}")
            log_event("journal_failed_after_exit", symbol=position.symbol,
                      detail=str(exc))

    def _consider_entries(self, result: CycleResult, candidates, quote_for,
                          hypothesis_for, session_date, capital_deployed,
                          realized_pnl_today, positions_opened_today,
                          minutes_to_close) -> None:
        """Scan, decide, enter. The only step that adds risk."""
        held = {p.symbol for p in self.positions.open_positions()}
        deployed = capital_deployed
        opened = positions_opened_today
        failures: List[str] = []

        for symbol in candidates:
            result.symbols_scanned += 1
            if symbol in held:
                continue
            if self.positions.open_count >= self.limits.max_concurrent_positions:
                break
            if opened >= self.limits.max_new_positions_per_day:
                break

            hypothesis = _safe_call(hypothesis_for, symbol)
            if hypothesis is None:
                continue
            result.hypotheses_generated += 1

            quote = _safe_call(quote_for, symbol)
            context = RiskContext(
                session_date=session_date, market_session="OPEN",
                minutes_to_close=minutes_to_close,
                trading_enabled=self.trading_enabled,
                execution_available=self.execution_available,
                price=None if quote is None else quote.get("price"),
                spread_pct=None if quote is None else quote.get("spread_pct"),
                dollar_volume=(None if quote is None
                               else quote.get("dollar_volume")),
                quote_age_seconds=(None if quote is None
                                   else quote.get("age_seconds")),
                capital_deployed_today=deployed,
                realized_pnl_today=realized_pnl_today,
                open_positions=self.positions.open_count,
                positions_opened_today=opened,
                held_symbols=sorted(held))

            decision = evaluate_risk(hypothesis, context, limits=self.limits)
            if not decision.approved:
                continue
            result.decisions_approved += 1

            try:
                order = submit_approved(
                    self.broker, decision, hypothesis,
                    reference_price=context.price,
                    execution_available=self.execution_available)
            except ExecutionRefused as exc:
                failures.append(f"{symbol}: {exc}")
                result.entries_failed += 1
                continue
            except Exception as exc:                      # noqa: BLE001
                failures.append(f"{symbol}: {exc}")
                result.entries_failed += 1
                continue

            if (order.get("filled_quantity") or 0) <= 0:
                continue

            result.entries_submitted += 1
            deployed += decision.capital_required
            opened += 1
            held.add(symbol)
            self._entry_orders[symbol] = order
            self._open_position(result, order, decision, hypothesis)

        result.add_step("consider_entries", ok=not failures,
                        detail="; ".join(failures) if failures else
                        f"{result.entries_submitted} entry(ies) submitted")
        if failures:
            result.errors.extend(failures)

    def _open_position(self, result, order, decision, hypothesis) -> None:
        """Attach the exit plan.

        If this fails the broker holds a position the agent is not
        managing. That is exactly what reconciliation catches on the
        next cycle, which will halt - so the failure is loud rather than
        silent, which is the best available outcome.
        """
        fill = order.get("average_fill_price")
        stop_pct = decision.stop_distance_pct
        if fill is None or stop_pct is None:
            result.errors.append(
                f"{order.get('symbol')}: filled but no exit plan could be "
                "built; reconciliation will halt on the next cycle")
            log_event("position_unmanaged_after_fill",
                      symbol=order.get("symbol"),
                      order_id=order.get("order_id"),
                      alert=("the broker holds a position the agent is not "
                             "managing"))
            return
        plan = ExitPlan(
            stop_price=fill * (1 - stop_pct / 100.0),
            target_price=fill * (1 + (stop_pct * 2) / 100.0),
            trailing_stop_pct=stop_pct,
            stop_mechanism=StopMechanism.ENGINE_POLLED)
        try:
            self.positions.open_from_order(
                order, plan, hypothesis_id=hypothesis.hypothesis_id,
                risk_decision_id=decision.decision_id,
                config_version=CONFIG_VERSION)
        except Exception as exc:                          # noqa: BLE001
            result.errors.append(
                f"{order.get('symbol')}: filled but not managed: {exc}")
            log_event("position_unmanaged_after_fill",
                      symbol=order.get("symbol"), detail=str(exc),
                      alert=("the broker holds a position the agent is not "
                             "managing"))


def _now() -> str:
    from .models import utcnow
    return utcnow()


def _safe_call(fn, *args):
    """Call an injected callable without letting it abort the cycle.

    A data provider raising must not stop the exits from running. The
    caller sees None, which every downstream check treats as missing
    data and therefore as a reason not to trade.
    """
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception:                                     # noqa: BLE001
        return None
