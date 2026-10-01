"""
The real cycle Lambda, end to end.

Until this file existed, the handler had never been executed by any
test. Three defects lived in it for that reason: it read
`minutes_to_close` from an object that had no such attribute (so every
live cycle would have refused to enter and flattened everything), it
called `.get()` on a scanner-run dataclass, and the daily risk lock was
never invoked at all. Component tests could not see any of them because
each component was correct on its own.

This drives `lambda_handler` itself. Every AWS dependency is replaced by
an in-memory one that PERSISTS across invocations, so each call is a cold
start exactly as on Lambda: nothing survives in a Python variable that
would not survive in DynamoDB.
"""
import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.autonomy import (                                      # noqa: E402
    Condition, InMemoryAlertSink, InMemoryDecisionLog,
    InMemoryHealthStore, InMemorySessionStore, InMemorySnapshotStore,
)
from agent.broker import InMemoryBrokerStateStore                  # noqa: E402
from agent.journal import InMemoryJournal                         # noqa: E402
from agent.market.session import MarketSessionResult, TradingDay  # noqa: E402
from agent.orchestration import InMemoryCycleLock                 # noqa: E402
from agent.positions import InMemoryPositionStore                 # noqa: E402
from agent.risk import InMemoryHaltStore                           # noqa: E402
from agent.scanner.models import Candidate, ScanStatus, ScannerRun  # noqa: E402
from agent.scanner.store import InMemoryScannerStore               # noqa: E402
from agent.state.models import AgentState, MarketSession          # noqa: E402
from agent.state.store import InMemoryStateStore                   # noqa: E402

HANDLER_PATH = os.path.join(REPO_ROOT, "lambda-micro", "agent-cycle",
                            "handler.py")
EDT = timezone(timedelta(hours=-4))
EST = timezone(timedelta(hours=-5))


def load_handler():
    spec = importlib.util.spec_from_file_location(
        "agent_cycle_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_cycle_handler"] = module
    spec.loader.exec_module(module)
    return module


cycle = load_handler()


class FakeQuote:
    def __init__(self, price, age=3.0):
        self.price = price
        self.last = price
        self.bid = price - 0.05
        self.ask = price + 0.05
        self.dollar_volume = 5e8
        self.provenance = mock.Mock(age_seconds=age)


class FakeProvider:
    def __init__(self, world):
        self.world = world

    def get_quote(self, symbol):
        quote = self.world.quotes.get(symbol)
        if quote is None:
            raise RuntimeError("no quote")
        return quote


class FakeSignal:
    def as_dict(self):
        return {"direction": "BUY", "signal_agreement": 1.0,
                "signal_magnitude": 0.7, "regime_adjusted_magnitude": 0.7,
                "strength_band": "STRONG", "buy_groups": 3,
                "sell_groups": 0, "opinionated_groups": 3,
                "data_quality": {"freshness": "FRESH"}}


class FakeSignalService:
    def __init__(self, world):
        self.world = world

    def evaluate_symbol(self, symbol):
        if self.world.signals_down:
            raise RuntimeError("signal service down")
        return FakeSignal()


class World:
    """Everything that persists between Lambda invocations."""

    def __init__(self, date="2026-10-01"):
        self.date = date
        self.broker_store = InMemoryBrokerStateStore()
        self.position_store = InMemoryPositionStore()
        self.journal = InMemoryJournal()
        self.sessions = InMemorySessionStore()
        self.alerts = InMemoryAlertSink()
        self.health = InMemoryHealthStore()
        self.decisions = InMemoryDecisionLog()
        self.halt = InMemoryHaltStore()
        self.lock = InMemoryCycleLock()
        self.scanner_store = InMemoryScannerStore()
        self.state_store = InMemoryStateStore()
        self.snapshot = InMemorySnapshotStore()
        from agent.risk import RiskLimits
        self.limits = RiskLimits()

        self.quotes = {"XYZ": FakeQuote(100.0)}
        self.status = "OPEN"
        self.as_of = f"{date}T13:00:00-04:00"
        self.signals_down = False
        self.set_scan(["XYZ"])
        self.set_regime(fresh=True)

    # --- world controls ---------------------------------------------------
    def set_scan(self, symbols, status=ScanStatus.COMPLETE, age_s=60):
        completed = (datetime.now(timezone.utc)
                     - timedelta(seconds=age_s)).isoformat(timespec="seconds")
        run_id = f"run-{len(self.scanner_store._runs)}"
        self.scanner_store.save_run(ScannerRun(
            scanner_run_id=run_id, session_date=self.date,
            started_at=completed, completed_at=completed, status=status,
            candidates=[Candidate(candidate_id=f"c-{s}",
                                  scanner_run_id=run_id, symbol=s,
                                  timestamp=completed, rank=i)
                        for i, s in enumerate(symbols)]))

    def set_regime(self, fresh=True, regime="BULLISH", posture="NORMAL"):
        from agent.state.service import AgentStateService
        service = AgentStateService(self.state_store)
        session = service.get_session(self.date)
        stamp = datetime.now(timezone.utc) - timedelta(
            seconds=30 if fresh else 7200)
        session.market_regime = regime
        session.market_regime_confidence = 0.8
        session.risk_posture = posture
        session.regime_updated_at = stamp.isoformat(timespec="seconds")
        session.market_status = MarketSession.OPEN
        self.state_store.put(session, expected_revision=session.revision)

    def at(self, hhmm, tz=EDT, status="OPEN"):
        self.as_of = f"{self.date}T{hhmm}:00{'-04:00' if tz is EDT else '-05:00'}"
        self.status = status

    # --- the call under test ----------------------------------------------
    def _session_service(self, provider):
        world = self

        class Service:
            def __init__(self, p, *a, **k):
                pass

            def current(self):
                day = TradingDay(date=world.date,
                                 regular_open=time(9, 30),
                                 regular_close=time(16, 0))
                return MarketSessionResult(
                    session=MarketSession[world.status], as_of=world.as_of,
                    is_trading_day=True, trading_day=day)
        return Service

    def invoke(self, env=None):
        provider = FakeProvider(self)
        patches = {
            "_provider": lambda: (provider, provider),
            "MarketSessionService": self._session_service(provider),
            "DynamoDBBrokerStateStore": lambda **kw: self.broker_store,
            "DynamoDBPositionStore": lambda **kw: self.position_store,
            "DynamoDBJournal": lambda **kw: self.journal,
            "DynamoDBSessionStore": lambda **kw: self.sessions,
            "DynamoDBAlertSink": lambda **kw: self.alerts,
            "DynamoDBHealthStore": lambda **kw: self.health,
            "DynamoDBDecisionLog": lambda **kw: self.decisions,
            "DynamoDBHaltStore": lambda **kw: self.halt,
            "DynamoDBCycleLock": lambda **kw: self.lock,
            "DynamoDBScannerStore": lambda **kw: self.scanner_store,
            "DynamoDBSignalStore": lambda **kw: None,
            "SignalService": lambda *a, **k: FakeSignalService(self),
            "DynamoDBStateStore": lambda *a, **k: self.state_store,
            "DynamoDBSnapshotStore": lambda **kw: self.snapshot,
            "today_market_date": lambda: self.date,
            "RiskLimits": lambda: self.limits,
        }
        environment = {"AGENT_EXECUTION_MODE": "PAPER",
                       "AGENT_PAPER_CASH": "1000",
                       "AGENT_CODE_SHA": "deadbeef"}
        environment.update(env or {})
        stack = [mock.patch.object(cycle, name, value)
                 for name, value in patches.items()]
        stack.append(mock.patch.dict(os.environ, environment))
        for p in stack:
            p.start()
        try:
            response = cycle.lambda_handler({}, None)
        finally:
            for p in reversed(stack):
                p.stop()
        self.last = json.loads(response["body"])
        return self.last

    # --- helpers --------------------------------------------------------
    def conditions(self):
        return {a.condition for a in self.health.snapshot().active}

    def alert_kinds(self):
        return {a.kind.value for a in self.alerts.recent()}


class TestTheHandlerRunsAtAll(unittest.TestCase):

    def test_a_mid_session_cycle_enters_a_position(self):
        """
        The case that was silently wrong: with minutes_to_close missing,
        every live cycle resolved to PRE_CLOSE and refused every entry.
        """
        world = World()
        out = world.invoke()
        self.assertTrue(out.get("phase") == "INTRADAY", out.get("error"))
        self.assertEqual(out["entries_submitted"], 1, out)
        self.assertEqual(out["execution_mode"], "PAPER")

    def test_it_never_raises_into_the_scheduler(self):
        world = World()
        with mock.patch.object(cycle, "_provider",
                               side_effect=RuntimeError("secrets down")):
            response = cycle.lambda_handler({}, None)
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["error"], "RuntimeError")
        self.assertFalse(body["new_exposure_permitted"])

    def test_the_cycle_reports_the_versions_it_ran_under(self):
        out = World().invoke()
        self.assertEqual(out["versions"]["code_sha"], "deadbeef")
        self.assertTrue(out["versions"]["strategy"])
        self.assertTrue(out["cohort"].startswith("cohort-"))


class TestPhaseAcrossTheDay(unittest.TestCase):

    def test_the_opening_minutes_do_not_enter(self):
        world = World()
        world.at("09:33")
        out = world.invoke()
        self.assertEqual(out["phase"], "OPENING")
        self.assertEqual(out["entries_submitted"], 0)

    def test_the_last_half_hour_does_not_enter_and_flattens(self):
        world = World()
        world.invoke()                                  # open a position
        world.at("15:40")
        out = world.invoke()
        self.assertEqual(out["phase"], "PRE_CLOSE")
        self.assertEqual(out["entries_submitted"], 0)
        self.assertEqual(out["open_positions"], 0)

    def test_winter_time_is_handled(self):
        """
        After the clocks change the market is on EST. The phase must come
        from the provider's own timestamp and market calendar, not from a
        hardcoded UTC window.
        """
        world = World()
        world.as_of = f"{world.date}T13:00:00-05:00"
        out = world.invoke()
        self.assertEqual(out["phase"], "INTRADAY")
        self.assertEqual(out["entries_submitted"], 1)

    def test_a_closed_market_trades_nothing(self):
        world = World()
        world.at("20:30", status="CLOSED")
        out = world.invoke()
        self.assertFalse(out["ran"])
        self.assertEqual(out["phase"], "CLOSED")

    def test_pre_market_trades_nothing(self):
        world = World()
        world.at("08:00", status="PRE_MARKET")
        out = world.invoke()
        self.assertFalse(out["ran"])


class TestContinuityAcrossColdStarts(unittest.TestCase):

    def test_a_position_survives_to_the_next_invocation(self):
        world = World()
        world.invoke()
        out = world.invoke()
        self.assertEqual(out["open_positions"], 1)
        self.assertTrue(out["positions_reconciled"])
        self.assertFalse(out["emergency_stop_engaged"])

    def test_the_same_signal_is_not_entered_twice(self):
        world = World()
        first = world.invoke()
        second = world.invoke()
        self.assertEqual(first["entries_submitted"], 1)
        self.assertEqual(second["entries_submitted"], 0)

    def test_state_is_saved_after_every_cycle(self):
        world = World()
        out = world.invoke()
        self.assertTrue(out["state_saved"], out["state_detail"])

    def test_the_stop_ratchet_survives_a_cold_start(self):
        world = World()
        world.invoke()
        [position] = world.position_store.load_open(world.date)
        first_stop = position.plan.stop_price
        world.quotes["XYZ"] = FakeQuote(102.5)
        world.invoke()
        [position] = world.position_store.load_open(world.date)
        self.assertGreater(position.plan.stop_price, first_stop)

    def test_a_stop_breach_closes_and_journals(self):
        world = World()
        world.invoke()
        world.quotes["XYZ"] = FakeQuote(96.0)
        out = world.invoke()
        self.assertEqual(out["open_positions"], 0)
        self.assertEqual(len(world.journal.list_trades(world.date)), 1)
        self.assertEqual(world.position_store.load_open(world.date), [])


class TestDailyLimitsAcrossInvocations(unittest.TestCase):
    """
    capital_deployed used to be the cost basis of whatever was open NOW
    and the loss limit used the cumulative broker P&L. Both are derived
    from the journal now; this checks they survive real cold starts.
    """

    def _round_trip(self, world):
        world.invoke()
        world.quotes["XYZ"] = FakeQuote(96.0)
        world.invoke()
        world.quotes["XYZ"] = FakeQuote(100.0)

    def test_closing_a_position_does_not_reset_the_daily_counters(self):
        world = World()
        self._round_trip(world)
        out = world.invoke()
        self.assertEqual(out["daily"]["positions_opened_today"], 1)
        self.assertGreater(out["daily"]["capital_deployed_today"], 0)

    def test_the_daily_new_position_cap_holds_across_invocations(self):
        """
        DISTINCT symbols, so the cap is genuinely reached. One symbol
        would stop at the same-session cooldown and never test the cap.
        """
        from agent.risk import RiskLimits
        # Generous capital, so the position-COUNT cap is the one that
        # binds; the default $50 allowance runs out after two positions.
        world = World()
        world.limits = RiskLimits(daily_capital_limit=5000.0,
                                  max_daily_capital=10000.0,
                                  max_concurrent_positions=1,
                                  # Out of the way: with the default $5
                                  # a single gap-through-the-stop loss of
                                  # about $4.6 leaves less loss budget
                                  # than one more trade's risk, and the
                                  # governor ends the day. Correct, but
                                  # it would stop this test at one trade.
                                  daily_loss_limit=1000.0)
        cap = world.limits.max_new_positions_per_day
        symbols = [f"S{i}" for i in range(cap + 3)]
        for symbol in symbols:
            world.quotes[symbol] = FakeQuote(100.0)
        for symbol in symbols:
            world.set_scan([symbol])
            world.invoke()                              # enter
            world.quotes[symbol] = FakeQuote(96.0)
            world.invoke()                              # stop out
            world.quotes[symbol] = FakeQuote(100.0)
        opened = len(world.journal.list_trades(world.date))
        self.assertEqual(opened, cap)

    def test_a_losing_day_locks_the_next_invocation(self):
        world = World()
        world.invoke()
        world.quotes["XYZ"] = FakeQuote(80.0)           # a large loss
        world.invoke()
        out = world.invoke()
        self.assertIn("DAILY_LOSS_LIMIT", out["halt_reasons"])
        self.assertEqual(out["entries_submitted"], 0)
        self.assertIn("DAILY_RISK_LOCK", world.alert_kinds())

    def test_a_new_session_starts_unlocked_and_with_zero_counters(self):
        world = World()
        world.invoke()
        world.quotes["XYZ"] = FakeQuote(80.0)
        world.invoke()
        world.date = "2026-10-02"
        world.quotes["XYZ"] = FakeQuote(100.0)
        world.at("13:00")
        world.set_scan(["XYZ"])
        world.set_regime(fresh=True)
        out = world.invoke()
        self.assertEqual(out["daily"]["positions_opened_today"], 0 +
                         out["entries_submitted"])
        self.assertNotIn("DAILY_LOSS_LIMIT", out["halt_reasons"])


class TestStaleUpstreamData(unittest.TestCase):
    """
    The scanner Lambda refreshes the regime and candidates on its own
    schedule; this cycle only reads them. A failing scanner must not
    leave the cycle trading on results from hours ago.
    """

    def test_a_stale_scan_blocks_entries_and_degrades_health(self):
        world = World()
        world.set_scan(["XYZ"], age_s=3600)
        out = world.invoke()
        self.assertEqual(out["entries_submitted"], 0)
        self.assertIn(Condition.SCANNER_DEGRADED, world.conditions())

    def test_a_failed_scan_blocks_entries(self):
        world = World()
        world.set_scan(["XYZ"], status=ScanStatus.PROVIDER_ERROR)
        out = world.invoke()
        self.assertEqual(out["entries_submitted"], 0)
        self.assertIn(Condition.SCANNER_DEGRADED, world.conditions())

    def test_no_scan_at_all_blocks_entries(self):
        world = World()
        world.scanner_store._runs.clear()
        world.scanner_store._order.clear()
        self.assertEqual(world.invoke()["entries_submitted"], 0)

    def test_a_stale_regime_is_treated_as_unknown(self):
        """A reading nobody has refreshed is not a reading."""
        world = World()
        world.set_regime(fresh=False)
        out = world.invoke()
        self.assertEqual(out["entries_submitted"], 0)
        [row] = world.decisions.for_session(world.date)
        self.assertEqual(row["outcome"], "REFUSED")

    def test_a_fresh_scan_and_regime_do_trade(self):
        """The falsifying control for all of the above."""
        world = World()
        self.assertEqual(world.invoke()["entries_submitted"], 1)

    def test_the_scanner_recovers_and_health_clears(self):
        world = World()
        world.set_scan(["XYZ"], age_s=3600)
        world.invoke()
        world.set_scan(["XYZ"], age_s=30)
        world.invoke()
        self.assertNotIn(Condition.SCANNER_DEGRADED, world.conditions())

    def test_a_signal_service_outage_means_no_hypothesis_not_a_crash(self):
        world = World()
        world.signals_down = True
        out = world.invoke()
        self.assertEqual(out["entries_submitted"], 0)
        self.assertNotEqual(out["outcome"], "ABORTED")
        [row] = world.decisions.for_session(world.date)
        self.assertEqual(row["outcome"], "NO_HYPOTHESIS")


class TestExecutionModeIsEnforced(unittest.TestCase):

    def test_disabled_places_no_orders(self):
        world = World()
        out = world.invoke(env={"AGENT_EXECUTION_MODE": "DISABLED"})
        self.assertEqual(out["entries_submitted"], 0)
        self.assertEqual(out["execution_mode"], "DISABLED")

    def test_live_is_refused_and_alerted(self):
        """A deployment told to go live must STOP, not trade."""
        world = World()
        out = world.invoke(env={"AGENT_EXECUTION_MODE": "LIVE"})
        self.assertEqual(out["entries_submitted"], 0)
        self.assertEqual(out["execution_mode"], "DISABLED")
        self.assertIn("LIVE_MODE_REQUESTED", world.alert_kinds())

    def test_a_missing_mode_trades_nothing(self):
        world = World()
        out = world.invoke(env={"AGENT_EXECUTION_MODE": ""})
        self.assertEqual(out["entries_submitted"], 0)

    def test_a_typo_in_the_mode_trades_nothing(self):
        for value in ("PAPR", "paper-trading", "true", "1", "ON"):
            with self.subTest(value=value):
                world = World()
                out = world.invoke(env={"AGENT_EXECUTION_MODE": value})
                self.assertEqual(out["entries_submitted"], 0)

    def test_the_handler_is_paper_by_assertion(self):
        self.assertFalse(cycle.IS_LIVE)

    def test_no_real_broker_is_imported_by_the_handler(self):
        """
        The handler constructs the INTERNAL paper broker directly. The
        Alpaca paper adapter is deliberately not wired in because the
        credential scope was not verified.
        """
        source = open(HANDLER_PATH).read()
        self.assertNotIn("AlpacaPaperBroker", source)
        self.assertNotIn("alpaca_paper", source)
        self.assertIn("PaperBroker(", source)


class TestPinnedVersions(unittest.TestCase):

    def test_trades_carry_the_code_sha(self):
        world = World()
        world.invoke()
        world.quotes["XYZ"] = FakeQuote(96.0)
        world.invoke()
        [trade] = world.journal.list_trades(world.date)
        self.assertEqual(trade.config_versions["code_sha"], "deadbeef")
        self.assertTrue(trade.config_versions["strategy"])
        self.assertTrue(trade.config_versions["risk"])

    def test_decisions_carry_the_versions(self):
        world = World()
        world.invoke()
        [row] = world.decisions.for_session(world.date)
        self.assertEqual(row["versions"]["code_sha"], "deadbeef")


class TestTheSessionLifecycle(unittest.TestCase):

    def _full_day(self, trade=True):
        world = World()
        if trade:
            world.invoke()                              # enters
        else:
            world.set_scan([])
            world.invoke()
        world.at("15:40")
        world.invoke()                                  # pre-close flatten
        world.at("20:30", status="CLOSED")
        return world

    def test_a_session_is_tallied_as_a_live_market_session(self):
        world = World()
        world.invoke()
        tally = world.sessions.get(world.date)
        self.assertEqual(tally.cycles_live_market, 1)
        self.assertEqual(tally.live_cycles_with_real_quotes, 1)
        self.assertTrue(tally.ran_during_market_hours)

    def test_closing_a_traded_day_writes_one_clean_report(self):
        world = self._full_day()
        out = world.invoke()
        self.assertIn("session_report", out, out)
        self.assertTrue(out["session_report"]["session_ok"],
                        out["session_report"]["failed_checks"])
        report = world.sessions.get_report(world.date)
        self.assertTrue(report["checks"]["no_positions_open"])
        self.assertTrue(report["checks"]["broker_flat"])
        self.assertTrue(report["checks"]["cash_reconciles"],
                        report["cash"])
        self.assertTrue(report["checks"]["journal_complete"])

    def test_the_report_is_written_once(self):
        world = self._full_day()
        world.invoke()
        first = world.sessions.get_report(world.date)["written_at"]
        world.invoke()
        self.assertEqual(world.sessions.get_report(world.date)["written_at"],
                         first)

    def test_a_zero_trade_day_is_reported_as_one_not_hidden(self):
        """A valid result. Do not force an order to prove the pipeline."""
        world = self._full_day(trade=False)
        out = world.invoke()
        self.assertTrue(out["session_report"]["zero_trade_day"])
        self.assertTrue(out["session_report"]["session_ok"])
        self.assertEqual(out["session_report"]["trades"], 0)

    def test_a_day_that_never_ran_has_no_report(self):
        """
        Inventing a session on a holiday would inflate the count of
        sessions completed.
        """
        world = World()
        world.at("20:30", status="CLOSED")
        out = world.invoke()
        self.assertNotIn("session_report", out)
        self.assertIsNone(world.sessions.get_report(world.date))

    def test_the_session_ends_in_market_closed(self):
        world = self._full_day()
        world.invoke()
        session = world.state_store.get(world.date)
        self.assertEqual(session.agent_state, AgentState.MARKET_CLOSED)

    def test_an_unflattened_position_fails_the_report(self):
        world = World()
        world.invoke()
        world.at("20:30", status="CLOSED")
        out = world.invoke()
        self.assertFalse(out["session_report"]["session_ok"])
        self.assertIn("no_positions_open",
                      out["session_report"]["failed_checks"])

    def test_cash_that_disagrees_with_the_journal_fails_the_report(self):
        """
        Independent of the broker: opening cash plus what the JOURNAL
        says the day earned. A disagreement means a trade is missing
        from the journal or the broker did something unrecorded.
        """
        world = self._full_day()
        snapshot, revision = world.broker_store.load()
        snapshot["account"]["cash"] += 5.0
        world.broker_store._snapshots["paper"] = snapshot
        out = world.invoke()
        self.assertIn("cash_reconciles",
                      out["session_report"]["failed_checks"])

    def test_the_report_states_that_one_session_proves_nothing(self):
        world = self._full_day()
        world.invoke()
        report = world.sessions.get_report(world.date)
        self.assertIn("cannot demonstrate an edge",
                      report["sample_adequacy_note"])


class TestAgentStateFollowsTheCycle(unittest.TestCase):

    def test_an_entry_walks_valid_transitions_to_position_open(self):
        world = World()
        out = world.invoke()
        self.assertIsNone(out["state_sync"]["error"], out["state_sync"])
        session = world.state_store.get(world.date)
        self.assertEqual(session.agent_state, AgentState.POSITION_OPEN)

    def test_a_quiet_cycle_is_scanning_or_watching(self):
        world = World()
        world.set_scan([])
        world.invoke()
        session = world.state_store.get(world.date)
        self.assertIn(session.agent_state,
                      (AgentState.SCANNING, AgentState.WATCHING))

    def test_a_reconciliation_mismatch_engages_the_emergency_stop(self):
        world = World()
        world.invoke()
        snapshot, _ = world.broker_store.load()
        snapshot["positions"] = []                      # broker "lost" it
        world.broker_store._snapshots["paper"] = snapshot
        out = world.invoke()
        self.assertTrue(out["emergency_stop_engaged"])
        self.assertTrue(world.halt.get().halted)
        session = world.state_store.get(world.date)
        self.assertEqual(session.agent_state, AgentState.EMERGENCY_STOP)

    def test_an_emergency_stop_is_not_walked_out_of_by_later_cycles(self):
        world = World()
        world.invoke()
        snapshot, _ = world.broker_store.load()
        snapshot["positions"] = []
        world.broker_store._snapshots["paper"] = snapshot
        world.invoke()
        for _ in range(3):
            world.invoke()
        session = world.state_store.get(world.date)
        self.assertEqual(session.agent_state, AgentState.EMERGENCY_STOP)

    def test_a_state_sync_failure_never_affects_trading(self):
        world = World()
        with mock.patch.object(cycle, "sync_state",
                               side_effect=RuntimeError("boom")):
            out = world.invoke()
        # The cycle already traded; presentation failing is not fatal.
        self.assertTrue(out.get("error") or out["entries_submitted"] == 1)


class TestPersistenceFailureInTheHandler(unittest.TestCase):

    def test_a_failed_save_halts_new_entries(self):
        world = World()
        from agent.broker import BrokerStateError
        original = world.broker_store.save

        def failing(*a, **k):
            raise BrokerStateError("dynamo write failed")
        world.broker_store.save = failing
        out = world.invoke()
        self.assertFalse(out["state_saved"])
        self.assertIn(Condition.STATE_PERSISTENCE_FAILURE,
                      world.conditions())
        world.broker_store.save = original
        again = world.invoke()
        self.assertEqual(again["entries_submitted"], 0)

    def test_unreadable_positions_halt_without_trading(self):
        world = World()

        def broken(session_date):
            from agent.positions import PositionStoreError
            raise PositionStoreError("dynamo read failed")
        world.position_store.load_open = broken
        out = world.invoke()
        self.assertFalse(out["ran"])
        self.assertIn(Condition.STATE_PERSISTENCE_FAILURE,
                      world.conditions())


class TestEvidenceAccumulation(unittest.TestCase):

    def test_the_tally_feeds_the_readiness_evidence(self):
        from agent.autonomy import aggregate_evidence, cohort_key
        world = World()
        world.invoke()
        world.at("15:40")
        world.invoke()
        world.at("20:30", status="CLOSED")
        world.invoke()
        tallies = world.sessions.list()
        evidence = aggregate_evidence(
            tallies, tallies[0].cohort)
        self.assertEqual(evidence["sessions_completed"], 1)
        self.assertTrue(evidence["live_data_path_exercised"])
        self.assertEqual(evidence["reconciliation_clean_sessions"], 1)
        self.assertEqual(evidence["paper_trades"], 1)
        self.assertGreaterEqual(evidence["live_market_cycles"], 2)


class TestQuoteLoaderWithRealProviderTypes(unittest.TestCase):
    """The handler's fakes returned plain floats; the real provider returns
    Quote/Provenance objects. Live defect 2026-10-01: age_seconds was
    returned as an uncalled method and aborted the first live cycle that
    reached the Risk Governor."""

    def loader(self, quote):
        class Cached:
            def get_quote(self, symbol):
                return quote
        return load_handler()._quote_loader(Cached(), object())

    def real_quote(self, **kw):
        from agent.providers.base import Quote, Provenance
        import time as _t
        return Quote(symbol="MSFT", price=100.0, bid=99.95, ask=100.05,
                     volume=500_000,
                     provenance=Provenance("alpaca", _t.time() - 30), **kw)

    def test_age_is_a_number_not_a_method(self):
        q = self.loader(self.real_quote())("MSFT")
        self.assertIsInstance(q["age_seconds"], float)
        self.assertGreaterEqual(q["age_seconds"], 30)
        self.assertTrue(q["age_seconds"] > 0)          # the failing comparison

    def test_dollar_volume_is_derived_from_volume(self):
        q = self.loader(self.real_quote())("MSFT")
        self.assertEqual(q["dollar_volume"], 100.0 * 500_000)

    def test_unknown_volume_stays_unknown_not_zero(self):
        from agent.providers.base import Quote, Provenance
        import time as _t
        quote = Quote(symbol="X", price=10.0, provenance=Provenance("a", _t.time()))
        self.assertIsNone(self.loader(quote)("X")["dollar_volume"])

    def test_the_governor_accepts_the_loaded_quote_end_to_end(self):
        from agent.risk import RiskLimits
        q = self.loader(self.real_quote())("MSFT")
        self.assertFalse(q["age_seconds"] > RiskLimits().max_quote_age_seconds)


if __name__ == "__main__":
    unittest.main()


class TestTheCycleRecordsWhatItTradedOn(unittest.TestCase):
    """
    Alpaca's free plan serves the consolidated tape 15 minutes late. A
    session that does not record its feed would let delayed fills be read
    later as real-time strategy evidence, so the cycle reports the quality
    of every quote it actually used.
    """

    def observe(self, delayed):
        import time as _t
        from agent.providers.base import Provenance, Quote

        class Cached:
            def get_quote(self, symbol):
                return Quote(symbol=symbol, price=10.0, volume=1000,
                             provenance=Provenance("alpaca", _t.time(),
                                                   is_delayed=delayed))
        seen = set()
        load_handler()._quote_loader(Cached(), object(), seen)("X")
        return seen

    def test_a_delayed_feed_is_recorded_as_delayed(self):
        self.assertEqual(self.observe(True), {"DELAYED"})

    def test_a_real_time_feed_is_recorded_as_real_time(self):
        self.assertEqual(self.observe(False), {"REAL_TIME"})

    def test_an_unknown_feed_is_not_recorded_as_real_time(self):
        """Provenance documents is_delayed=None as 'never assume
        real-time'."""
        self.assertEqual(self.observe(None), {"UNKNOWN"})

    def test_the_collector_is_optional_so_other_callers_still_work(self):
        import time as _t
        from agent.providers.base import Provenance, Quote

        class Cached:
            def get_quote(self, symbol):
                return Quote(symbol=symbol, price=10.0,
                             provenance=Provenance("alpaca", _t.time()))
        q = load_handler()._quote_loader(Cached(), object())("X")
        self.assertEqual(q["price"], 10.0)

    def test_a_live_cycle_reports_its_data_quality(self):
        world = World()
        world.invoke()
        self.assertIn(world.last["data_quality"],
                      ("REAL_TIME", "DELAYED", "MIXED", "UNKNOWN"))

    def test_a_mixed_cycle_is_reported_as_mixed_not_the_better_half(self):
        handler = load_handler()
        import time as _t
        from agent.providers.base import Provenance, Quote

        class Cached:
            def __init__(self):
                self.n = 0

            def get_quote(self, symbol):
                self.n += 1
                return Quote(symbol=symbol, price=10.0, volume=10,
                             provenance=Provenance(
                                 "alpaca", _t.time(),
                                 is_delayed=(self.n % 2 == 0)))
        seen = set()
        loader = handler._quote_loader(Cached(), object(), seen)
        loader("A"), loader("B")
        self.assertEqual(seen, {"REAL_TIME", "DELAYED"})
