"""
Failure injection for autonomous paper operation.

Every test here breaks something and then asks one question: was the
SAFEST available state reached? Safest means, in order:

  1. no new exposure while anything is unexplained
  2. exits still running - a sick agent must be able to close what it holds
  3. a human told, for anything that warrants one
  4. nothing silently swallowed

Most scenarios run against the Alpaca adapter on a deliberately hostile
fake venue, because that is where uncertainty actually lives: the
internal broker is synchronous and cannot lose a reply.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

from agent.autonomy import (                                      # noqa: E402
    AlertKind, Condition, HealthState,
)
from agent.broker.alpaca_paper import AlpacaPaperBroker           # noqa: E402
from agent.orchestration import (                                 # noqa: E402
    CycleOutcome, CyclePhase, HaltReason, InMemoryCycleLock,
    MarketDayOrchestrator,
)
from agent.autonomy import AutonomyPolicy, ExecutionMode          # noqa: E402
from agent.positions import ExitPlan, PositionManager             # noqa: E402
from agent.risk import RiskLimits                                 # noqa: E402
from fake_alpaca import ACCEPT, REJECT, RETURN, FakeAlpaca        # noqa: E402
from test_autonomy import (                                       # noqa: E402
    Rig, SESSION, hypothesis_for, quote_for, stopping_quote,
)

POLICIES = (REJECT, ACCEPT, RETURN)


def alpaca_rig(policy=REJECT, **venue_kwargs):
    venue = FakeAlpaca(duplicate_policy=policy, **venue_kwargs)
    broker = AlpacaPaperBroker(transport=venue, sleep=lambda s: None)
    return venue, Rig(broker=broker)


def nothing(symbol):
    return None


def conditions(rig):
    return {a.condition for a in rig.health.snapshot().active}


def alert_kinds(rig):
    return {a.kind for a in rig.alerts.recent()}


class TestDuplicateSchedulerInvocation(unittest.TestCase):
    """EventBridge delivers at-least-once."""

    def test_an_overlapping_invocation_is_refused(self):
        rig = Rig()
        rig.lock.acquire("another-cycle")
        result = rig.cycle()
        self.assertEqual(result.outcome, CycleOutcome.SKIPPED_DUPLICATE)
        self.assertEqual(result.entries_submitted, 0)

    def test_a_duplicate_invocation_does_not_double_the_position(self):
        """The same signal arriving twice, back to back."""
        rig = Rig()
        first = rig.cycle()
        second = rig.cycle()
        self.assertEqual(first.entries_submitted, 1)
        self.assertEqual(second.entries_submitted, 0)
        self.assertEqual(rig.manager.open_count, 1)

    def test_a_duplicate_invocation_against_alpaca_places_one_order(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, rig = alpaca_rig(policy)
                rig.cycle()
                rig.cycle()
                self.assertEqual(venue.order_count(), 1)

    def test_an_unreadable_lock_refuses_the_cycle_and_is_recorded(self):
        class Broken:
            def acquire(self, cid):
                raise RuntimeError("lock table unavailable")

            def release(self, cid):
                pass
        rig = Rig()
        rig.orchestrator.cycle_lock = Broken()
        result = rig.cycle()
        self.assertEqual(result.outcome, CycleOutcome.SKIPPED_DUPLICATE)
        self.assertEqual(result.entries_submitted, 0)
        self.assertIn(Condition.CYCLE_LOCK_FAILURE, conditions(rig))


class TestBrokerTimeoutBeforeAcknowledgement(unittest.TestCase):

    def _uncertain_entry(self, policy=REJECT):
        venue, rig = alpaca_rig(policy)
        venue.drop_before_processing = 1
        result = rig.cycle()
        return venue, rig, result

    def test_no_position_is_recorded_for_an_unconfirmed_order(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, rig, _r = self._uncertain_entry(policy)
                self.assertEqual(rig.manager.open_count, 0)

    def test_entries_stop_and_the_condition_latches(self):
        _v, rig, result = self._uncertain_entry()
        self.assertIn(HaltReason.UNCERTAIN_ORDER, result.halt_reasons)
        self.assertIn(Condition.UNCERTAIN_ORDER_STATE, conditions(rig))
        self.assertIs(rig.health.snapshot().state, HealthState.HALTED)

    def test_a_human_is_alerted(self):
        _v, rig, _r = self._uncertain_entry()
        self.assertIn(AlertKind.UNCERTAIN_ORDER_STATE, alert_kinds(rig))

    def test_the_decision_is_recorded_as_uncertain(self):
        _v, rig, _r = self._uncertain_entry()
        [row] = rig.decisions.for_session(SESSION)
        self.assertEqual(row["outcome"], "ORDER_UNCERTAIN")

    def test_the_next_cycle_does_not_re_enter_with_a_fresh_decision(self):
        """
        The client id is derived from the DECISION id, which is new every
        cycle. So idempotency protects retries of one decision, not a
        fresh decision made next cycle - the latching health condition is
        what stops this from placing a second order around an unknown.
        """
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, rig, _r = self._uncertain_entry(policy)
                posts = venue.post_count
                for _ in range(3):
                    again = rig.cycle()
                    self.assertEqual(again.entries_submitted, 0)
                self.assertEqual(venue.post_count, posts)
                self.assertLessEqual(venue.order_count(), 1)

    def test_the_agent_cannot_clear_the_uncertain_condition_itself(self):
        _v, rig, _r = self._uncertain_entry()
        for _ in range(3):
            rig.cycle()
        self.assertIn(Condition.UNCERTAIN_ORDER_STATE, conditions(rig))


class TestBrokerTimeoutAfterAcknowledgement(unittest.TestCase):

    def test_a_lost_reply_is_resolved_and_the_position_is_managed(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, rig = alpaca_rig(policy)
                venue.lose_response_after_processing = 1
                result = rig.cycle()
                self.assertEqual(result.entries_submitted, 1)
                self.assertEqual(venue.order_count(), 1)
                self.assertEqual(venue.post_count, 1)
                self.assertEqual(rig.manager.open_count, 1)
                self.assertNotIn(Condition.UNCERTAIN_ORDER_STATE,
                                 conditions(rig))

    def test_the_resolved_position_reconciles(self):
        venue, rig = alpaca_rig()
        venue.lose_response_after_processing = 1
        rig.cycle()
        second = rig.cycle(hypothesis=nothing)
        self.assertTrue(second.positions_reconciled)
        self.assertFalse(second.emergency_stop_engaged)

    def test_a_server_error_after_processing_places_exactly_one_order(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                venue, rig = alpaca_rig(policy)
                venue.fail_with_status_after_processing = 503
                rig.cycle()
                self.assertEqual(venue.order_count(), 1)


class TestPartialFill(unittest.TestCase):

    def test_the_remainder_is_cancelled_and_the_position_reconciles(self):
        venue, rig = alpaca_rig()
        venue.fill_fraction = 0.4
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 1)
        self.assertEqual(rig.manager.open_count, 1)
        again = rig.cycle(hypothesis=nothing)
        self.assertTrue(again.positions_reconciled)
        self.assertFalse(again.emergency_stop_engaged)

    def test_the_managed_quantity_is_what_was_filled(self):
        venue, rig = alpaca_rig()
        venue.fill_fraction = 0.4
        rig.cycle()
        [position] = rig.manager.open_positions()
        [held] = rig.broker.get_positions()
        self.assertAlmostEqual(position.quantity, held["quantity"],
                               places=6)

    def test_a_cancel_race_is_detected_not_assumed_away(self):
        """
        The remainder filled before the cancel landed, so the broker
        holds more than the agent recorded. That must surface as a
        reconciliation mismatch and an emergency stop - the agent
        believing it cancelled something that filled is the failure.
        """
        venue, rig = alpaca_rig()
        venue.fill_fraction = 0.4
        real_cancel = rig.broker.cancel_order

        def racing_cancel(order_id):
            order = venue.orders[order_id]
            qty = float(order["qty"])
            order["filled_qty"] = f"{qty:.9f}"
            order["status"] = "filled"
            venue.positions["XYZ"]["qty"] = f"{qty:.9f}"
            return real_cancel(order_id)
        rig.broker.cancel_order = racing_cancel
        rig.cycle()
        result = rig.cycle(hypothesis=nothing)
        self.assertTrue(result.emergency_stop_engaged)
        self.assertIn(Condition.RECONCILIATION_MISMATCH, conditions(rig))


class TestOrderRejection(unittest.TestCase):

    def test_a_rejection_leaves_no_position_and_no_halt(self):
        venue, rig = alpaca_rig()
        venue.reject_next_order = "insufficient"
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 0)
        self.assertEqual(rig.manager.open_count, 0)
        self.assertNotIn(HaltReason.EMERGENCY_STOP, result.halt_reasons)

    def test_the_rejection_is_recorded_as_a_decision(self):
        venue, rig = alpaca_rig()
        venue.reject_next_order = "insufficient"
        rig.cycle()
        [row] = rig.decisions.for_session(SESSION)
        self.assertEqual(row["outcome"], "ORDER_NOT_FILLED")

    def test_a_definite_rejection_does_not_raise_an_alert(self):
        """A rejection is information, not an emergency."""
        venue, rig = alpaca_rig()
        venue.reject_next_order = "x"
        rig.cycle()
        self.assertEqual(alert_kinds(rig), set())

    def test_trading_resumes_after_a_rejection(self):
        venue, rig = alpaca_rig()
        venue.reject_next_order = "x"
        rig.cycle()
        self.assertEqual(rig.cycle().entries_submitted, 1)


class TestStaleMarketData(unittest.TestCase):

    def _stale(self, symbol):
        return {"price": 100.0, "spread_pct": 0.05, "dollar_volume": 5e8,
                "age_seconds": 900.0, "source_age_seconds": 900.0}

    def test_a_stale_quote_blocks_the_entry(self):
        rig = Rig()
        result = rig.cycle(quote=self._stale)
        self.assertEqual(result.entries_submitted, 0)
        self.assertGreater(result.quotes_stale, 0)

    def test_staleness_is_raised_as_a_health_condition(self):
        rig = Rig()
        rig.cycle(quote=self._stale)
        self.assertIn(Condition.STALE_MARKET_DATA, conditions(rig))

    def test_staleness_clears_itself_when_data_is_fresh(self):
        """Non-latching: it describes a cause that passes."""
        rig = Rig()
        rig.cycle(quote=self._stale)
        rig.cycle(hypothesis=nothing)           # fresh quotes, no entry
        rig.cycle()
        self.assertNotIn(Condition.STALE_MARKET_DATA, conditions(rig))

    def test_a_held_position_with_a_stale_quote_is_closed(self):
        """Out is the safe direction when the stop cannot be evaluated."""
        rig = Rig()
        rig.open_position()
        result = rig.cycle(quote=self._stale, hypothesis=nothing)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(rig.manager.open_count, 0)

    def test_a_total_data_outage_blocks_entries_and_closes_positions(self):
        rig = Rig()
        rig.open_position()
        result = rig.cycle(quote=lambda s: None)
        self.assertEqual(result.entries_submitted, 0)
        self.assertGreater(result.exits_submitted, 0)
        self.assertIn(Condition.MARKET_DATA_UNAVAILABLE, conditions(rig))

    def test_a_repeated_outage_raises_one_alert_not_many(self):
        rig = Rig()
        for _ in range(6):
            rig.cycle(quote=lambda s: None)
        outage = [a for a in rig.alerts.recent()
                  if a.kind is AlertKind.REPEATED_PROVIDER_OUTAGE]
        self.assertEqual(len(outage), 1)

    def test_a_single_blip_does_not_alert(self):
        rig = Rig()
        rig.cycle(quote=lambda s: None)
        self.assertNotIn(AlertKind.REPEATED_PROVIDER_OUTAGE,
                         alert_kinds(rig))


class TestDependencyOutages(unittest.TestCase):

    def test_a_scanner_outage_means_no_candidates_and_no_abort(self):
        """Fewer entries is the safe direction. Exits still run."""
        rig = Rig()
        rig.open_position()
        result = rig.cycle(candidates=(), quote=stopping_quote)
        self.assertNotEqual(result.outcome, CycleOutcome.ABORTED)
        self.assertGreater(result.exits_submitted, 0)

    def test_an_evidence_outage_does_not_stop_trading(self):
        """
        Evidence is not collected inside the cycle at all, so an outage
        cannot make an entry riskier than the no-evidence case the
        hypothesis engine already handles.
        """
        rig = Rig()
        rig.health.raise_condition(Condition.EVIDENCE_PROVIDER_DEGRADED)

        def no_evidence(symbol):
            signal = {"direction": "BUY", "signal_agreement": 1.0,
                      "signal_magnitude": 0.7,
                      "regime_adjusted_magnitude": 0.7,
                      "strength_band": "STRONG", "buy_groups": 3,
                      "sell_groups": 0, "opinionated_groups": 3,
                      "data_quality": {"freshness": "FRESH"}}
            regime = {"regime": "BULLISH", "regime_confidence": 0.8,
                      "risk_posture": "NORMAL", "market_session": "OPEN"}
            from agent.hypothesis import generate
            return generate(symbol, signal, None, regime)
        result = rig.cycle(hypothesis=no_evidence)
        self.assertNotEqual(result.outcome, CycleOutcome.ABORTED)
        [row] = rig.decisions.for_session(SESSION)
        codes = [c["code"] for c in row["hypothesis"]["contradictions"]]
        self.assertIn("EVIDENCE_NOT_COLLECTED", codes)

    def test_an_llm_outage_does_not_block_trading_or_exits(self):
        """No decision depends on the LLM. It only explains."""
        rig = Rig()
        rig.open_position()
        rig.health.raise_condition(Condition.LLM_UNAVAILABLE)
        result = rig.cycle(quote=stopping_quote, hypothesis=nothing)
        self.assertGreater(result.exits_submitted, 0)
        # A DIFFERENT symbol: XYZ was just exited and is cooling down.
        from agent.broker import Quote
        rig.broker.set_quote(Quote(symbol="AAA", bid=99.9, ask=100.1,
                                   last=100.0))
        self.assertEqual(
            rig.cycle(candidates=("AAA",)).entries_submitted, 1)

    def test_no_decision_module_imports_an_llm_client(self):
        """
        Checked against the source: the deterministic core cannot be
        affected by an LLM outage because it does not import one.
        """
        for module in ("hypothesis", "risk", "positions", "orchestration",
                       "broker", "journal"):
            directory = os.path.join(REPO_ROOT, "agent", module)
            for name in os.listdir(directory):
                if not name.endswith(".py"):
                    continue
                body = open(os.path.join(directory, name)).read().lower()
                for banned in ("import openai", "from openai",
                               "chat.completions", "gpt-"):
                    with self.subTest(file=f"{module}/{name}",
                                      banned=banned):
                        self.assertNotIn(banned, body)

    def test_falsifying_control_the_import_scan_can_match(self):
        self.assertIn("import openai", "import openai\n")


class TestPersistenceFailures(unittest.TestCase):

    def test_a_journal_write_failure_does_not_unclose_the_position(self):
        rig = Rig()
        rig.open_position()

        class Broken:
            def record(self, trade):
                raise RuntimeError("dynamo write failed")

            def list_trades(self, **kw):
                return []
        rig.orchestrator.journal = Broken()
        result = rig.cycle(quote=stopping_quote, hypothesis=nothing)
        self.assertEqual(rig.manager.open_count, 0)
        self.assertEqual(result.exits_failed, 0)

    def test_a_journal_failure_latches_and_alerts(self):
        rig = Rig()
        rig.open_position()

        class Broken:
            def record(self, trade):
                raise RuntimeError("dynamo write failed")
        rig.orchestrator.journal = Broken()
        rig.cycle(quote=stopping_quote, hypothesis=nothing)
        self.assertIn(Condition.JOURNAL_PERSISTENCE_FAILURE, conditions(rig))
        self.assertIn(AlertKind.JOURNAL_PERSISTENCE_FAILURE,
                      alert_kinds(rig))

    def test_a_journal_failure_blocks_further_entries(self):
        """
        Trading on with an incomplete record would produce a performance
        history that silently omits trades.
        """
        rig = Rig()
        rig.health.raise_condition(Condition.JOURNAL_PERSISTENCE_FAILURE)
        self.assertEqual(rig.cycle().entries_submitted, 0)

    def test_a_halt_that_cannot_be_persisted_still_halts_this_cycle(self):
        class Broken(type(Rig().halt_store)):
            def engage(self, reason, engaged_by="system"):
                raise RuntimeError("dynamo write failed")
        venue, rig = alpaca_rig()
        rig.cycle()
        rig.orchestrator.halt_store = Broken()
        venue.positions.clear()                      # force a mismatch
        result = rig.cycle(hypothesis=nothing)
        self.assertTrue(result.emergency_stop_engaged)
        self.assertEqual(result.entries_submitted, 0)
        self.assertTrue(any("halt could not be persisted" in e
                            for e in result.errors))

    def test_a_health_write_failure_does_not_stop_the_exits(self):
        class Broken(type(Rig().health)):
            def raise_condition(self, condition, detail=""):
                raise RuntimeError("dynamo write failed")
        rig = Rig()
        rig.open_position()
        rig.orchestrator.health = Broken()
        result = rig.cycle(quote=lambda s: None, hypothesis=nothing)
        self.assertGreater(result.exits_submitted, 0)


class TestReconciliationMismatch(unittest.TestCase):

    def _mismatched(self):
        venue, rig = alpaca_rig()
        rig.cycle()
        venue.positions.clear()           # the broker no longer has it
        result = rig.cycle(hypothesis=nothing)
        return venue, rig, result

    def test_a_mismatch_engages_an_emergency_stop(self):
        _v, rig, result = self._mismatched()
        self.assertTrue(result.emergency_stop_engaged)
        self.assertTrue(rig.halt_store.get().halted)

    def test_no_new_exposure_follows(self):
        _v, rig, result = self._mismatched()
        self.assertEqual(result.entries_submitted, 0)
        again = rig.cycle()
        self.assertEqual(again.entries_submitted, 0)
        self.assertIn(HaltReason.GLOBAL_HALT, again.halt_reasons)

    def test_the_mismatch_alerts_critically(self):
        _v, rig, _r = self._mismatched()
        kinds = alert_kinds(rig)
        self.assertIn(AlertKind.EMERGENCY_STOP, kinds)
        self.assertIn(AlertKind.RECONCILIATION_MISMATCH, kinds)

    def test_the_condition_latches_and_the_agent_cannot_clear_it(self):
        _v, rig, _r = self._mismatched()
        for _ in range(3):
            rig.cycle()
        self.assertIn(Condition.RECONCILIATION_MISMATCH, conditions(rig))
        self.assertIn(Condition.EMERGENCY_STOP, conditions(rig))

    def test_an_unreadable_broker_is_not_an_emergency_stop(self):
        """
        An outage is transient and clears when the broker returns. A
        broker that was READ and DISAGREES is the emergency.
        """
        venue, rig = alpaca_rig()
        rig.cycle()
        venue.unreadable = True
        result = rig.cycle(hypothesis=nothing)
        self.assertFalse(result.emergency_stop_engaged)
        self.assertIn(Condition.BROKER_UNAVAILABLE, conditions(rig))
        self.assertEqual(result.entries_submitted, 0)

    def test_an_unreadable_broker_recovers_when_it_returns(self):
        venue, rig = alpaca_rig()
        rig.cycle()
        venue.unreadable = True
        rig.cycle(hypothesis=nothing)
        venue.unreadable = False
        rig.cycle(hypothesis=nothing)
        self.assertNotIn(Condition.BROKER_UNAVAILABLE, conditions(rig))

    def test_a_matching_broker_raises_nothing(self):
        """The falsifying control: reconciliation must be able to pass."""
        venue, rig = alpaca_rig()
        rig.cycle()
        result = rig.cycle(hypothesis=nothing)
        self.assertTrue(result.positions_reconciled)
        self.assertFalse(result.emergency_stop_engaged)
        self.assertEqual(alert_kinds(rig), set())


class TestUnexpectedExternalPosition(unittest.TestCase):

    def _with_foreign_position(self):
        venue, rig = alpaca_rig()
        venue.positions["NVDA"] = {
            "symbol": "NVDA", "qty": "5", "avg_entry_price": "100",
            "current_price": "100", "market_value": "500",
            "unrealized_pl": "0", "cost_basis": "500", "side": "long"}
        result = rig.cycle(hypothesis=nothing)
        return venue, rig, result

    def test_it_engages_an_emergency_stop(self):
        _v, _r, result = self._with_foreign_position()
        self.assertTrue(result.emergency_stop_engaged)

    def test_it_alerts_naming_the_symbol(self):
        _v, rig, _r = self._with_foreign_position()
        self.assertIn(AlertKind.UNEXPECTED_BROKER_POSITION, alert_kinds(rig))
        alert = next(a for a in rig.alerts.recent()
                     if a.kind is AlertKind.UNEXPECTED_BROKER_POSITION)
        self.assertIn("NVDA", alert.detail)

    def test_it_is_NOT_closed_automatically(self):
        """
        It has no plan and no record. Acting on something unexplained is
        exactly the guess the emergency stop exists to prevent, so a
        human decides.
        """
        venue, _r, _res = self._with_foreign_position()
        self.assertIn("NVDA", venue.positions)

    def test_no_entries_follow(self):
        _v, rig, _r = self._with_foreign_position()
        self.assertEqual(rig.cycle().entries_submitted, 0)

    def test_our_own_position_is_still_managed_beside_it(self):
        venue, rig = alpaca_rig()
        rig.cycle()                                   # opens XYZ
        venue.positions["NVDA"] = {
            "symbol": "NVDA", "qty": "5", "avg_entry_price": "100",
            "current_price": "100", "market_value": "500",
            "unrealized_pl": "0", "cost_basis": "500", "side": "long"}
        venue.price = 96.0
        result = rig.cycle(quote=stopping_quote, hypothesis=nothing)
        self.assertTrue(result.emergency_stop_engaged)
        self.assertGreater(result.exits_submitted, 0)


class TestCycleCrashes(unittest.TestCase):

    def test_a_crash_after_the_fill_is_flagged_and_then_halts(self):
        """
        The broker holds a position the agent failed to record. The
        failure must be loud this cycle and an emergency stop the next -
        never a quiet unmanaged holding.
        """
        venue, rig = alpaca_rig()

        def crash(*a, **k):
            raise RuntimeError("process state lost")
        rig.manager.open_from_order = crash
        first = rig.cycle()
        self.assertTrue(any("filled but not managed" in e
                            for e in first.errors))
        self.assertEqual(rig.manager.open_count, 0)
        self.assertEqual(len(venue.positions), 1)

        second = rig.cycle(hypothesis=nothing)
        self.assertTrue(second.emergency_stop_engaged)
        self.assertIn(AlertKind.UNEXPECTED_BROKER_POSITION,
                      alert_kinds(rig))
        self.assertEqual(rig.cycle().entries_submitted, 0)

    def test_an_unhandled_error_aborts_without_undoing_exits(self):
        rig = Rig()
        rig.open_position()

        def boom(*a, **k):
            raise RuntimeError("entry path broken")
        rig.orchestrator._consider_entries = boom
        result = rig.cycle(quote=stopping_quote)
        self.assertEqual(result.outcome, CycleOutcome.ABORTED)
        self.assertGreater(result.exits_submitted, 0)
        self.assertTrue(result.risk_was_managed)

    def test_repeated_aborts_raise_a_repeated_failure_alert(self):
        rig = Rig()

        def boom(*a, **k):
            raise RuntimeError("entry path broken")
        rig.orchestrator._consider_entries = boom
        for _ in range(3):
            rig.cycle()
        self.assertIn(AlertKind.REPEATED_CYCLE_FAILURE, alert_kinds(rig))
        self.assertIn(Condition.REPEATED_CYCLE_FAILURE, conditions(rig))

    def test_two_aborts_do_not_yet_alert(self):
        rig = Rig()

        def boom(*a, **k):
            raise RuntimeError("entry path broken")
        rig.orchestrator._consider_entries = boom
        for _ in range(2):
            rig.cycle()
        self.assertNotIn(AlertKind.REPEATED_CYCLE_FAILURE, alert_kinds(rig))

    def test_a_success_resets_the_failure_streak(self):
        rig = Rig()
        original = rig.orchestrator._consider_entries

        def boom(*a, **k):
            raise RuntimeError("x")
        rig.orchestrator._consider_entries = boom
        rig.cycle()
        rig.cycle()
        rig.orchestrator._consider_entries = original
        rig.cycle(hypothesis=nothing)
        rig.orchestrator._consider_entries = boom
        rig.cycle()
        rig.cycle()
        self.assertNotIn(Condition.REPEATED_CYCLE_FAILURE, conditions(rig))

    def test_a_kill_between_the_broker_close_and_the_local_record(self):
        """
        The process dies after the broker closed the position but before
        the agent recorded it. A cold start restores the OLD position
        set, finds the broker flat, and must halt rather than try to
        close a position that no longer exists.
        """
        class Killed(BaseException):
            """Escapes `except Exception`, as a real kill would."""

        venue, rig = alpaca_rig()
        rig.cycle()
        saved = list(rig.manager.open_positions())
        self.assertEqual(len(saved), 1)

        real_close = rig.broker.close_position

        def close_then_die(symbol, intent="EXIT"):
            real_close(symbol, intent=intent)
            raise Killed()
        rig.broker.close_position = close_then_die
        with self.assertRaises(Killed):
            rig.cycle(quote=stopping_quote, hypothesis=nothing)
        self.assertEqual(venue.positions, {})

        # Cold start: a fresh manager restored from the stale position set.
        rig2 = Rig(broker=AlpacaPaperBroker(transport=venue,
                                            sleep=lambda s: None))
        for position in saved:
            rig2.manager._positions[position.symbol] = position
        result = rig2.cycle(hypothesis=nothing)
        self.assertTrue(result.emergency_stop_engaged)
        self.assertEqual(result.entries_submitted, 0)

    def test_the_lock_is_released_even_when_the_process_is_killed(self):
        """
        The `finally` releases it. And if even that fails, the lease
        expires - covered by the lock tests - so a crash cannot deadlock
        the session.
        """
        class Killed(BaseException):
            pass
        rig = Rig()

        def boom(*a, **k):
            raise Killed()
        rig.orchestrator._consider_entries = boom
        with self.assertRaises(Killed):
            rig.cycle()
        self.assertIsNone(rig.lock.held_by)


class TestEndOfDayFlatten(unittest.TestCase):

    def test_a_successful_flatten_leaves_nothing_open(self):
        rig = Rig()
        rig.open_position(stop=1.0)
        result = rig.cycle(phase=CyclePhase.PRE_CLOSE, minutes_to_close=5,
                           hypothesis=nothing)
        self.assertEqual(rig.manager.open_count, 0)
        self.assertFalse(result.eod_flatten_failed)

    def test_a_failed_flatten_is_flagged_and_alerts(self):
        rig = Rig()
        rig.open_position(stop=1.0)

        def refuse(symbol, intent="EXIT"):
            raise RuntimeError("broker rejected the close")
        rig.broker.close_position = refuse
        result = rig.cycle(phase=CyclePhase.PRE_CLOSE, minutes_to_close=5,
                           hypothesis=nothing)
        self.assertTrue(result.eod_flatten_failed)
        self.assertEqual(rig.manager.open_count, 1)
        self.assertIn(Condition.EOD_FLATTEN_FAILURE, conditions(rig))
        kinds = alert_kinds(rig)
        self.assertIn(AlertKind.EOD_FLATTEN_FAILURE, kinds)
        self.assertIn(AlertKind.POSITION_OPEN_NEAR_CLOSE, kinds)

    def test_a_position_open_with_time_to_spare_does_not_raise_the_near_close_alert(self):
        rig = Rig()
        rig.open_position(stop=1.0)

        def refuse(symbol, intent="EXIT"):
            raise RuntimeError("rejected")
        rig.broker.close_position = refuse
        rig.cycle(phase=CyclePhase.PRE_CLOSE, minutes_to_close=28,
                  hypothesis=nothing)
        self.assertNotIn(AlertKind.POSITION_OPEN_NEAR_CLOSE,
                         alert_kinds(rig))

    def test_a_failed_flatten_blocks_entries_next_cycle(self):
        rig = Rig()
        rig.health.raise_condition(Condition.EOD_FLATTEN_FAILURE)
        self.assertEqual(rig.cycle().entries_submitted, 0)

    def test_the_flatten_is_retried_on_the_next_cycle(self):
        rig = Rig()
        rig.open_position(stop=1.0)
        real = rig.broker.close_position
        attempts = []

        def flaky(symbol, intent="EXIT"):
            attempts.append(symbol)
            if len(attempts) == 1:
                raise RuntimeError("transient")
            return real(symbol, intent=intent)
        rig.broker.close_position = flaky
        rig.cycle(phase=CyclePhase.PRE_CLOSE, minutes_to_close=20,
                  hypothesis=nothing)
        rig.cycle(phase=CyclePhase.PRE_CLOSE, minutes_to_close=15,
                  hypothesis=nothing)
        self.assertEqual(rig.manager.open_count, 0)

    def test_a_flatten_against_alpaca_closes_the_broker_position(self):
        venue, rig = alpaca_rig()
        rig.cycle()
        self.assertEqual(len(venue.positions), 1)
        rig.cycle(phase=CyclePhase.PRE_CLOSE, minutes_to_close=5,
                  hypothesis=nothing)
        self.assertEqual(venue.positions, {})
        self.assertEqual(rig.manager.open_count, 0)


class TestEmergencyStopDuringAnOpenPosition(unittest.TestCase):

    def test_the_position_is_closed_and_no_entry_is_made(self):
        rig = Rig()
        rig.open_position()
        rig.halt_store.engage("operator halt", engaged_by="operator")
        result = rig.cycle()
        self.assertEqual(rig.manager.open_count, 0)
        self.assertEqual(result.entries_submitted, 0)
        self.assertIn(HaltReason.GLOBAL_HALT, result.halt_reasons)

    def test_the_exit_is_recorded_as_a_halt_exit(self):
        rig = Rig()
        rig.open_position()
        rig.halt_store.engage("operator halt", engaged_by="operator")
        rig.cycle(hypothesis=nothing)
        [trade] = rig.journal.list_trades()
        self.assertIn("GLOBAL_HALT", trade.all_exit_reasons)

    def test_a_halt_does_not_lift_itself_after_the_position_closes(self):
        rig = Rig()
        rig.open_position()
        rig.halt_store.engage("operator halt", engaged_by="operator")
        rig.cycle(hypothesis=nothing)
        again = rig.cycle()
        self.assertEqual(again.entries_submitted, 0)
        self.assertTrue(rig.halt_store.get().halted)


class TestDuplicateOrderAttemptAlert(unittest.TestCase):

    def test_a_suppressed_duplicate_is_flagged_to_a_human(self):
        """
        The broker refusing a duplicate is the system working, but it
        means something tried to place an order twice, and someone
        should know why.
        """
        rig = Rig()
        real = rig.broker.submit_order

        def duplicate(*a, **k):
            rig.broker.duplicate_attempts = getattr(
                rig.broker, "duplicate_attempts", 0) + 1
            return real(*a, **k)
        rig.broker.submit_order = duplicate
        result = rig.cycle()
        self.assertEqual(result.duplicate_attempts, 1)
        self.assertIn(AlertKind.DUPLICATE_ORDER_ATTEMPT, alert_kinds(rig))
        self.assertIn(Condition.DUPLICATE_ORDER_ATTEMPT, conditions(rig))

    def test_a_cycle_with_no_duplicates_does_not_flag(self):
        rig = Rig()
        result = rig.cycle()
        self.assertEqual(result.duplicate_attempts, 0)
        self.assertNotIn(AlertKind.DUPLICATE_ORDER_ATTEMPT,
                         alert_kinds(rig))


class TestTheSafestStateIsAlwaysReachable(unittest.TestCase):
    """
    The property under all of the above: whatever is broken, exits keep
    running. Parametrised over every health condition.
    """

    def test_every_condition_still_lets_a_position_close(self):
        for condition in Condition:
            with self.subTest(condition=condition):
                rig = Rig()
                rig.open_position()
                rig.health.raise_condition(condition, "injected")
                result = rig.cycle(quote=stopping_quote, hypothesis=nothing)
                self.assertGreater(
                    result.exits_submitted, 0,
                    f"{condition} stopped a position from closing")

    def test_every_halting_condition_stops_entries(self):
        """
        CYCLE_LOCK_FAILURE is the one exception, and a principled one:
        the cycle clears it the moment it successfully ACQUIRES the lock,
        because a lock that just worked proves there is no overlap this
        cycle. Covered separately below.
        """
        from agent.autonomy.health import HALTING
        for condition in HALTING - {Condition.CYCLE_LOCK_FAILURE}:
            with self.subTest(condition=condition):
                rig = Rig()
                rig.health.raise_condition(condition, "injected")
                self.assertEqual(rig.cycle().entries_submitted, 0)

    def test_a_lock_failure_clears_once_the_lock_is_acquired(self):
        rig = Rig()
        rig.health.raise_condition(Condition.CYCLE_LOCK_FAILURE, "earlier")
        rig.cycle(hypothesis=nothing)
        self.assertNotIn(Condition.CYCLE_LOCK_FAILURE, conditions(rig))

    def test_a_lock_failure_persists_while_the_lock_stays_unreadable(self):
        class Broken:
            def acquire(self, cid):
                raise RuntimeError("down")

            def release(self, cid):
                pass
        rig = Rig()
        rig.orchestrator.cycle_lock = Broken()
        for _ in range(3):
            rig.cycle()
        self.assertIn(Condition.CYCLE_LOCK_FAILURE, conditions(rig))


class TestMarketDataDeadlock(unittest.TestCase):
    """
    A non-latching data condition blocks entries, a blocked cycle
    requests no quotes, so with nothing to observe it could never clear.
    One stale quote would have halted trading until a human stepped in.
    Found when a test expected staleness to clear itself and it did not.
    """

    def _stale(self, symbol):
        return {"price": 100.0, "spread_pct": 0.05, "dollar_volume": 5e8,
                "age_seconds": 900.0, "source_age_seconds": 900.0}

    def test_staleness_recovers_with_no_positions_and_entries_blocked(self):
        rig = Rig()
        rig.cycle(quote=self._stale)
        self.assertIn(Condition.STALE_MARKET_DATA, conditions(rig))
        # Data is fresh again. No position, no entry possible this cycle.
        rig.cycle(hypothesis=nothing)
        self.assertNotIn(Condition.STALE_MARKET_DATA, conditions(rig))
        self.assertEqual(rig.cycle().entries_submitted, 1)

    def test_an_outage_recovers_the_same_way(self):
        rig = Rig()
        rig.cycle(quote=lambda s: None)
        self.assertIn(Condition.MARKET_DATA_UNAVAILABLE, conditions(rig))
        rig.cycle(hypothesis=nothing)
        self.assertNotIn(Condition.MARKET_DATA_UNAVAILABLE, conditions(rig))

    def test_recovery_works_with_no_candidates_via_the_canary(self):
        """A scanner outage leaves no symbol to probe with."""
        rig = Rig()
        rig.cycle(quote=self._stale)
        asked = []

        def fresh(symbol):
            asked.append(symbol)
            return {"price": 100.0, "spread_pct": 0.05,
                    "dollar_volume": 5e8, "age_seconds": 3.0,
                    "source_age_seconds": 3.0}
        rig.cycle(candidates=(), quote=fresh)
        self.assertEqual(asked, ["SPY"])
        self.assertNotIn(Condition.STALE_MARKET_DATA, conditions(rig))

    def test_the_condition_stays_while_the_data_stays_bad(self):
        """The falsifying control: the probe must not clear it blindly."""
        rig = Rig()
        for _ in range(4):
            rig.cycle(quote=self._stale)
        self.assertIn(Condition.STALE_MARKET_DATA, conditions(rig))

    def test_the_probe_costs_nothing_in_normal_operation(self):
        """No data condition active, so no extra quote requests."""
        rig = Rig()
        asked = []

        def counting(symbol):
            asked.append(symbol)
            return quote_for(symbol)
        rig.cycle(hypothesis=nothing, quote=counting)
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
