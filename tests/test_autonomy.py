"""
Autonomy: the policy, health, alerts, and the orchestrator under them.

Autonomy here means no individual PAPER trade needs a human. It does not
mean the agent decides what it may do. These tests are mostly attempts
to get the agent to exceed its authority or to keep trading while
something is wrong, and each must fail.

The shape of most tests: break something, then check the SAFEST state
was reached - entries stopped, exits still running, a human told.
"""
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

from agent.autonomy import (                                      # noqa: E402
    FORBIDDEN, PERMITTED, Action, AlertKind, AutonomyPolicy, Condition,
    ExecutionMode, HealthSnapshot, HealthState, InMemoryAlertSink,
    InMemoryDecisionLog, InMemoryHealthStore, LiveExecutionRefused,
    PolicyViolation, Severity, TOLERATED_FOR_ENTRIES, ActiveCondition,
    daily_counters, policy_from_environment,
)
from agent.autonomy.alerts import Alert                           # noqa: E402
from agent.autonomy import health as health_module                # noqa: E402
from agent.broker import (                                        # noqa: E402
    PaperBroker, PaperBrokerConfig, Quote,
)
from agent.broker.order_ledger import (                           # noqa: E402
    ExternalOrderRecord, InMemoryOrderLedger, OrderLedgerError,
)
from agent.hypothesis import generate                             # noqa: E402
from agent.journal import InMemoryJournal                         # noqa: E402
from agent.orchestration import (                                 # noqa: E402
    CycleOutcome, CyclePhase, HaltReason, InMemoryCycleLock,
    MarketDayOrchestrator, phase_from_session,
)
from agent.positions import ExitPlan, PositionManager             # noqa: E402
from agent.risk import InMemoryHaltStore, RiskLimits               # noqa: E402

SESSION = "2026-10-01"


def quote_for(symbol):
    return {"price": 100.0, "spread_pct": 0.05, "dollar_volume": 5e8,
            "age_seconds": 5.0, "source_age_seconds": 5.0,
            "feed_quality": "REALTIME_SIP"}


def stopping_quote(symbol):
    """Through the stop, inside the daily loss limit."""
    return {"price": 96.0, "spread_pct": 0.05, "dollar_volume": 5e8,
            "age_seconds": 5.0, "source_age_seconds": 5.0,
            "feed_quality": "REALTIME_SIP"}


def disastrous_quote(symbol):
    """A loss well beyond the $5 daily limit."""
    return {"price": 80.0, "spread_pct": 0.05, "dollar_volume": 5e8,
            "age_seconds": 5.0, "source_age_seconds": 5.0,
            "feed_quality": "REALTIME_SIP"}


def hypothesis_for(symbol):
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


class Rig:
    """An orchestrator wired with every autonomy dependency."""

    def __init__(self, mode=ExecutionMode.PAPER, cash=1000.0, broker=None,
                 with_lock=True, limits=None):
        self.broker = broker or PaperBroker(PaperBrokerConfig(
            starting_cash=cash, seed=3, partial_fill_probability=0.0))
        if isinstance(self.broker, PaperBroker):
            self.broker.set_quote(Quote(symbol="XYZ", bid=99.9, ask=100.1,
                                        last=100.0))
        self.manager = PositionManager(broker=self.broker,
                                       execution_available=True)
        self.journal = InMemoryJournal()
        self.halt_store = InMemoryHaltStore()
        self.health = InMemoryHealthStore()
        self.alerts = InMemoryAlertSink()
        self.decisions = InMemoryDecisionLog()
        # Supplied for every broker, not only the external one. The
        # record is useful either way, and a rig that only wires it for
        # Alpaca would leave the simulator path untested against it.
        self.ledger = InMemoryOrderLedger()
        self.lock = InMemoryCycleLock() if with_lock else None
        self.versions = {"strategy": "s1", "risk": "r1", "exits": "e1",
                         "orchestration": "o1", "code_sha": "abc"}
        self.orchestrator = MarketDayOrchestrator(
            broker=self.broker, position_manager=self.manager,
            journal=self.journal, halt_store=self.halt_store,
            limits=limits or RiskLimits(), cycle_lock=self.lock,
            autonomy=AutonomyPolicy(mode=mode), health=self.health,
            alerts=self.alerts, decisions=self.decisions,
            versions=self.versions, order_ledger=self.ledger,
            cohort="TEST")

    def cycle(self, phase=CyclePhase.INTRADAY, quote=quote_for,
              hypothesis=hypothesis_for, candidates=("XYZ",),
              minutes_to_close=200, **kw):
        return self.orchestrator.run_cycle(
            session_date=SESSION, phase=phase, candidates=list(candidates),
            quote_for=quote, hypothesis_for=hypothesis,
            minutes_to_close=minutes_to_close, **kw)

    def open_position(self, stop=97.0):
        order = self.broker.submit_order("XYZ", "BUY", 1.0)
        self.manager.open_from_order(
            order, ExitPlan(stop_price=stop), hypothesis_id="h1",
            risk_decision_id="d1")
        self.orchestrator._entry_orders["XYZ"] = order
        return order


# ===========================================================================
# POLICY
# ===========================================================================

class TestLiveIsNotReachable(unittest.TestCase):
    """The most important property in the milestone."""

    def test_a_live_policy_cannot_be_constructed(self):
        with self.assertRaises(LiveExecutionRefused):
            AutonomyPolicy(mode=ExecutionMode.LIVE)

    def test_live_trading_is_never_enabled(self):
        for mode in (ExecutionMode.DISABLED, ExecutionMode.PAPER):
            with self.subTest(mode=mode):
                self.assertFalse(AutonomyPolicy(mode=mode)
                                 .live_trading_enabled)

    def test_live_trading_enabled_cannot_be_assigned(self):
        with self.assertRaises((AttributeError, Exception)):
            AutonomyPolicy(mode=ExecutionMode.PAPER)\
                .live_trading_enabled = True

    def test_the_policy_is_frozen(self):
        """The agent cannot edit its own permissions."""
        policy = AutonomyPolicy(mode=ExecutionMode.PAPER)
        with self.assertRaises(Exception):
            policy.mode = ExecutionMode.DISABLED

    def test_live_in_the_environment_becomes_disabled_not_live(self):
        """
        A deployment told to go live must STOP, not trade. Raising out of
        a Lambda could be retried into something; DISABLED cannot.
        """
        policy = policy_from_environment("LIVE")
        self.assertIs(policy.mode, ExecutionMode.DISABLED)
        self.assertFalse(policy.may_open_new_exposure)

    def test_unrecognised_modes_fail_closed(self):
        for value in ("", None, "paper ", "PAPER2", "yes", "true", "1"):
            with self.subTest(value=value):
                mode = ExecutionMode.parse(value)
                # "paper " with a trailing space parses; everything else
                # must be DISABLED.
                if str(value).strip().upper() == "PAPER":
                    self.assertIs(mode, ExecutionMode.PAPER)
                else:
                    self.assertIs(mode, ExecutionMode.DISABLED)

    def test_the_orchestrator_cannot_be_built_in_live_mode(self):
        with self.assertRaises(LiveExecutionRefused):
            Rig(mode=ExecutionMode.LIVE)

    def test_paper_mode_refuses_a_broker_that_does_not_declare_paper(self):
        """
        PAPER must not be able to reach a real broker. Checked by what
        the broker says it is, and an adapter that cannot say is
        refused.
        """
        class Mystery:
            def get_positions(self):
                return []

        class Pretender:
            def capabilities(self):
                return {"is_paper": False}

            def get_positions(self):
                return []
        for broker in (Mystery(), Pretender()):
            with self.subTest(broker=type(broker).__name__):
                with self.assertRaises(PolicyViolation):
                    Rig(broker=broker)

    def test_paper_mode_accepts_both_paper_brokers(self):
        from agent.broker.alpaca_paper import AlpacaPaperBroker
        from fake_alpaca import FakeAlpaca
        Rig()                                              # internal
        Rig(broker=AlpacaPaperBroker(transport=FakeAlpaca()))

    def test_a_raising_capabilities_call_is_treated_as_not_paper(self):
        class Opaque:
            def capabilities(self):
                raise RuntimeError("nope")
        with self.assertRaises(PolicyViolation):
            Rig(broker=Opaque())

    def test_the_legacy_booleans_are_derived_from_the_policy(self):
        """
        They cannot disagree with it and cannot be set independently:
        one boolean once meant both "trading is on" and "this can reach
        real money".
        """
        rig = Rig(mode=ExecutionMode.PAPER)
        self.assertTrue(rig.orchestrator.trading_enabled)
        self.assertTrue(rig.orchestrator.execution_available)
        off = Rig(mode=ExecutionMode.DISABLED)
        self.assertFalse(off.orchestrator.trading_enabled)
        self.assertFalse(off.orchestrator.execution_available)


class TestPolicyPartition(unittest.TestCase):

    def test_every_action_is_permitted_or_forbidden_never_both(self):
        self.assertEqual(PERMITTED & FORBIDDEN, frozenset())
        self.assertEqual(set(Action), set(PERMITTED) | set(FORBIDDEN))

    def test_the_actions_the_agent_must_not_take_are_all_forbidden(self):
        for action in (Action.ENABLE_REAL_EXECUTION, Action.SELECT_LIVE_MODE,
                       Action.RAISE_RISK_CEILING,
                       Action.INCREASE_DAILY_CAPITAL, Action.ENABLE_MARGIN,
                       Action.ENABLE_SHORTING, Action.ENABLE_OPTIONS,
                       Action.ALLOW_OVERNIGHT_POSITIONS,
                       Action.CHANGE_BROKER, Action.DISABLE_RECONCILIATION,
                       Action.CLEAR_CRITICAL_HALT, Action.WIDEN_STOP,
                       Action.CLEAR_EMERGENCY_STOP, Action.EDIT_OWN_POLICY):
            with self.subTest(action=action):
                self.assertIn(action, FORBIDDEN)
                self.assertFalse(
                    AutonomyPolicy(mode=ExecutionMode.PAPER).permits(action))

    def test_the_actions_the_agent_may_take_are_permitted_in_paper(self):
        policy = AutonomyPolicy(mode=ExecutionMode.PAPER)
        for action in (Action.SUBMIT_PAPER_ORDER, Action.CANCEL_PAPER_ORDER,
                       Action.TIGHTEN_STOP, Action.EXIT_PAPER_POSITION,
                       Action.FLATTEN_POSITIONS, Action.ENGAGE_RISK_LOCK,
                       Action.ENGAGE_EMERGENCY_STOP,
                       Action.TAKE_ZERO_TRADES, Action.REJECT_HYPOTHESIS):
            with self.subTest(action=action):
                self.assertTrue(policy.permits(action))

    def test_the_agent_may_engage_a_protection_but_never_clear_one(self):
        """
        Lifting a halt is how a system talks itself back into the
        situation that caused it.
        """
        policy = AutonomyPolicy(mode=ExecutionMode.PAPER)
        self.assertTrue(policy.permits(Action.ENGAGE_EMERGENCY_STOP))
        self.assertFalse(policy.permits(Action.CLEAR_EMERGENCY_STOP))
        self.assertTrue(policy.permits(Action.ENGAGE_RISK_LOCK))
        self.assertFalse(policy.permits(Action.CLEAR_CRITICAL_HALT))

    def test_disabled_mode_cannot_originate_orders_but_can_reduce_risk(self):
        policy = AutonomyPolicy(mode=ExecutionMode.DISABLED)
        self.assertFalse(policy.permits(Action.SUBMIT_PAPER_ORDER))
        for action in (Action.EXIT_PAPER_POSITION, Action.FLATTEN_POSITIONS,
                       Action.CANCEL_PAPER_ORDER):
            with self.subTest(action=action):
                self.assertTrue(policy.permits(action))

    def test_require_raises_on_a_forbidden_action(self):
        policy = AutonomyPolicy(mode=ExecutionMode.PAPER)
        with self.assertRaises(PolicyViolation):
            policy.require(Action.ENABLE_REAL_EXECUTION)

    def test_require_passes_on_a_permitted_action(self):
        AutonomyPolicy(mode=ExecutionMode.PAPER).require(
            Action.SUBMIT_PAPER_ORDER)

    def test_the_serialised_policy_states_real_money_is_disabled(self):
        data = AutonomyPolicy(mode=ExecutionMode.PAPER).as_dict()
        self.assertTrue(data["real_money_disabled"])
        self.assertFalse(data["live_trading_enabled"])


# ===========================================================================
# HEALTH
# ===========================================================================

class TestHealthIsDerived(unittest.TestCase):

    def _snap(self, *conditions):
        return HealthSnapshot(active=[
            ActiveCondition(condition=c, since="t") for c in conditions])

    def test_no_conditions_is_healthy(self):
        snap = self._snap()
        self.assertIs(snap.state, HealthState.HEALTHY)
        self.assertTrue(snap.entries_permitted)

    def test_a_halting_condition_halts(self):
        snap = self._snap(Condition.RECONCILIATION_MISMATCH)
        self.assertIs(snap.state, HealthState.HALTED)
        self.assertFalse(snap.entries_permitted)

    def test_every_halting_condition_blocks_entries(self):
        for condition in health_module.HALTING:
            with self.subTest(condition=condition):
                self.assertFalse(self._snap(condition).entries_permitted)

    def test_a_degraded_condition_in_the_exception_list_permits_entries(self):
        for condition in TOLERATED_FOR_ENTRIES:
            with self.subTest(condition=condition):
                snap = self._snap(condition)
                self.assertIs(snap.state, HealthState.DEGRADED)
                self.assertTrue(snap.entries_permitted)

    def test_every_tolerated_exception_has_a_written_reason(self):
        """Documented exceptions: an exception with no reason is just a
        hole."""
        for condition, reason in TOLERATED_FOR_ENTRIES.items():
            with self.subTest(condition=condition):
                self.assertGreater(len(reason), 80)

    def test_an_unlisted_degraded_condition_blocks_entries(self):
        """
        A condition added later fails CLOSED until someone decides it
        should not.
        """
        snap = self._snap(Condition.EVIDENCE_PROVIDER_DEGRADED)
        self.assertTrue(snap.entries_permitted)
        with mock.patch.dict(health_module.TOLERATED_FOR_ENTRIES, clear=True):
            self.assertIs(snap.state, HealthState.DEGRADED)
            self.assertFalse(snap.entries_permitted)

    def test_a_tolerated_condition_beside_a_halting_one_still_halts(self):
        snap = self._snap(Condition.LLM_UNAVAILABLE,
                          Condition.BROKER_UNAVAILABLE)
        self.assertFalse(snap.entries_permitted)

    def test_exits_are_always_permitted(self):
        """A sick agent must still be able to close what it holds."""
        for condition in Condition:
            with self.subTest(condition=condition):
                self.assertTrue(self._snap(condition).exits_permitted)

    def test_state_cannot_be_assigned(self):
        with self.assertRaises(AttributeError):
            self._snap().state = HealthState.HEALTHY

    def test_entries_permitted_cannot_be_assigned(self):
        with self.assertRaises(AttributeError):
            self._snap(Condition.EMERGENCY_STOP).entries_permitted = True


class TestHealthStore(unittest.TestCase):

    def test_a_raised_condition_appears_in_the_snapshot(self):
        store = InMemoryHealthStore()
        store.raise_condition(Condition.STALE_MARKET_DATA, "old")
        self.assertIs(store.snapshot().state, HealthState.HALTED)

    def test_repeats_increment_a_count(self):
        store = InMemoryHealthStore()
        for _ in range(3):
            store.raise_condition(Condition.MARKET_DATA_UNAVAILABLE)
        [active] = store.snapshot().active
        self.assertEqual(active.count, 3)

    def test_a_non_latching_condition_can_be_cleared_by_the_agent(self):
        store = InMemoryHealthStore()
        store.raise_condition(Condition.STALE_MARKET_DATA)
        self.assertTrue(store.clear_condition(Condition.STALE_MARKET_DATA))
        self.assertIs(store.snapshot().state, HealthState.HEALTHY)

    def test_the_agent_cannot_clear_a_latching_condition(self):
        """
        Otherwise a system could talk itself back into the situation
        that halted it.
        """
        for condition in health_module.LATCHING:
            with self.subTest(condition=condition):
                store = InMemoryHealthStore()
                store.raise_condition(condition)
                self.assertFalse(store.clear_condition(condition))
                self.assertIn(condition, [a.condition for a in
                                          store.snapshot().active])

    def test_a_named_human_can_clear_a_latching_condition(self):
        """The falsifying control: latching must be clearable by SOMEONE."""
        store = InMemoryHealthStore()
        store.raise_condition(Condition.EMERGENCY_STOP)
        self.assertTrue(store.clear_condition(
            Condition.EMERGENCY_STOP, cleared_by="operator"))
        self.assertIs(store.snapshot().state, HealthState.HEALTHY)

    def test_clearing_an_absent_condition_is_a_noop(self):
        self.assertFalse(
            InMemoryHealthStore().clear_condition(Condition.EMERGENCY_STOP))

    def test_the_failure_streak_resets_on_success(self):
        store = InMemoryHealthStore()
        self.assertEqual(store.record_cycle(False), 1)
        self.assertEqual(store.record_cycle(False), 2)
        self.assertEqual(store.record_cycle(True), 0)

    def test_an_unreadable_dynamo_health_store_raises(self):
        """An unreadable health record is not a clean bill of health."""
        class Broken:
            def get_item(self, **kw):
                raise RuntimeError("dynamo down")
        store = health_module.DynamoDBHealthStore(
            table_name="x", client=Broken())
        with self.assertRaises(RuntimeError):
            store.snapshot()


# ===========================================================================
# ALERTS
# ===========================================================================

class TestAlerts(unittest.TestCase):

    def test_critical_kinds_are_marked_critical(self):
        for kind in (AlertKind.EMERGENCY_STOP,
                     AlertKind.RECONCILIATION_MISMATCH,
                     AlertKind.UNEXPECTED_BROKER_POSITION,
                     AlertKind.DUPLICATE_ORDER_ATTEMPT,
                     AlertKind.EOD_FLATTEN_FAILURE):
            with self.subTest(kind=kind):
                self.assertIs(Alert(kind=kind, detail="x",
                                    session_date=SESSION).severity,
                              Severity.CRITICAL)

    def test_a_repeated_alert_is_suppressed_not_flooded(self):
        """An all-day outage is one alert that says it persists."""
        sink = InMemoryAlertSink()
        first = sink.emit(Alert(kind=AlertKind.REPEATED_PROVIDER_OUTAGE,
                                detail="x", session_date=SESSION))
        again = sink.emit(Alert(kind=AlertKind.REPEATED_PROVIDER_OUTAGE,
                                detail="x", session_date=SESSION))
        self.assertTrue(first)
        self.assertFalse(again)
        self.assertEqual(sink.transport_calls, 1)

    def test_occurrences_are_counted_even_when_suppressed(self):
        sink = InMemoryAlertSink()
        for _ in range(5):
            sink.emit(Alert(kind=AlertKind.EMERGENCY_STOP, detail="x",
                            session_date=SESSION))
        [alert] = sink.recent()
        self.assertEqual(alert.context["occurrences"], 5)

    def test_the_same_kind_on_a_new_day_is_a_new_alert(self):
        sink = InMemoryAlertSink()
        sink.emit(Alert(kind=AlertKind.EMERGENCY_STOP, detail="x",
                        session_date="2026-10-01"))
        self.assertTrue(sink.emit(Alert(
            kind=AlertKind.EMERGENCY_STOP, detail="x",
            session_date="2026-10-02")))

    def test_secrets_are_redacted_from_alert_context(self):
        alert = Alert(kind=AlertKind.EMERGENCY_STOP, detail="x",
                      session_date=SESSION,
                      context={"api_key": "SECRET123",
                               "nested": {"password": "hunter2",
                                          "symbol": "XYZ"},
                               "auth_header": "Bearer abc"})
        text = str(alert.as_dict())
        for secret in ("SECRET123", "hunter2", "Bearer abc"):
            self.assertNotIn(secret, text)
        self.assertIn("XYZ", text)

    def test_a_normal_paper_trade_raises_no_alert(self):
        """
        The value of the channel is its restraint. An alert stream that
        fires on routine activity trains its reader to ignore it.
        """
        rig = Rig()
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 1)
        rig.cycle(quote=stopping_quote, hypothesis=lambda s: None)
        self.assertEqual(rig.alerts.recent(), [])

    def test_a_zero_trade_cycle_raises_no_alert(self):
        rig = Rig()
        rig.cycle(hypothesis=lambda s: None)
        self.assertEqual(rig.alerts.recent(), [])


# ===========================================================================
# THE ORCHESTRATOR UNDER AUTONOMY
# ===========================================================================

class TestPaperTradingNeedsNoHuman(unittest.TestCase):

    def test_a_paper_order_is_placed_without_any_approval_step(self):
        rig = Rig()
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 1)
        self.assertEqual(result.execution_mode, "PAPER")
        self.assertEqual(rig.manager.open_count, 1)

    def test_disabled_mode_places_no_order(self):
        rig = Rig(mode=ExecutionMode.DISABLED)
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 0)
        self.assertEqual(rig.manager.open_count, 0)

    def test_the_risk_governor_remains_mandatory(self):
        """Autonomy removes the human, not the governor."""
        rig = Rig()
        result = rig.cycle(quote=lambda s: {"price": 100.0,
                                            "spread_pct": 5.0,
                                            "dollar_volume": 5e8,
                                            "age_seconds": 5.0,
                                            "source_age_seconds": 5.0})
        self.assertEqual(result.entries_submitted, 0)
        rows = rig.decisions.for_session(SESSION)
        self.assertEqual(rows[0]["outcome"], "REFUSED")
        self.assertIn("SPREAD_TOO_WIDE",
                      rows[0]["risk"]["reason_codes"])

    def test_the_agent_may_choose_to_take_zero_trades(self):
        """
        Available capital is a ceiling, not a target. A day of nothing is
        a valid day.
        """
        rig = Rig()
        result = rig.cycle(hypothesis=lambda s: None)
        self.assertEqual(result.entries_submitted, 0)
        self.assertNotEqual(result.outcome, CycleOutcome.ABORTED)

    def test_every_candidate_leaves_a_decision_row(self):
        """"Why did you not trade X" must be answerable from the record."""
        rig = Rig()
        rig.cycle(hypothesis=lambda s: None)
        [row] = rig.decisions.for_session(SESSION)
        self.assertEqual(row["outcome"], "NO_HYPOTHESIS")
        self.assertEqual(row["symbol"], "XYZ")

    def test_a_taken_trade_records_hypothesis_risk_and_order(self):
        rig = Rig()
        rig.cycle()
        [row] = rig.decisions.for_session(SESSION)
        self.assertEqual(row["outcome"], "ENTERED")
        self.assertTrue(row["hypothesis"]["hypothesis_id"])
        self.assertTrue(row["risk"]["approved"])
        self.assertEqual(row["order"]["status"], "FILLED")

    def test_decisions_carry_the_pinned_versions(self):
        rig = Rig()
        rig.cycle()
        [row] = rig.decisions.for_session(SESSION)
        self.assertEqual(row["versions"]["strategy"], "s1")
        self.assertEqual(row["versions"]["code_sha"], "abc")

    def test_trades_are_journalled_with_the_pinned_versions(self):
        """Every trade must be traceable to the behaviour that made it."""
        rig = Rig()
        rig.cycle()
        rig.cycle(quote=stopping_quote, hypothesis=lambda s: None)
        [trade] = rig.journal.list_trades()
        self.assertEqual(trade.config_versions["strategy"], "s1")
        self.assertEqual(trade.config_versions["risk"], "r1")
        self.assertEqual(trade.config_versions["code_sha"], "abc")

    def test_a_decision_log_failure_does_not_stop_trading(self):
        """A record that cannot be written must not become a reason to
        leave risk unmanaged."""
        class Broken:
            def record(self, row):
                raise RuntimeError("dynamo down")
        rig = Rig()
        rig.orchestrator.decisions = Broken()
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 1)
        self.assertTrue(any("decision not recorded" in e
                            for e in result.errors))


class TestHealthGatesEntriesNotExits(unittest.TestCase):

    def test_a_halting_condition_blocks_entries(self):
        rig = Rig()
        rig.health.raise_condition(Condition.BROKER_UNAVAILABLE, "down")
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 0)
        self.assertIn(HaltReason.HEALTH_NOT_PERMITTING, result.halt_reasons)

    def test_a_halting_condition_does_not_stop_exits(self):
        rig = Rig()
        rig.open_position()
        rig.health.raise_condition(Condition.JOURNAL_PERSISTENCE_FAILURE)
        result = rig.cycle(quote=stopping_quote)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(rig.manager.open_count, 0)

    def test_a_tolerated_degradation_still_permits_entries(self):
        rig = Rig()
        rig.health.raise_condition(Condition.EVIDENCE_PROVIDER_DEGRADED)
        self.assertEqual(rig.cycle().entries_submitted, 1)

    def test_an_unreadable_health_record_blocks_entries(self):
        class Broken(InMemoryHealthStore):
            def snapshot(self):
                raise RuntimeError("dynamo down")
        rig = Rig()
        rig.orchestrator.health = Broken()
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 0)
        self.assertIn(HaltReason.HEALTH_UNREADABLE, result.halt_reasons)

    def test_the_cycle_reports_its_health_state(self):
        rig = Rig()
        self.assertEqual(rig.cycle().health_state, "HEALTHY")


# ===========================================================================
# THE PHASE DERIVATION BUG
# ===========================================================================

class TestPhaseFromRealSessionObjects(unittest.TestCase):
    """
    The first cycle Lambda read minutes_to_close with getattr(..., None),
    but MarketSessionResult had no such attribute. Every value became
    None, and "open but unknown time to close" resolves to PRE_CLOSE, so
    the live cycle would have refused every entry and flattened every
    position on every run. Nothing raised. 18 integration tests missed it
    because they supply the numbers directly.
    """

    def _session(self, as_of, status="OPEN"):
        from datetime import time
        from agent.market.session import MarketSessionResult, TradingDay
        from agent.state.models import MarketSession
        day = TradingDay(date="2026-10-01", regular_open=time(9, 30),
                         regular_close=time(16, 0))
        return MarketSessionResult(
            session=MarketSession[status], as_of=as_of,
            is_trading_day=True, trading_day=day)

    def test_a_mid_session_result_resolves_to_intraday(self):
        """The case that was silently wrong."""
        phase = phase_from_session(self._session("2026-10-01T13:00:00-04:00"))
        self.assertIs(phase, CyclePhase.INTRADAY)
        self.assertTrue(phase.permits_new_exposure)

    def test_the_first_minutes_resolve_to_opening(self):
        phase = phase_from_session(self._session("2026-10-01T09:33:00-04:00"))
        self.assertIs(phase, CyclePhase.OPENING)

    def test_the_last_half_hour_resolves_to_pre_close(self):
        phase = phase_from_session(self._session("2026-10-01T15:40:00-04:00"))
        self.assertIs(phase, CyclePhase.PRE_CLOSE)

    def test_utc_timestamps_are_converted_to_market_time(self):
        phase = phase_from_session(self._session("2026-10-01T17:00:00+00:00"))
        self.assertIs(phase, CyclePhase.INTRADAY)

    def test_a_closed_market_is_closed(self):
        phase = phase_from_session(
            self._session("2026-10-01T20:30:00-04:00", status="CLOSED"))
        self.assertIs(phase, CyclePhase.CLOSED)

    def test_a_missing_timestamp_forbids_entries_rather_than_guessing(self):
        phase = phase_from_session(self._session(""))
        self.assertFalse(phase.permits_new_exposure)

    def test_a_result_lacking_the_attributes_raises_not_defaults(self):
        """
        The original failure was a SILENT default. A session object that
        does not provide these must blow up loudly.
        """
        class Bare:
            session = "OPEN"
        with self.assertRaises(AttributeError):
            phase_from_session(Bare())

    def test_the_real_result_exposes_the_attributes(self):
        session = self._session("2026-10-01T13:00:00-04:00")
        self.assertIsNotNone(session.minutes_to_close)
        self.assertIsNotNone(session.minutes_since_open)

    def test_the_minutes_are_numerically_right(self):
        session = self._session("2026-10-01T15:35:00-04:00")
        self.assertAlmostEqual(session.minutes_to_close, 25.0, places=3)
        self.assertAlmostEqual(session.minutes_since_open, 365.0, places=3)

    def test_after_the_close_the_minutes_are_negative_not_none(self):
        session = self._session("2026-10-01T17:00:00-04:00", status="CLOSED")
        self.assertLess(session.minutes_to_close, 0)


# ===========================================================================
# THE DAILY RISK LOCK (previously never invoked)
# ===========================================================================

class TestDailyRiskLock(unittest.TestCase):
    """
    should_engage_risk_lock existed since Milestone 8 and nothing called
    it. A bad day would never have locked and never flattened.
    """

    def test_a_realised_loss_alone_locks_the_day_with_nothing_open(self):
        rig = Rig()
        result = rig.cycle(realized_pnl_today=-6.0)
        self.assertIn(HaltReason.DAILY_LOSS_LIMIT, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 0)

    def test_the_lock_raises_an_alert(self):
        rig = Rig()
        rig.cycle(realized_pnl_today=-6.0)
        kinds = [a.kind for a in rig.alerts.recent()]
        self.assertIn(AlertKind.DAILY_RISK_LOCK, kinds)

    def test_an_unrealised_loss_beyond_the_limit_flattens(self):
        """
        Waiting for a loss to be realised before locking would enforce
        the limit only after the damage is taken.
        """
        rig = Rig()
        rig.open_position(stop=1.0)           # a stop that will not fire
        result = rig.cycle(quote=disastrous_quote)
        self.assertIn(HaltReason.DAILY_LOSS_LIMIT, result.halt_reasons)
        self.assertEqual(rig.manager.open_count, 0)
        [trade] = rig.journal.list_trades()
        self.assertIn("DAILY_LOSS_LIMIT", trade.all_exit_reasons)

    def test_a_loss_inside_the_limit_does_not_lock(self):
        """The falsifying control: the lock must not always fire."""
        rig = Rig()
        result = rig.cycle(realized_pnl_today=-1.0)
        self.assertNotIn(HaltReason.DAILY_LOSS_LIMIT, result.halt_reasons)
        self.assertEqual(result.entries_submitted, 1)

    def test_the_lock_is_recomputed_so_a_new_session_starts_unlocked(self):
        rig = Rig()
        rig.cycle(realized_pnl_today=-6.0)
        result = rig.cycle(realized_pnl_today=0.0)
        self.assertNotIn(HaltReason.DAILY_LOSS_LIMIT, result.halt_reasons)

    def test_the_lock_exits_even_though_entries_are_blocked(self):
        rig = Rig()
        rig.open_position()
        result = rig.cycle(quote=stopping_quote, realized_pnl_today=-6.0)
        self.assertGreater(result.exits_submitted, 0)


class TestDailyCountersSurviveClosingAPosition(unittest.TestCase):
    """
    capital_deployed used to be the cost basis of whatever was open NOW,
    and positions_opened_today defaulted to zero, so both reset the
    moment a position closed. The daily ceiling and the daily cap could
    be bypassed by closing one position and opening another.
    """

    def _closed_round_trip(self, rig):
        rig.cycle()
        rig.cycle(quote=stopping_quote, hypothesis=lambda s: None)

    def test_capital_deployed_includes_positions_already_closed(self):
        rig = Rig()
        self._closed_round_trip(rig)
        self.assertEqual(rig.manager.open_count, 0)
        counters = daily_counters(rig.journal, rig.manager, SESSION)
        self.assertGreater(counters["capital_deployed_today"], 0.0)

    def test_positions_opened_today_includes_closed_ones(self):
        rig = Rig()
        self._closed_round_trip(rig)
        counters = daily_counters(rig.journal, rig.manager, SESSION)
        self.assertEqual(counters["positions_opened_today"], 1)

    def test_realised_pnl_is_today_only_not_cumulative(self):
        """
        The broker account's realised P&L is cumulative since inception.
        Using it would measure the daily loss limit against all-time
        results.
        """
        rig = Rig()
        self._closed_round_trip(rig)
        today = daily_counters(rig.journal, rig.manager, SESSION)
        other = daily_counters(rig.journal, rig.manager, "2026-10-02")
        self.assertLess(today["realized_pnl_today"], 0.0)
        self.assertEqual(other["realized_pnl_today"], 0.0)

    def test_open_positions_count_toward_capital_and_openings(self):
        rig = Rig()
        rig.cycle()
        counters = daily_counters(rig.journal, rig.manager, SESSION)
        self.assertEqual(counters["positions_opened_today"], 1)
        self.assertGreater(counters["capital_deployed_today"], 0.0)

    def test_the_daily_new_position_cap_holds_across_cycles(self):
        """
        The bypass, as a behaviour: close a position and open another,
        repeatedly, until the cap.

        Uses DISTINCT symbols. An earlier version round-tripped one
        symbol, and once the same-session cooldown existed it stopped
        after one trade and passed WITHOUT EVER REACHING the cap it
        claimed to test.
        """
        # A generous capital limit, so the position-COUNT cap is the one
        # that binds. With the default $50 allowance the capital ceiling
        # binds first (two positions), which is also correct but means a
        # count-cap test never reaches the count cap.
        limits = RiskLimits(daily_capital_limit=5000.0,
                            max_daily_capital=10000.0,
                            max_concurrent_positions=1,
                            daily_loss_limit=1000.0)
        symbols = [f"S{i}" for i in range(limits.max_new_positions_per_day + 3)]
        rig = Rig(cash=50000.0, limits=limits)
        for symbol in symbols:
            rig.broker.set_quote(Quote(symbol=symbol, bid=99.9, ask=100.1,
                                       last=100.0))
        for symbol in symbols:
            counters = daily_counters(rig.journal, rig.manager, SESSION)
            rig.cycle(
                candidates=(symbol,),
                capital_deployed=counters["capital_deployed_today"],
                realized_pnl_today=0.0,
                positions_opened_today=counters["positions_opened_today"])
            rig.cycle(quote=stopping_quote, hypothesis=lambda s: None,
                      realized_pnl_today=0.0)
        counters = daily_counters(rig.journal, rig.manager, SESSION)
        # EXACTLY the cap: the extra symbols were offered and refused.
        self.assertEqual(counters["positions_opened_today"],
                         limits.max_new_positions_per_day)

    def test_a_symbol_exited_this_session_is_not_re_entered(self):
        """
        Buying straight back into a stop-out is churn. Found when a
        handler test saw the agent stop out of XYZ at 96 and re-enter at
        96 in the same cycle.
        """
        rig = Rig()
        rig.open_position()
        result = rig.cycle(quote=stopping_quote)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(result.entries_submitted, 0)
        outcomes = [r["outcome"] for r in rig.decisions.for_session(SESSION)]
        self.assertIn("COOLDOWN", outcomes)

    def test_the_cooldown_persists_across_cycles(self):
        rig = Rig()
        rig.open_position()
        rig.cycle(quote=stopping_quote, hypothesis=lambda s: None)
        again = rig.cycle()
        self.assertEqual(again.entries_submitted, 0)

    def test_the_cooldown_does_not_block_a_different_symbol(self):
        """The falsifying control: it must not freeze everything."""
        rig = Rig()
        rig.broker.set_quote(Quote(symbol="AAA", bid=99.9, ask=100.1,
                                   last=100.0))
        rig.open_position()
        rig.cycle(quote=stopping_quote, hypothesis=lambda s: None)
        self.assertEqual(rig.cycle(candidates=("AAA",)).entries_submitted, 1)

    def test_the_cooldown_applies_to_the_day_only(self):
        """A new session may trade the name again."""
        rig = Rig()
        rig.open_position()
        rig.cycle(quote=stopping_quote, hypothesis=lambda s: None)
        result = rig.orchestrator.run_cycle(
            session_date="2026-10-02", phase=CyclePhase.INTRADAY,
            candidates=["XYZ"], quote_for=quote_for,
            hypothesis_for=hypothesis_for, minutes_to_close=200)
        self.assertEqual(result.entries_submitted, 1)

    def test_an_unreadable_journal_blocks_entries_rather_than_assuming_none(self):
        """Unknown is treated as cooling down."""
        class Broken:
            def list_trades(self, **kw):
                raise RuntimeError("dynamo read failed")

            def record(self, t):
                pass
        rig = Rig()
        rig.orchestrator.journal = Broken()
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 0)
        self.assertTrue(any("cooldown unknown" in e for e in result.errors))

    def test_an_empty_day_has_zero_counters(self):
        rig = Rig()
        counters = daily_counters(rig.journal, rig.manager, SESSION)
        self.assertEqual(counters["capital_deployed_today"], 0.0)
        self.assertEqual(counters["positions_opened_today"], 0)


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
# PENDING ORDERS ARE EXPOSURE
# ===========================================================================

class TestAnAcceptedOrderIsExposureBeforeItFills(unittest.TestCase):
    """The arithmetic that keeps the ceiling real.

    An order the venue has accepted but not filled may fill a moment
    from now. A second entry sized as though it were not live puts the
    account past the limit its own configuration describes - and nothing
    in the daily counters would show it, because the counters are built
    from JOURNALLED trades and an unfilled order has not traded yet.
    """

    def _pending(self, notional, cli="cli_pending"):
        return ExternalOrderRecord(
            client_order_id=cli, session_date=SESSION, symbol="PEND",
            side="BUY", requested_quantity=1.0,
            requested_notional=notional, intent="ENTRY",
            intent_at="2026-09-30T13:00:00+00:00")

    def test_a_pending_order_consumes_the_daily_capital_ceiling(self):
        rig = Rig()
        # The whole ceiling is already reserved by an accepted order.
        rig.ledger.record_intent(self._pending(RiskLimits().daily_capital_limit))
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 0)
        self.assertGreater(result.committed_exposure, 0.0)
        self.assertTrue(result.committed_exposure_known)
        # The reason is a BUDGET one, not a specific code. Reserving
        # the whole ceiling leaves less room than min_position_value, so
        # the governor refuses for INSUFFICIENT_CAPITAL before it ever
        # reaches DAILY_CAPITAL_EXCEEDED. Asserting one exact code here
        # would be asserting the order of the governor's checks rather
        # than the property under test, which is that the pending order
        # consumed the room.
        from agent.autonomy.inactivity import CATEGORY
        codes = {c for row in rig.decisions.for_session(SESSION)
                 for c in ((row.get("risk") or {}).get("reason_codes") or [])}
        self.assertTrue(codes, "no reason codes were recorded")
        self.assertTrue(
            any(CATEGORY.get(c) == "BUDGET" for c in codes),
            f"expected a budget refusal, got {sorted(codes)}")

    def test_without_the_pending_order_the_same_cycle_does_trade(self):
        """The control. Without this, the test above would pass for a
        cycle that was never going to trade anyway."""
        rig = Rig()
        self.assertEqual(rig.cycle().entries_submitted, 1)

    def test_a_terminal_order_reserves_nothing(self):
        rig = Rig()
        row = self._pending(RiskLimits().daily_capital_limit)
        rig.ledger.record_intent(row)
        rig.ledger.record_observation(row.client_order_id, {
            "order_id": "brk_done", "status": "CANCELLED",
            "raw_status": "canceled", "filled_quantity": 0.0,
            "remaining_quantity": 0.0}, "2026-09-30T13:05:00+00:00")
        self.assertEqual(rig.cycle().entries_submitted, 1)

    def test_unknown_exposure_opens_nothing_at_all(self):
        """UNKNOWN is not zero. Zero is the one value that would let
        every entry through."""
        rig = Rig()

        class Unreadable(InMemoryOrderLedger):
            def for_session(self, session_date):
                raise OrderLedgerError("ledger unreadable")

        rig.orchestrator.order_ledger = Unreadable()
        result = rig.cycle()
        self.assertEqual(result.entries_submitted, 0)
        self.assertFalse(result.committed_exposure_known)
        self.assertIsNone(result.committed_exposure,
                          "unknown exposure must not arrive as 0.0")
        step = next(s for s in result.steps if s.name == "consider_entries")
        self.assertTrue(step.skipped)
        self.assertIn("could not be established", step.skip_reason)

    def test_an_unsizeable_pending_order_also_blocks_entries(self):
        rig = Rig()
        row = self._pending(None, cli="cli_unsizeable")
        row.requested_quantity = None
        rig.ledger.record_intent(row)
        result = rig.cycle()
        self.assertFalse(result.committed_exposure_known)
        self.assertEqual(result.entries_submitted, 0)

    def test_the_poll_step_reports_its_integrity(self):
        rig = Rig()
        result = rig.cycle()
        step = next(s for s in result.steps
                    if s.name == "poll_external_orders")
        self.assertIn("integrity=COMPLETE", step.detail)
        self.assertIn("exposure_known=True", step.detail)
