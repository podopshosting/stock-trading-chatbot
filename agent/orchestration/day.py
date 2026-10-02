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

from ..autonomy.alerts import Alert, AlertKind
from ..autonomy.health import Condition
from ..autonomy.policy import Action, AutonomyPolicy, PolicyViolation
from ..broker.alpaca_paper import UncertainSubmission
from ..broker.execution import ExecutionRefused, submit_approved, submit_exit
from ..broker.order_poller import poll_outstanding
from ..positions.adoption import adopt_external_positions
from ..journal import record_closed_position
from ..observability import log_event
from ..positions import ExitContext, ExitPlan, ExitReason, StopMechanism
from ..risk import RiskContext, RiskLimits, evaluate as evaluate_risk
from ..risk import should_engage_risk_lock
from .models import (
    CycleOutcome, CyclePhase, CycleResult, HaltReason,
)

# v1.1.0: same-session re-entry cooldown. Bumped because it changes
# behaviour, which starts a new evidence cohort by design.
CONFIG_VERSION = "orchestration-v1.1.0"

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


def phase_from_session(session) -> CyclePhase:
    """Derive the cycle phase from a MarketSessionResult.

    Reads the attributes DIRECTLY, with no getattr default. The first
    version of the cycle Lambda used getattr(..., None) for
    minutes_to_close, which did not exist on the result; every value
    silently became None, and since "open but unknown time to close"
    resolves to PRE_CLOSE, the live cycle would have refused every entry
    and flattened every position on every run. Nothing failed - it just
    quietly did the wrong thing. A missing attribute here must raise.
    """
    return resolve_phase(
        market_status=str(session.session),
        minutes_since_open=session.minutes_since_open,
        minutes_to_close=session.minutes_to_close)


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
                 cycle_lock=None,
                 autonomy: Optional[AutonomyPolicy] = None,
                 health=None, alerts=None, decisions=None,
                 versions: Optional[Dict] = None,
                 order_ledger=None, cohort: Optional[str] = None):
        self.broker = broker
        self.positions = position_manager
        self.journal = journal
        self.halt_store = halt_store
        self.limits = limits or RiskLimits()
        self.cycle_lock = cycle_lock
        self.autonomy = autonomy
        self.health = health
        self.alerts = alerts
        self.decisions = decisions
        self.versions = dict(versions or {})
        # The durable record of every order sent to an external venue.
        # An external broker will refuse to submit without it, which is
        # the point: an order that nothing recorded cannot be found
        # again after a crash.
        self.order_ledger = order_ledger
        self.cohort = cohort
        # The position manager cannot import the submission path without
        # putting it within reach of the read API, so the orchestrator -
        # which legitimately owns that path - supplies it.
        if getattr(position_manager, "exit_submitter", None) is None:
            try:
                position_manager.exit_submitter = submit_exit
            except AttributeError:
                pass
        if getattr(position_manager, "order_ledger", None) is None:
            try:
                position_manager.order_ledger = order_ledger
                position_manager.cohort = cohort
            except AttributeError:
                pass

        if autonomy is not None:
            # The policy is the single source of truth. The two legacy
            # booleans are DERIVED from it, so they cannot disagree with
            # it and cannot be set independently.
            trading_enabled = autonomy.may_open_new_exposure
            execution_available = autonomy.paper_trading_enabled
            if autonomy.paper_trading_enabled:
                self._require_paper_broker(broker)
        self.trading_enabled = trading_enabled
        self.execution_available = execution_available
        self._entry_orders: Dict[str, Dict] = {}

    @staticmethod
    def _require_paper_broker(broker) -> None:
        """PAPER mode must not be able to reach a real broker.

        Checked by what the broker SAYS it is, and an adapter that
        cannot say is refused: a broker that does not declare itself
        paper is not trusted as one. This is the structural answer to
        "can paper mode reach real money" - the orchestrator will not
        even be constructed with a broker that is not paper.
        """
        describe = getattr(broker, "capabilities", None)
        try:
            caps = describe() if callable(describe) else {}
        except Exception:                                 # noqa: BLE001
            caps = {}
        if caps.get("is_paper") is not True:
            raise PolicyViolation(
                "PAPER mode requires a broker that declares itself paper; "
                f"{type(broker).__name__} does not")

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
        result.execution_mode = (str(self.autonomy.mode)
                                 if self.autonomy else "LEGACY")
        result.versions = dict(self.versions)
        duplicates_before = getattr(self.broker, "duplicate_attempts", 0)
        # closed_positions() is the manager's whole-PROCESS history. The
        # cooldown must only see exits from THIS cycle (earlier ones are
        # in the journal), or in a long-lived process yesterday's exit
        # would block today's entry. Lambda hides the difference with a
        # fresh manager per call; replay and tests do not.
        self._closed_at_cycle_start = len(self.positions.closed_positions())

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
            # Realised losses alone can breach the limit with nothing
            # open, and _manage_exits returns early in that case, so the
            # lock is checked here as well. The exits step re-checks with
            # fresh prices so unrealised losses count too.
            self._check_daily_loss(result, realized_pnl_today, 0.0)

            # The session the exit ledger keys on. Set per cycle rather
            # than at construction: the manager outlives a single
            # session in a warm Lambda, and a stale date would file an
            # exit order under the wrong day - or, when it was never set
            # at all, refuse the exit entirely and leave the position
            # open, which is how this was found.
            try:
                self.positions.session_date = session_date
            except AttributeError:
                pass

            # --- 1. learn what is actually held, THEN reconcile ----------
            #
            # Adoption first: a position the agent can PROVE it opened is
            # an unrecorded one, not a foreign one, and reconciling
            # before recording it latches a stop over the agent's own
            # trade. A genuinely foreign position is still unadopted
            # when reconcile runs, and still trips it.
            self._adopt_external_positions(result, session_date)
            self._reconcile(result)
            # Before exits and before entries: an accepted order
            # is exposure, and the entry path must see it.
            self._poll_external_orders(result, session_date)

            # --- 2. exits, ALWAYS before entries -------------------------
            self._manage_exits(result, phase, quote_for, minutes_to_close,
                               session_date, realized_pnl_today)

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
        self._finish_health(result, phase, duplicates_before,
                            quote_for, candidates)

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

    # --- autonomy helpers ----------------------------------------------

    def _raise(self, result: CycleResult, condition: Condition,
               detail: str = "") -> None:
        """Record a health condition. Never raises: a failure to record
        health must not stop the exits from running."""
        if self.health is None:
            return
        try:
            self.health.raise_condition(condition, detail)
            log_event("health_condition_raised", condition=str(condition),
                      detail=detail[:160])
        except Exception as exc:                          # noqa: BLE001
            result.errors.append(
                f"could not record health condition {condition}: {exc}")

    def _clear(self, condition: Condition) -> None:
        """Clear a NON-latching condition once its cause has passed.
        Latching conditions refuse (no human named), which is the point:
        the cycle cannot talk itself back into a halted state."""
        if self.health is None:
            return
        try:
            if self.health.clear_condition(condition, cleared_by=None):
                log_event("health_condition_cleared",
                          condition=str(condition))
        except Exception:                                 # noqa: BLE001
            pass

    def _alert(self, result: CycleResult, kind: AlertKind, detail: str,
               **context) -> None:
        """Raise an operator alert. Never raises."""
        if self.alerts is None:
            return
        try:
            alert = Alert(kind=kind, detail=detail,
                          session_date=result.session_date, context=context)
            if self.alerts.emit(alert):
                result.alerts_raised.append(str(kind))
        except Exception as exc:                          # noqa: BLE001
            result.errors.append(f"alert {kind} could not be raised: {exc}")

    def _record_decision(self, result: CycleResult, symbol: str,
                         outcome: str, hypothesis=None, decision=None,
                         detail: str = "", order=None) -> None:
        """Persist why a candidate was or was not traded.

        Rejections are recorded as faithfully as entries: "why did you
        not trade AAPL" has to be answerable from the record, and an
        agent that only logs what it did cannot explain what it chose
        not to do. A failure here is reported, never fatal.
        """
        if self.decisions is None:
            return
        try:
            row = {
                "symbol": symbol, "outcome": outcome, "detail": detail,
                "cycle_id": result.cycle_id, "phase": str(result.phase),
                "session_date": result.session_date,
                "versions": dict(self.versions),
                "execution_mode": result.execution_mode,
            }
            if hypothesis is not None:
                row["hypothesis"] = {
                    "hypothesis_id": getattr(hypothesis, "hypothesis_id",
                                             None),
                    "strategy": str(getattr(hypothesis, "strategy", "")),
                    "is_actionable": getattr(hypothesis, "is_actionable",
                                             None),
                    "strength": getattr(hypothesis, "hypothesis_strength",
                                        None),
                    "contradictions": [
                        {"code": c.code, "severity": str(c.severity)}
                        for c in getattr(hypothesis, "contradictions", [])],
                }
            if decision is not None:
                row["risk"] = {
                    "decision_id": decision.decision_id,
                    "approved": decision.approved,
                    "reason_codes": [str(c) for c in decision.reason_codes],
                    "reasons": list(decision.reasons),
                    "capital_required": decision.capital_required,
                    "max_loss": decision.max_loss,
                    # Stored so that a position adopted later can be
                    # managed under the stop it was actually opened
                    # under, rather than one reconstructed from a
                    # limit. Its absence is why the DRAM adoption has
                    # to reconstruct.
                    "stop_distance_pct": decision.stop_distance_pct,
                }
            if order is not None:
                row["order"] = {
                    "order_id": order.get("order_id"),
                    "status": order.get("status"),
                    "filled_quantity": order.get("filled_quantity"),
                    "average_fill_price": order.get("average_fill_price"),
                }
            self.decisions.record(row)
            result.decisions_recorded += 1
        except Exception as exc:                          # noqa: BLE001
            result.errors.append(f"decision not recorded: {exc}")

    def _engage_emergency_stop(self, result: CycleResult, reason: str,
                               condition: Condition,
                               alert_kind: AlertKind, **context) -> None:
        """Latch a halt that only a human can clear.

        NO_NEW_EXPOSURE follows automatically (the halt is read by the
        next check and by every later cycle), while exits continue.
        Invoking a protection is within the agent's authority; lifting
        one is not, and nothing in this module can.
        """
        result.emergency_stop_engaged = True
        result.halt(HaltReason.EMERGENCY_STOP, reason)
        self._raise(result, condition, reason)
        self._raise(result, Condition.EMERGENCY_STOP, reason)
        if self.halt_store is not None:
            try:
                self.halt_store.engage(reason, engaged_by="agent")
            except Exception as exc:                      # noqa: BLE001
                result.errors.append(f"halt could not be persisted: {exc}")
        log_event("emergency_stop_engaged", reason=reason[:200],
                  condition=str(condition))
        self._alert(result, AlertKind.EMERGENCY_STOP, reason, **context)
        self._alert(result, alert_kind, reason, **context)

    # A liquid, always-quoted symbol used to re-test market data when no
    # other quote was requested this cycle.
    CANARY_SYMBOL = "SPY"

    def _probe_market_data(self, result: CycleResult, phase: CyclePhase,
                           quote_for, candidates) -> None:
        """Re-test market data when a data condition is blocking entries.

        Without this, a single stale quote deadlocks trading: the
        condition is non-latching and is meant to clear when a cycle
        sees good data, but a cycle that is blocked requests NO quotes,
        so it can never observe the recovery and the condition stands
        until a human intervenes. Found by a failure-injection test that
        expected staleness to clear itself.

        Probes only while such a condition is active and nothing else
        requested a quote this cycle, so it costs nothing in normal
        operation.
        """
        if (self.health is None or quote_for is None
                or not phase.permits_exits or result.quotes_requested):
            return
        try:
            active = {a.condition for a in self.health.snapshot().active}
        except Exception:                                 # noqa: BLE001
            return
        if not active & {Condition.STALE_MARKET_DATA,
                         Condition.MARKET_DATA_UNAVAILABLE}:
            return
        symbol = (list(candidates)[0] if candidates else self.CANARY_SYMBOL)
        quote = _safe_call(quote_for, symbol)
        result.quotes_requested += 1
        if quote is None or quote.get("price") is None:
            result.quotes_missing += 1
        elif (quote.get("age_seconds") is not None
              and quote["age_seconds"] > self.limits.max_quote_age_seconds):
            result.quotes_stale += 1

    def _finish_health(self, result: CycleResult, phase: CyclePhase,
                       duplicates_before: int, quote_for=None,
                       candidates=()) -> None:
        """End-of-cycle health bookkeeping. Never raises."""
        self._probe_market_data(result, phase, quote_for, candidates)
        # Duplicate-order attempts: the broker suppressing one is the
        # system working, but it means something tried to place an order
        # twice, and a human should know why.
        delta = getattr(self.broker, "duplicate_attempts", 0) \
            - duplicates_before
        if delta > 0:
            result.duplicate_attempts = delta
            self._raise(result, Condition.DUPLICATE_ORDER_ATTEMPT,
                        f"{delta} duplicate submission(s) suppressed")
            self._alert(result, AlertKind.DUPLICATE_ORDER_ATTEMPT,
                        f"{delta} duplicate order submission(s) were "
                        "suppressed this cycle", count=delta)

        # Market data: missing and stale are different failures and both
        # are non-latching, so they clear when a cycle sees good data.
        if result.quotes_requested:
            if result.quotes_missing >= result.quotes_requested:
                self._raise(result, Condition.MARKET_DATA_UNAVAILABLE,
                            f"{result.quotes_missing} of "
                            f"{result.quotes_requested} quotes missing")
                self._maybe_outage_alert(result,
                                         Condition.MARKET_DATA_UNAVAILABLE)
            else:
                self._clear(Condition.MARKET_DATA_UNAVAILABLE)
            if result.quotes_stale:
                self._raise(result, Condition.STALE_MARKET_DATA,
                            f"{result.quotes_stale} stale quote(s)")
            else:
                self._clear(Condition.STALE_MARKET_DATA)

        # Cycle failure streak.
        if self.health is not None:
            try:
                ok = result.outcome is not CycleOutcome.ABORTED
                streak = self.health.record_cycle(ok)
                if streak >= 3:
                    self._raise(result, Condition.REPEATED_CYCLE_FAILURE,
                                f"{streak} consecutive failed cycles")
                    self._alert(result, AlertKind.REPEATED_CYCLE_FAILURE,
                                f"{streak} consecutive cycles failed",
                                streak=streak)
                elif ok:
                    self._clear(Condition.REPEATED_CYCLE_FAILURE)
            except Exception as exc:                      # noqa: BLE001
                result.errors.append(f"health streak not recorded: {exc}")
            try:
                result.health_state = str(self.health.snapshot().state)
            except Exception:                             # noqa: BLE001
                result.health_state = "UNREADABLE"

    def _maybe_outage_alert(self, result: CycleResult,
                            condition: Condition) -> None:
        """One alert for a REPEATED outage, not one per blip."""
        if self.health is None:
            return
        try:
            for active in self.health.snapshot().active:
                if active.condition is condition and active.count >= 3:
                    self._alert(
                        result, AlertKind.REPEATED_PROVIDER_OUTAGE,
                        f"{condition} has been seen {active.count} times",
                        condition=str(condition), count=active.count)
        except Exception:                                 # noqa: BLE001
            pass

    def _check_daily_loss(self, result: CycleResult, realized: float,
                          unrealized: float) -> bool:
        """Invoke the daily risk lock if the day's loss limit is reached.

        A pure function of the day's P&L, recomputed every cycle, so it
        is not a discretionary choice the agent could talk itself out
        of: the lock holds exactly while the loss does.

        Until now nothing called should_engage_risk_lock at all. The
        limit existed, the governor read a flag derived from it, and no
        code ever set that flag, so a bad day would never have locked.
        """
        engaged, why = should_engage_risk_lock(realized, unrealized,
                                               self.limits)
        if engaged:
            result.halt(HaltReason.DAILY_LOSS_LIMIT, why)
            self._raise(result, Condition.DAILY_RISK_LOCK, why)
            self._alert(result, AlertKind.DAILY_RISK_LOCK, why,
                        realized=round(realized, 2),
                        unrealized=round(unrealized, 2),
                        limit=self.limits.daily_loss_limit)
        else:
            # Recomputed, so it releases when the loss no longer stands
            # - a new session starts at zero. Latching conditions are
            # unaffected: _clear refuses them.
            self._clear(Condition.DAILY_RISK_LOCK)
        return engaged

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
            self._raise(result, Condition.CYCLE_LOCK_FAILURE, str(exc)[:160])
            return False
        result.add_step("acquire_lock", ok=acquired,
                        detail="" if acquired else "already held")
        if acquired:
            self._clear(Condition.CYCLE_LOCK_FAILURE)
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

        # Health gates ENTRIES only. A degraded or halted agent must
        # still be able to close what it holds, so this adds a halt
        # reason (which blocks new exposure) and nothing else.
        if self.health is not None:
            try:
                snapshot = self.health.snapshot()
            except Exception as exc:                      # noqa: BLE001
                # An unreadable health record is not a clean bill of
                # health.
                result.halt(HaltReason.HEALTH_UNREADABLE, str(exc)[:160])
            else:
                result.health_state = str(snapshot.state)
                if not snapshot.entries_permitted:
                    result.halt(HaltReason.HEALTH_NOT_PERMITTING,
                                "; ".join(snapshot.blocking_reasons())[:240])

        result.add_step("check_switches", ok=True,
                        detail=(f"mode={result.execution_mode} "
                                f"trading_enabled={self.trading_enabled} "
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
        acting on fiction, so this runs before anything else.

        Two different failures are kept apart. An UNREADABLE broker is
        an outage: entries stop, and it clears when the broker comes
        back. A broker that was READ and DISAGREES is an emergency
        stop, latched until a human clears it, because the agent's risk
        arithmetic is then wrong in a direction nobody knows.
        """
        try:
            reconciliation = self.positions.reconcile()
        except Exception as exc:                          # noqa: BLE001
            result.halt(HaltReason.BROKER_DIVERGENCE,
                        f"reconciliation failed: {exc}")
            result.add_step("reconcile", ok=False, detail=str(exc))
            self._raise(result, Condition.BROKER_UNAVAILABLE, str(exc)[:160])
            self._maybe_outage_alert(result, Condition.BROKER_UNAVAILABLE)
            return

        result.positions_reconciled = reconciliation.matched
        if reconciliation.matched:
            self._clear(Condition.BROKER_UNAVAILABLE)
        elif reconciliation.unreadable:
            result.halt(HaltReason.BROKER_DIVERGENCE,
                        reconciliation.detail)
            self._raise(result, Condition.BROKER_UNAVAILABLE,
                        reconciliation.detail[:160])
            self._maybe_outage_alert(result, Condition.BROKER_UNAVAILABLE)
        else:
            result.halt(HaltReason.BROKER_DIVERGENCE,
                        reconciliation.detail)
            self._engage_emergency_stop(
                result, f"reconciliation mismatch: {reconciliation.detail}",
                Condition.RECONCILIATION_MISMATCH,
                AlertKind.RECONCILIATION_MISMATCH,
                agent_only=reconciliation.agent_only,
                broker_only=reconciliation.broker_only,
                quantity_mismatches=reconciliation.quantity_mismatches)
            if reconciliation.broker_only:
                # A position the agent never opened. It has no plan and
                # no record, so it is NOT closed automatically: acting on
                # something unexplained is the kind of guess the
                # emergency stop exists to prevent. A human decides.
                self._raise(result, Condition.UNEXPECTED_BROKER_POSITION,
                            ", ".join(reconciliation.broker_only))
                self._alert(
                    result, AlertKind.UNEXPECTED_BROKER_POSITION,
                    "the broker holds position(s) the agent did not open: "
                    + ", ".join(reconciliation.broker_only),
                    symbols=reconciliation.broker_only)
        result.add_step("reconcile", ok=reconciliation.matched,
                        detail=reconciliation.detail)

    def _adopt_external_positions(self, result: CycleResult,
                                  session_date: str) -> None:
        """Take back positions the agent provably created.

        Runs BEFORE reconcile, deliberately. `broker_only` means "not in
        the internal store", which conflates "not ours" with "ours but
        unrecorded" - and the second is a record gap, not a foreign
        position. Reconciling first would latch an emergency stop over a
        position the agent can prove it opened, which is what happened
        on 2026-10-02 and is why that position could not be closed.

        This does not weaken reconciliation: adoption removes the CAUSE
        of the disagreement by recording what the agent owns, and a
        genuinely foreign position still reaches reconcile unadopted and
        still trips it.
        """
        if self.broker is None:
            return
        adoption = adopt_external_positions(
            self.broker, self.positions, session_date=session_date,
            ledger=self.order_ledger, decisions=self.decisions,
            max_trade_risk=self.limits.max_trade_risk,
            config_version=self.versions.get("exits", ""))

        result.adoption = adoption
        result.adopted_positions = adoption["adopted"]
        result.unknown_origin_positions = list(adoption["unknown_origin"])
        result.preexisting_positions = list(adoption["preexisting"])
        result.exposure_blocked_by_adoption = bool(
            adoption["blocks_new_exposure"])

        for symbol in adoption["adopted_symbols"]:
            self._alert(result, AlertKind.UNEXPECTED_BROKER_POSITION,
                        f"{symbol} was held at the venue and not in the "
                        f"agent's own store; provenance proved it was the "
                        f"agent's own order, so it is now managed",
                        symbol=symbol)
        if adoption["unknown_origin"]:
            # Surfaced prominently and NOT closed. New exposure stops.
            self._raise(result, Condition.UNEXPECTED_BROKER_POSITION,
                        "unattributable position(s): "
                        + ", ".join(adoption["unknown_origin"]))
            self._alert(result, AlertKind.UNEXPECTED_BROKER_POSITION,
                        "position(s) at the venue cannot be attributed to "
                        "this agent and will not be closed automatically: "
                        + ", ".join(adoption["unknown_origin"]),
                        symbols=adoption["unknown_origin"])
        result.add_step(
            "adopt_positions",
            ok=adoption["integrity"] == "COMPLETE" and not adoption["refused"],
            detail=(f"integrity={adoption['integrity']} "
                    f"discovered={adoption['discovered']} "
                    f"adopted={adoption['adopted']} "
                    f"already={adoption['already_managed']} "
                    f"unknown={len(adoption['unknown_origin'])} "
                    f"preexisting={len(adoption['preexisting'])}"))

    def _poll_external_orders(self, result: CycleResult,
                              session_date: str) -> None:
        """Follow up every order the venue has not finished with.

        Runs BEFORE entries, because an accepted-but-unfilled order is
        exposure and a decision sized as though it were not live is how
        an account ends up past its own limits.

        Never fatal. A poll that could not complete reports PARTIAL or
        UNKNOWN integrity, which the entry path reads as a reason to
        refuse rather than as a clean zero.
        """
        if self.order_ledger is None:
            result.add_step("poll_external_orders", ok=True, skipped=True,
                            skip_reason="no order ledger (internal broker)")
            return
        try:
            poll = poll_outstanding(self.broker, self.order_ledger,
                                    session_date)
        except Exception as exc:                          # noqa: BLE001
            # Leave committed_exposure as None, not 0.0. The entry path
            # treats unknown exposure as a refusal.
            result.committed_exposure_known = False
            result.add_step("poll_external_orders", ok=False,
                            detail=f"{type(exc).__name__}: {exc}"[:200])
            log_event("order_poll_failed", session_date=session_date,
                      error=str(exc)[:200])
            return

        result.order_poll = poll
        result.pending_external_orders = poll.get("outstanding")
        result.committed_exposure_known = bool(poll.get("exposure_known"))
        exposure = poll.get("exposure") or {}
        result.committed_exposure = (exposure.get("reserved")
                                     if result.committed_exposure_known
                                     else None)
        result.oldest_pending_order_age_seconds = poll.get(
            "oldest_pending_age_seconds")

        if poll.get("never_placed"):
            log_event("order_never_placed", session_date=session_date,
                      detail=f"{poll['never_placed']} order(s) confirmed "
                             f"absent at the venue")
        if poll.get("vanished"):
            self._alert(result, AlertKind.UNCERTAIN_ORDER_STATE,
                        f"{poll['vanished']} order(s) acknowledged by the "
                        f"venue can no longer be found there")
        integrity = poll.get("integrity")
        result.add_step(
            "poll_external_orders",
            ok=integrity == "COMPLETE",
            detail=(f"integrity={integrity} outstanding="
                    f"{poll.get('outstanding')} observed="
                    f"{poll.get('observed')} never_placed="
                    f"{poll.get('never_placed')} "
                    f"exposure_known={result.committed_exposure_known}"))

    def _manage_exits(self, result: CycleResult, phase: CyclePhase,
                      quote_for, minutes_to_close, session_date,
                      realized_pnl_today: float = 0.0) -> None:
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

        # Gather every quote FIRST so the daily loss check sees fresh
        # prices. Unrealised losses count: waiting for a loss to be
        # realised before locking would enforce the limit only after the
        # damage is already taken, which is the opposite of a limit.
        quotes = {p.symbol: _safe_call(quote_for, p.symbol)
                  for p in open_positions}
        unrealized = 0.0
        for position in open_positions:
            q = quotes.get(position.symbol)
            if q is not None and q.get("price") is not None:
                unrealized += position.quantity * (
                    q["price"] - position.entry_price)
        self._check_daily_loss(result, realized_pnl_today, unrealized)

        halted = result.halted
        contexts = {}
        for position in open_positions:
            quote = quotes.get(position.symbol)
            result.quotes_requested += 1
            price = None if quote is None else quote.get("price")
            age = None if quote is None else quote.get("age_seconds")
            if price is None:
                result.quotes_missing += 1
            elif age is not None and age > self.limits.max_quote_age_seconds:
                result.quotes_stale += 1
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
            except UncertainSubmission as exc:
                # The close may or may not have been sent. It is never
                # resubmitted blindly; the next cycle resolves it by
                # lookup.
                failures.append(f"{intent.symbol}: {exc}")
                result.exits_failed += 1
                self._raise(result, Condition.UNCERTAIN_ORDER_STATE,
                            str(exc)[:160])
                self._alert(result, AlertKind.UNCERTAIN_ORDER_STATE,
                            f"exit order for {intent.symbol} is in an "
                            "uncertain state", symbol=intent.symbol)
                continue
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

        # End-of-day verification. Anything still open after a flatten
        # attempt is the failure this system exists to avoid: overnight
        # exposure with only a polled stop between it and a gap.
        if phase.requires_flatten and self.positions.open_count > 0:
            remaining = [p.symbol for p in self.positions.open_positions()]
            result.eod_flatten_failed = True
            self._raise(result, Condition.EOD_FLATTEN_FAILURE,
                        "still open after flatten: " + ", ".join(remaining))
            self._alert(result, AlertKind.EOD_FLATTEN_FAILURE,
                        "positions remain open after the end-of-day "
                        "flatten: " + ", ".join(remaining),
                        symbols=remaining)
            if (minutes_to_close is not None and minutes_to_close <= 10):
                self._alert(result, AlertKind.POSITION_OPEN_NEAR_CLOSE,
                            f"{len(remaining)} position(s) open with "
                            f"{minutes_to_close:.0f} minutes to the close",
                            symbols=remaining,
                            minutes_to_close=minutes_to_close)
        elif phase.requires_flatten:
            self._clear(Condition.EOD_FLATTEN_FAILURE)

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
                config_versions={"orchestration": CONFIG_VERSION,
                                 **self.versions},
                is_paper=not getattr(self.broker, "is_live", False))
            result.trades_journalled += 1
            self._entry_orders.pop(position.symbol, None)
        except Exception as exc:                          # noqa: BLE001
            # A journalling failure must not be mistaken for an exit
            # failure: the position IS closed, the record is missing.
            self._raise(result, Condition.JOURNAL_PERSISTENCE_FAILURE,
                        f"{position.symbol}: {str(exc)[:120]}")
            self._alert(result, AlertKind.JOURNAL_PERSISTENCE_FAILURE,
                        f"the trade for {position.symbol} closed but could "
                        "not be journalled", symbol=position.symbol)
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
        # --- pending orders are exposure ----------------------------------
        #
        # An accepted-but-unfilled BUY may fill a moment from now. Sizing
        # a second entry as though it were not live is how an account
        # ends up past the ceiling its limits describe. So the reserved
        # amount is added to the day's deployed capital BEFORE any
        # decision is evaluated.
        #
        # And if the reservation could not be established, no entry is
        # made at all. Treating unknown exposure as zero is the one
        # arithmetic error that silently permits everything.
        if result.exposure_blocked_by_adoption:
            result.add_step(
                "consider_entries", ok=False, skipped=True,
                skip_reason="a position at the venue cannot be attributed "
                            "to this agent; no new exposure is opened "
                            "while exposure cannot be explained")
            return
        if self.order_ledger is not None and not result.committed_exposure_known:
            result.add_step(
                "consider_entries", ok=False, skipped=True,
                skip_reason="committed exposure from outstanding orders "
                            "could not be established; no new exposure is "
                            "opened while it is unknown")
            log_event("entries_blocked_unknown_exposure",
                      session_date=session_date,
                      detail="outstanding external order exposure is "
                             "UNKNOWN, which is not zero")
            return

        held = {p.symbol for p in self.positions.open_positions()}
        deployed = capital_deployed + (result.committed_exposure or 0.0)
        opened = positions_opened_today
        failures: List[str] = []

        # Same-session re-entry cooldown. Buying straight back into a
        # name that has just been stopped out of is churn: the same
        # price, the same signal that has already been wrong once, and
        # the daily cap is the only thing limiting it. Found when a
        # handler test saw the agent stop out of XYZ at 96 and re-enter
        # XYZ at 96 within the same cycle.
        #
        # Unknown is treated as cooling down. If the journal cannot be
        # read, no entry is made rather than assuming nothing was
        # traded today.
        try:
            exited_today = {t.symbol for t in
                            self.journal.list_trades(session_date=session_date)}
            exited_today |= {p.symbol for p in
                             self.positions.closed_positions()[
                                 getattr(self, "_closed_at_cycle_start", 0):]}
            cooldown_known = True
        except Exception as exc:                          # noqa: BLE001
            exited_today, cooldown_known = set(), False
            result.errors.append(f"cooldown unknown, no entries made: {exc}")

        for symbol in candidates:
            result.symbols_scanned += 1
            if not cooldown_known:
                self._record_decision(
                    result, symbol, "NOT_EVALUATED",
                    detail="today's trades could not be read, so the "
                           "re-entry cooldown cannot be applied")
                continue
            if symbol in exited_today:
                self._record_decision(
                    result, symbol, "COOLDOWN",
                    detail="already traded and exited this session; no "
                           "same-session re-entry")
                continue
            if symbol in held:
                self._record_decision(result, symbol, "ALREADY_HELD",
                                      detail="a position is already open")
                continue
            if self.positions.open_count >= self.limits.max_concurrent_positions:
                self._record_decision(
                    result, symbol, "NOT_EVALUATED",
                    detail="the concurrent position limit is reached")
                break
            if opened >= self.limits.max_new_positions_per_day:
                self._record_decision(
                    result, symbol, "NOT_EVALUATED",
                    detail="the daily new-position limit is reached")
                break

            hypothesis = _safe_call(hypothesis_for, symbol)
            if hypothesis is None:
                self._record_decision(
                    result, symbol, "NO_HYPOTHESIS",
                    detail="no hypothesis could be formed (signal or "
                           "data provider unavailable)")
                continue
            result.hypotheses_generated += 1

            quote = _safe_call(quote_for, symbol)
            result.quotes_requested += 1
            if quote is None or quote.get("price") is None:
                result.quotes_missing += 1
            elif (quote.get("age_seconds") is not None
                  and quote["age_seconds"] > self.limits.max_quote_age_seconds):
                result.quotes_stale += 1
            context = RiskContext(
                session_date=session_date, market_session="OPEN",
                minutes_to_close=minutes_to_close,
                trading_enabled=self.trading_enabled,
                execution_available=self.execution_available,
                price=None if quote is None else quote.get("price"),
                spread_pct=None if quote is None else quote.get("spread_pct"),
                dollar_volume=(None if quote is None
                               else quote.get("dollar_volume")),
                source_age_seconds=(None if quote is None
                                    else quote.get("source_age_seconds")),
                feed_quality=(None if quote is None
                              else quote.get("feed_quality")),
                quote_age_seconds=(None if quote is None
                                   else quote.get("age_seconds")),
                capital_deployed_today=deployed,
                realized_pnl_today=realized_pnl_today,
                open_positions=self.positions.open_count,
                positions_opened_today=opened,
                held_symbols=sorted(held))

            decision = evaluate_risk(hypothesis, context, limits=self.limits)
            if not decision.approved:
                self._record_decision(
                    result, symbol, "REFUSED", hypothesis=hypothesis,
                    decision=decision,
                    detail="; ".join(decision.reasons)[:300])
                continue
            result.decisions_approved += 1

            try:
                order = submit_approved(
                    self.broker, decision, hypothesis,
                    reference_price=context.price,
                    execution_available=self.execution_available,
                    ledger=self.order_ledger,
                    session_date=session_date,
                    cohort=self.cohort)
            except UncertainSubmission as exc:
                # The order may or may not exist. Placing anything else
                # around it would compound an unknown, so entries stop
                # for this cycle and the condition latches.
                failures.append(f"{symbol}: {exc}")
                result.entries_failed += 1
                result.halt(HaltReason.UNCERTAIN_ORDER, str(exc)[:200])
                self._raise(result, Condition.UNCERTAIN_ORDER_STATE,
                            str(exc)[:160])
                self._alert(result, AlertKind.UNCERTAIN_ORDER_STATE,
                            f"entry order for {symbol} is in an uncertain "
                            "state and was not retried", symbol=symbol)
                self._record_decision(
                    result, symbol, "ORDER_UNCERTAIN",
                    hypothesis=hypothesis, decision=decision,
                    detail=str(exc)[:240])
                break
            except ExecutionRefused as exc:
                failures.append(f"{symbol}: {exc}")
                result.entries_failed += 1
                self._record_decision(
                    result, symbol, "EXECUTION_REFUSED",
                    hypothesis=hypothesis, decision=decision,
                    detail=str(exc)[:240])
                continue
            except Exception as exc:                      # noqa: BLE001
                failures.append(f"{symbol}: {exc}")
                result.entries_failed += 1
                self._record_decision(
                    result, symbol, "ORDER_ERROR", hypothesis=hypothesis,
                    decision=decision, detail=str(exc)[:240])
                continue

            if (order.get("filled_quantity") or 0) <= 0:
                cancel_note = self._cancel_abandoned_entry(
                    result, symbol, order)
                self._record_decision(
                    result, symbol, "ORDER_NOT_FILLED",
                    hypothesis=hypothesis, decision=decision, order=order,
                    detail=f"status {order.get('status')}; {cancel_note}")
                continue

            result.entries_submitted += 1
            deployed += decision.capital_required
            opened += 1
            held.add(symbol)
            self._entry_orders[symbol] = order
            self._record_decision(
                result, symbol, "ENTERED", hypothesis=hypothesis,
                decision=decision, order=order)
            self._open_position(result, order, decision, hypothesis)

        result.add_step("consider_entries", ok=not failures,
                        detail="; ".join(failures) if failures else
                        f"{result.entries_submitted} entry(ies) submitted")
        if failures:
            result.errors.extend(failures)

    def _cancel_abandoned_entry(self, result: CycleResult, symbol: str,
                                order: Dict) -> str:
        """Cancel an entry order that read back unfilled.

        An order that has left the agent is exposure whether or not it
        has filled yet. This path used to record ORDER_NOT_FILLED and
        move on, leaving the order WORKING at the venue.

        Against the internal simulator that was safe, because it fills
        synchronously or never - so "unfilled on read-back" really did
        mean "will never fill". A real venue can fill a marketable limit
        milliseconds after the agent looks. On 2026-10-02 it did: a DRAM
        order was abandoned here and filled afterwards, and the agent
        held a position it had no record of until reconciliation halted
        the next cycle.
        """
        order_id = order.get("order_id")
        if not order_id:
            return "no order id, so the order could not be cancelled"
        try:
            self.broker.cancel_order(order_id)
        except Exception as exc:                          # noqa: BLE001
            # The order may still be working, or may have filled while
            # the cancel was in flight. Either way exposure is now
            # UNCERTAIN and must not be reported as absent - the next
            # cycle's reconciliation is what resolves it.
            self._raise(result, Condition.UNCERTAIN_ORDER_STATE,
                        f"{symbol}: unfilled entry order {order_id} could "
                        f"not be cancelled: {type(exc).__name__}")
            self._alert(result, AlertKind.UNCERTAIN_ORDER_STATE,
                        f"{symbol}: an unfilled entry order could not be "
                        f"cancelled and may still fill", symbol=symbol)
            return (f"cancel FAILED ({type(exc).__name__}); the order may "
                    f"still fill")
        log_event("entry_remainder_cancelled", symbol=symbol,
                  order_id=order_id, filled_quantity=0,
                  reason="entry read back unfilled; not left working")
        return "cancelled so it cannot fill later"

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
