"""
Market-day orchestration.

The property these tests exist to protect: there is no failure mode in
which the agent keeps opening positions while something is wrong, and
no failure mode in which it stops closing them.

Those two are not symmetrical. Refusing to enter costs an opportunity;
refusing to exit costs money without limit. So almost every test here
is a variation on "break something, then check that exits still ran and
entries did not".
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    PaperBroker, PaperBrokerConfig, Quote,
)
from agent.hypothesis import generate                             # noqa: E402
from agent.journal import InMemoryJournal                         # noqa: E402
from agent.orchestration import (                                 # noqa: E402
    OPENING_MINUTES, PRE_CLOSE_MINUTES, CycleOutcome, CyclePhase,
    CycleResult, HaltReason, InMemoryCycleLock, MarketDayOrchestrator,
    resolve_phase,
)
from agent.positions import (                                     # noqa: E402
    ExitPlan, PositionManager,
)
from agent.risk import InMemoryHaltStore, RiskLimits               # noqa: E402


def quote_for(symbol):
    return {"price": 100.0, "spread_pct": 0.05, "dollar_volume": 5e8,
            "age_seconds": 5.0, "source_age_seconds": 5.0,
            "feed_quality": "REALTIME_SIP"}


def falling_quote(symbol):
    """A price below any normal stop, so an exit definitely fires.

    Needed because open_from_order refuses a stop at or above the entry,
    so a position cannot be constructed already in breach - the price
    has to come down to it.
    """
    return {"price": 90.0, "spread_pct": 0.05, "dollar_volume": 5e8,
            "age_seconds": 5.0, "source_age_seconds": 5.0,
            "feed_quality": "REALTIME_SIP"}


def stopping_quote(symbol):
    """Through the stop, but a loss INSIDE the daily loss limit.

    falling_quote drops to 90, a $10 loss against a $5 daily limit, which
    now correctly engages the daily risk lock and blocks entries. Tests
    about OTHER behaviour need a position that stops out without
    tripping that lock, or they are silently testing the lock instead.
    """
    return {"price": 96.0, "spread_pct": 0.05, "dollar_volume": 5e8,
            "age_seconds": 5.0, "source_age_seconds": 5.0,
            "feed_quality": "REALTIME_SIP"}


def hypothesis_for(symbol):
    """A hypothesis strong enough to be approved."""
    signal = {"direction": "BUY", "signal_agreement": 1.0,
              "signal_magnitude": 0.7, "regime_adjusted_magnitude": 0.7,
              "strength_band": "STRONG", "buy_groups": 3, "sell_groups": 0,
              "opinionated_groups": 3,
              "data_quality": {"freshness": "FRESH"}}
    catalyst = {"has_active_catalyst": True, "direction": "POSITIVE",
                "primary_catalyst": {"materiality": 0.7, "novelty": 0.8,
                                     "type": "EARNINGS"},
                "evidence_score": 0.7, "independent_source_count": 2,
                "primary_source_count": 1}
    regime = {"regime": "BULLISH", "regime_confidence": 0.8,
              "risk_posture": "NORMAL", "market_session": "OPEN"}
    return generate(symbol, signal, catalyst, regime)


def build(trading=True, execution=True, lock=None, halt_store=None,
          cash=200.0, limits=None):
    broker = PaperBroker(PaperBrokerConfig(starting_cash=cash, seed=3,
                                           partial_fill_probability=0.0))
    broker.set_quote(Quote(symbol="XYZ", bid=99.9, ask=100.1, last=100.0))
    broker.set_quote(Quote(symbol="AAA", bid=49.9, ask=50.1, last=50.0))
    manager = PositionManager(broker=broker, execution_available=execution)
    orchestrator = MarketDayOrchestrator(
        broker=broker, position_manager=manager, journal=InMemoryJournal(),
        halt_store=halt_store, limits=limits, trading_enabled=trading,
        execution_available=execution, cycle_lock=lock)
    return broker, manager, orchestrator


def cycle(orchestrator, phase=CyclePhase.INTRADAY, candidates=("XYZ",),
          minutes_to_close=200, **kwargs):
    return orchestrator.run_cycle(
        session_date="2026-09-30", phase=phase, candidates=list(candidates),
        quote_for=quote_for, hypothesis_for=hypothesis_for,
        minutes_to_close=minutes_to_close, **kwargs)


def with_open_position(orchestrator, manager, broker, stop=97.0):
    order = broker.submit_order("XYZ", "BUY", 1.0)
    manager.open_from_order(order, ExitPlan(stop_price=stop),
                            hypothesis_id="h1", risk_decision_id="d1")
    orchestrator._entry_orders["XYZ"] = order
    return order


class TestPhaseResolution(unittest.TestCase):

    def test_an_unknown_market_status_is_never_treated_as_open(self):
        """
        A provider that cannot tell us whether the market is trading is
        not evidence that it is.
        """
        phase = resolve_phase("UNKNOWN")
        self.assertIs(phase, CyclePhase.UNKNOWN)
        self.assertFalse(phase.permits_new_exposure)

    def test_intraday_is_the_only_phase_that_opens_positions(self):
        for phase in CyclePhase:
            with self.subTest(phase=phase):
                self.assertEqual(phase.permits_new_exposure,
                                 phase is CyclePhase.INTRADAY)

    def test_the_opening_minutes_do_not_open_positions(self):
        """
        Spreads are widest and the first print is often not a price
        anyone could have traded.
        """
        phase = resolve_phase("OPEN", minutes_since_open=OPENING_MINUTES - 1,
                              minutes_to_close=300)
        self.assertIs(phase, CyclePhase.OPENING)
        self.assertFalse(phase.permits_new_exposure)
        self.assertTrue(phase.permits_exits)

    def test_the_pre_close_window_does_not_open_positions(self):
        """
        A position opened there cannot be given time to work before it
        must be flattened.
        """
        phase = resolve_phase("OPEN", minutes_since_open=200,
                              minutes_to_close=PRE_CLOSE_MINUTES - 1)
        self.assertIs(phase, CyclePhase.PRE_CLOSE)
        self.assertFalse(phase.permits_new_exposure)

    def test_open_but_unknown_time_to_close_forbids_entries(self):
        """
        Without knowing how long is left, the flatten window cannot be
        respected. Exits are still safe.
        """
        phase = resolve_phase("OPEN", minutes_since_open=60,
                              minutes_to_close=None)
        self.assertFalse(phase.permits_new_exposure)
        self.assertTrue(phase.permits_exits)

    def test_a_normal_mid_session_cycle_is_intraday(self):
        """The falsifying control: something must reach INTRADAY."""
        self.assertIs(resolve_phase("OPEN", 60, 300), CyclePhase.INTRADAY)

    def test_exits_are_permitted_wherever_the_market_is_reachable(self):
        for phase in (CyclePhase.OPENING, CyclePhase.INTRADAY,
                      CyclePhase.PRE_CLOSE):
            with self.subTest(phase=phase):
                self.assertTrue(phase.permits_exits)


class TestCycleOrdering(unittest.TestCase):
    """
    reconcile -> exits -> entries. If the process dies partway through,
    having done exits first means risk was reduced before the failure.
    """

    def test_the_steps_run_in_the_safe_order(self):
        _b, _m, orchestrator = build()
        result = cycle(orchestrator)
        names = [s.name for s in result.steps]
        self.assertLess(names.index("reconcile"), names.index("manage_exits"))
        self.assertLess(names.index("manage_exits"),
                        names.index("consider_entries"))

    def test_reconciliation_precedes_everything_that_acts(self):
        """
        Acting on a position set that disagrees with the broker is
        acting on fiction.
        """
        _b, _m, orchestrator = build()
        result = cycle(orchestrator)
        names = [s.name for s in result.steps]
        self.assertLess(names.index("reconcile"),
                        names.index("consider_entries"))

    def test_risk_was_managed_reports_the_protective_half(self):
        _b, _m, orchestrator = build()
        self.assertTrue(cycle(orchestrator).risk_was_managed)

    def test_risk_was_managed_is_false_if_reconciliation_failed(self):
        class Broken:
            def get_positions(self):
                raise RuntimeError("no connection")
        _b, manager, orchestrator = build()
        manager.broker = Broken()
        result = cycle(orchestrator)
        self.assertFalse(result.risk_was_managed)


class TestEntriesStopOnAnyProblem(unittest.TestCase):

    def test_trading_disabled_blocks_entries(self):
        _b, _m, orchestrator = build(trading=False)
        result = cycle(orchestrator)
        self.assertIn(HaltReason.TRADING_DISABLED, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_execution_unavailable_blocks_entries(self):
        _b, _m, orchestrator = build(execution=False)
        result = cycle(orchestrator)
        self.assertIn(HaltReason.EXECUTION_UNAVAILABLE, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_a_global_halt_blocks_entries(self):
        store = InMemoryHaltStore()
        store.engage("operator halt", engaged_by="test")
        _b, _m, orchestrator = build(halt_store=store)
        result = cycle(orchestrator)
        self.assertIn(HaltReason.GLOBAL_HALT, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_an_unreadable_halt_state_blocks_entries(self):
        """
        An operator may have halted trading and we cannot see it.
        """
        class Broken:
            def get(self):
                raise RuntimeError("dynamo unavailable")
        _b, _m, orchestrator = build(halt_store=Broken())
        result = cycle(orchestrator)
        self.assertIn(HaltReason.HALT_STATE_UNREADABLE, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_broker_divergence_blocks_entries(self):
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)
        broker._positions.clear()
        result = cycle(orchestrator)
        self.assertIn(HaltReason.BROKER_DIVERGENCE, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_a_non_intraday_phase_blocks_entries(self):
        _b, _m, orchestrator = build()
        for phase in (CyclePhase.OPENING, CyclePhase.PRE_CLOSE,
                      CyclePhase.PRE_MARKET, CyclePhase.CLOSED,
                      CyclePhase.UNKNOWN):
            with self.subTest(phase=phase):
                result = cycle(orchestrator, phase=phase)
                self.assertEqual(result.entries_submitted, 0)

    def test_a_clean_intraday_cycle_does_enter(self):
        """
        The falsifying control for this whole class: the gate must be
        passable, or the tests above prove nothing.
        """
        _b, _m, orchestrator = build()
        result = cycle(orchestrator)
        self.assertEqual(result.entries_submitted, 1)
        self.assertEqual(result.outcome, CycleOutcome.COMPLETED)

    def test_new_exposure_permitted_needs_the_phase_and_no_halts(self):
        """
        Both conditions, so a halt cannot be cleared by changing the
        phase and a wrong phase cannot be excused by an empty halt list.
        """
        result = CycleResult(cycle_id="c", session_date="d",
                             phase=CyclePhase.INTRADAY,
                             outcome=CycleOutcome.COMPLETED)
        self.assertTrue(result.new_exposure_permitted)
        result.halt(HaltReason.GLOBAL_HALT)
        self.assertFalse(result.new_exposure_permitted)

        other = CycleResult(cycle_id="c", session_date="d",
                            phase=CyclePhase.PRE_CLOSE,
                            outcome=CycleOutcome.COMPLETED)
        self.assertFalse(other.new_exposure_permitted)

    def test_new_exposure_permitted_is_derived_not_assignable(self):
        result = CycleResult(cycle_id="c", session_date="d",
                             phase=CyclePhase.PRE_CLOSE,
                             outcome=CycleOutcome.COMPLETED)
        with self.assertRaises(AttributeError):
            result.new_exposure_permitted = True

    def test_capital_and_position_limits_are_respected(self):
        limits = RiskLimits(max_concurrent_positions=1)
        broker, _m, orchestrator = build(limits=limits, cash=500.0)
        result = cycle(orchestrator, candidates=("XYZ", "AAA"))
        self.assertLessEqual(result.entries_submitted, 1)

    def test_a_held_symbol_is_not_entered_again(self):
        """Averaging down is forbidden; so is a second plan."""
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)
        result = cycle(orchestrator, candidates=("XYZ",))
        self.assertEqual(result.entries_submitted, 0)


class TestExitsAlwaysRun(unittest.TestCase):
    """
    Refusing to enter costs an opportunity; refusing to exit costs money
    without limit. The two are not symmetrical.
    """

    def test_exits_run_even_when_halted(self):
        store = InMemoryHaltStore()
        store.engage("operator halt", engaged_by="test")
        broker, manager, orchestrator = build(halt_store=store)
        with_open_position(orchestrator, manager, broker)
        result = cycle(orchestrator)
        self.assertTrue(result.halted)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(result.entries_submitted, 0)

    def test_a_halt_makes_the_exit_a_global_halt_exit(self):
        store = InMemoryHaltStore()
        store.engage("operator halt", engaged_by="test")
        broker, manager, orchestrator = build(halt_store=store)
        with_open_position(orchestrator, manager, broker)
        cycle(orchestrator)
        closed = orchestrator.journal.list_trades()
        self.assertTrue(closed)
        self.assertIn("GLOBAL_HALT", closed[0].all_exit_reasons)

    def test_exits_run_in_the_pre_close_phase(self):
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)
        result = cycle(orchestrator, phase=CyclePhase.PRE_CLOSE,
                       minutes_to_close=5)
        self.assertGreater(result.exits_submitted, 0)

    def test_the_pre_close_phase_flattens_even_a_healthy_position(self):
        """
        This system's risk model assumes no overnight gap risk, so it
        must not carry any - a stop does nothing between the close and
        the open.
        """
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker, stop=1.0)
        result = cycle(orchestrator, phase=CyclePhase.PRE_CLOSE,
                       minutes_to_close=5)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(manager.open_count, 0)

    def test_the_orchestrator_flattens_when_the_exit_engine_does_not(self):
        """
        The exit engine has its own END_OF_DAY rule, which fires first
        and normally makes the orchestrator's flatten redundant. That
        made the test above unable to distinguish the two layers.

        Here the position's plan has no end-of-day rule at all, so the
        engine produces nothing and the orchestrator's flatten is the
        ONLY thing that can close it. This system's risk model assumes
        no overnight gap risk, so something must.
        """
        broker, manager, orchestrator = build()
        order = broker.submit_order("XYZ", "BUY", 1.0)
        manager.open_from_order(
            order,
            ExitPlan(stop_price=1.0, target_price=None,
                     trailing_stop_pct=None,
                     flatten_before_close_minutes=None),
            hypothesis_id="h1", risk_decision_id="d1")
        orchestrator._entry_orders["XYZ"] = order

        result = cycle(orchestrator, phase=CyclePhase.PRE_CLOSE,
                       minutes_to_close=5)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(manager.open_count, 0)
        closed = orchestrator.journal.list_trades()
        self.assertIn("END_OF_DAY", closed[0].all_exit_reasons)

    def test_a_missing_quote_still_produces_an_exit(self):
        """
        An open position whose price cannot be established is holding
        unbounded risk. Out is the safe direction.
        """
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)
        result = orchestrator.run_cycle(
            session_date="2026-09-30", phase=CyclePhase.INTRADAY,
            candidates=["XYZ"], quote_for=lambda s: None,
            hypothesis_for=hypothesis_for, minutes_to_close=200)
        self.assertGreater(result.exits_submitted, 0)

    def test_a_raising_quote_provider_does_not_stop_the_exits(self):
        """
        A data provider raising must not prevent risk from being
        managed.
        """
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)

        def exploding(symbol):
            raise RuntimeError("provider down")
        result = orchestrator.run_cycle(
            session_date="2026-09-30", phase=CyclePhase.INTRADAY,
            candidates=["XYZ"], quote_for=exploding,
            hypothesis_for=hypothesis_for, minutes_to_close=200)
        self.assertGreater(result.exits_submitted, 0)

    def test_a_failed_exit_is_counted_and_flagged(self):
        """
        Uses a falling quote so an exit definitely fires. Without that,
        submit_exit is never reached and the assertion would pass
        against a cycle that did nothing.
        """
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)

        def refuse_exit(intent):
            raise RuntimeError("broker rejected the exit")
        manager.submit_exit = refuse_exit

        result = orchestrator.run_cycle(
            session_date="2026-09-30", phase=CyclePhase.INTRADAY,
            candidates=["XYZ"], quote_for=falling_quote,
            hypothesis_for=hypothesis_for, minutes_to_close=200)
        self.assertEqual(result.exits_failed, 1)
        self.assertTrue(any("rejected the exit" in e for e in result.errors))

    def test_exits_are_skipped_only_where_the_market_is_unreachable(self):
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)
        result = cycle(orchestrator, phase=CyclePhase.CLOSED)
        step = next(s for s in result.steps if s.name == "manage_exits")
        self.assertTrue(step.skipped)
        self.assertEqual(result.exits_submitted, 0)


class TestIdempotency(unittest.TestCase):
    """
    EventBridge delivers at-least-once and Lambdas overlap. Two cycles
    passing the risk checks against the same capital would both enter,
    producing double the intended exposure from one day's signal.
    """

    def test_a_second_concurrent_cycle_is_refused(self):
        lock = InMemoryCycleLock()
        _b, _m, orchestrator = build(lock=lock)
        lock.acquire("another-cycle")
        result = cycle(orchestrator)
        self.assertEqual(result.outcome, CycleOutcome.SKIPPED_DUPLICATE)
        self.assertIn(HaltReason.CONCURRENT_CYCLE, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_the_lock_is_released_so_the_next_cycle_can_run(self):
        lock = InMemoryCycleLock()
        _b, _m, orchestrator = build(lock=lock)
        cycle(orchestrator)
        self.assertIsNone(lock.held_by)
        self.assertTrue(lock.acquire("next-cycle"))

    def test_only_the_holder_may_release_the_lock(self):
        """
        A late cycle releasing a lock it does not own would let a third
        cycle in alongside the holder.
        """
        lock = InMemoryCycleLock()
        lock.acquire("owner")
        lock.release("impostor")
        self.assertEqual(lock.held_by, "owner")

    def test_an_unreadable_lock_refuses_the_cycle(self):
        """
        Cannot tell whether another cycle is running. Two cycles
        entering the same position is worse than skipping one.
        """
        class Broken:
            def acquire(self, cycle_id):
                raise RuntimeError("lock table unavailable")

            def release(self, cycle_id):
                pass
        _b, _m, orchestrator = build(lock=Broken())
        result = cycle(orchestrator)
        self.assertEqual(result.outcome, CycleOutcome.SKIPPED_DUPLICATE)
        self.assertEqual(result.entries_submitted, 0)

    def test_the_lock_expires_so_a_dead_lambda_does_not_deadlock(self):
        lock = InMemoryCycleLock(ttl_seconds=0)
        lock.acquire("crashed-cycle")
        self.assertTrue(lock.acquire("new-cycle"))

    def test_the_same_signal_does_not_enter_twice_across_cycles(self):
        _b, manager, orchestrator = build(cash=500.0)
        first = cycle(orchestrator)
        second = cycle(orchestrator)
        self.assertEqual(first.entries_submitted, 1)
        self.assertEqual(second.entries_submitted, 0)
        self.assertEqual(manager.open_count, 1)


class TestFailureContainment(unittest.TestCase):

    def test_an_unhandled_error_halts_rather_than_propagates(self):
        """
        A cycle must never raise into the scheduler: a retry would run
        the whole thing again, and the parts that succeeded would run
        twice.
        """
        _b, manager, orchestrator = build()
        manager.reconcile = lambda: (_ for _ in ()).throw(
            RuntimeError("boom"))
        result = cycle(orchestrator)
        self.assertIn(HaltReason.BROKER_DIVERGENCE, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_a_failure_inside_entries_does_not_undo_the_exits(self):
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)

        def exploding(*args, **kwargs):
            raise RuntimeError("entry path broken")
        orchestrator._consider_entries = exploding
        result = orchestrator.run_cycle(
            session_date="2026-09-30", phase=CyclePhase.INTRADAY,
            candidates=["XYZ"], quote_for=stopping_quote,
            hypothesis_for=hypothesis_for, minutes_to_close=200)
        self.assertEqual(result.outcome, CycleOutcome.ABORTED)
        self.assertGreater(result.exits_submitted, 0)
        self.assertTrue(result.risk_was_managed)

    def test_a_raising_hypothesis_provider_does_not_abort_the_cycle(self):
        _b, _m, orchestrator = build()

        def exploding(symbol):
            raise RuntimeError("signal service down")
        result = orchestrator.run_cycle(
            session_date="2026-09-30", phase=CyclePhase.INTRADAY,
            candidates=["XYZ"], quote_for=quote_for,
            hypothesis_for=exploding, minutes_to_close=200)
        self.assertEqual(result.entries_submitted, 0)
        self.assertNotEqual(result.outcome, CycleOutcome.ABORTED)

    def test_a_journal_failure_is_distinguished_from_an_exit_failure(self):
        """
        The position IS closed; only the record is missing. Confusing
        the two would make someone think risk was still live.
        """
        broker, manager, orchestrator = build()
        with_open_position(orchestrator, manager, broker)

        class BrokenJournal:
            def record(self, trade):
                raise RuntimeError("dynamo down")

            def list_trades(self, **kwargs):
                return []
        orchestrator.journal = BrokenJournal()
        result = orchestrator.run_cycle(
            session_date="2026-09-30", phase=CyclePhase.INTRADAY,
            candidates=["XYZ"], quote_for=falling_quote,
            hypothesis_for=hypothesis_for, minutes_to_close=200)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(result.exits_failed, 0)
        self.assertTrue(any("only the record is missing" in e
                            for e in result.errors))

    def test_a_fill_that_cannot_be_managed_is_flagged_loudly(self):
        """
        The broker then holds a position the agent is not managing.
        Reconciliation catches it next cycle and halts, so the failure
        is loud rather than silent - the best available outcome.
        """
        _b, manager, orchestrator = build()

        def refuse(*args, **kwargs):
            raise RuntimeError("could not attach a plan")
        manager.open_from_order = refuse
        result = cycle(orchestrator)
        self.assertEqual(result.entries_submitted, 1)
        self.assertTrue(any("filled but not managed" in e
                            for e in result.errors))


class TestReporting(unittest.TestCase):

    def test_every_step_is_recorded_individually(self):
        """
        "The cycle failed" is not useful. "Reconciliation succeeded,
        exits succeeded, the scan failed" tells you risk was managed
        before the failure.
        """
        _b, _m, orchestrator = build()
        result = cycle(orchestrator)
        names = {s.name for s in result.steps}
        self.assertIn("reconcile", names)
        self.assertIn("manage_exits", names)
        self.assertIn("consider_entries", names)

    def test_a_skipped_step_says_why(self):
        _b, _m, orchestrator = build()
        result = cycle(orchestrator, phase=CyclePhase.PRE_CLOSE,
                       minutes_to_close=5)
        step = next(s for s in result.steps if s.name == "consider_entries")
        self.assertTrue(step.skipped)
        self.assertTrue(step.skip_reason)

    def test_the_result_records_its_configuration_version(self):
        _b, _m, orchestrator = build()
        self.assertTrue(cycle(orchestrator).config_version)

    def test_halt_reasons_are_not_duplicated(self):
        result = CycleResult(cycle_id="c", session_date="d",
                             phase=CyclePhase.INTRADAY,
                             outcome=CycleOutcome.COMPLETED)
        result.halt(HaltReason.GLOBAL_HALT)
        result.halt(HaltReason.GLOBAL_HALT)
        self.assertEqual(len(result.halt_reasons), 1)

    def test_the_result_serialises(self):
        _b, _m, orchestrator = build()
        data = cycle(orchestrator).as_dict()
        for key in ("outcome", "phase", "halted", "risk_was_managed",
                    "new_exposure_permitted", "steps"):
            with self.subTest(key=key):
                self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
