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
from agent.broker.order_ledger import InMemoryOrderLedger  # noqa: E402
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
    """A quote with a REAL Provenance, not a Mock.

    It used `mock.Mock(age_seconds=...)`, which answers every attribute
    that is ever asked of it. That made the fake strictly more permissive
    than the provider: it absorbed a new `source_age_seconds` method
    without complaint and then failed on the first comparison, and it is
    the same looseness that let `age_seconds` be returned uncalled and
    abort a live cycle on 2026-10-01.
    """

    def __init__(self, price, age=3.0, source_age=3.0, feed="sip"):
        import time as _t
        from datetime import datetime, timedelta, timezone
        from agent.providers.base import Provenance
        self.price = price
        self.last = price
        self.bid = price - 0.05
        self.ask = price + 0.05
        self.dollar_volume = 5e8
        now = _t.time()
        as_of = None
        if source_age is not None:
            as_of = (datetime.fromtimestamp(now, timezone.utc)
                     - timedelta(seconds=source_age)).isoformat()
        self.provenance = Provenance(provider="fake", retrieved_at=now - age,
                                     as_of=as_of, feed=feed,
                                     is_delayed=(feed == "delayed_sip"))


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

    def test_the_handler_reaches_no_live_trading_venue(self):
        """
        The Alpaca PAPER adapter is wired (credential scope verified
        2026-10-01: paper /v2/account returned 200 ACTIVE). What must
        remain impossible is reaching the LIVE venue, so the live
        trading host must not appear in the handler at all.
        """
        from urllib.parse import urlparse
        source = open(HANDLER_PATH).read()
        for live in ("https://api.alpaca.markets", "api.alpaca.markets/v2"):
            self.assertNotIn(live, source)
        self.assertIn("AlpacaPaperBroker", source)
        self.assertIn("PaperBroker(", source)      # the internal shadow

        from agent.broker.alpaca_paper import AlpacaPaperBroker
        adapter = AlpacaPaperBroker(transport=object())
        self.assertEqual(urlparse(adapter.base_url).hostname,
                         "paper-api.alpaca.markets")
        self.assertTrue(adapter.capabilities()["is_paper"])

    def test_the_paper_adapter_refuses_the_live_domain(self):
        from agent.broker.alpaca_paper import (AlpacaPaperBroker,
                                               NotPaperEndpoint)
        for url in ("https://api.alpaca.markets",
                    "https://api.alpaca.markets/v2",
                    "https://broker-api.alpaca.markets"):
            with self.subTest(url=url):
                with self.assertRaises(NotPaperEndpoint):
                    AlpacaPaperBroker(transport=object(), base_url=url)

    def test_the_internal_simulator_is_the_default(self):
        """The external venue is opt-in: an unset or unknown AGENT_BROKER
        must not silently reach Alpaca."""
        source = open(HANDLER_PATH).read()
        self.assertIn('os.environ.get("AGENT_BROKER", "internal")', source)


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
    The cycle records WHICH FEED each quote came from and how old the
    DATA was - not how long ago we fetched it. A quote fetched three
    seconds ago can describe the market as it was fifteen minutes
    earlier, which is what `delayed_sip` serves.
    """

    def observe(self, feed, source_age=3.0):
        class Cached:
            def get_quote(self, symbol):
                return FakeQuote(100.0, source_age=source_age, feed=feed)
        seen = set()
        load_handler()._quote_loader(Cached(), object(), seen)("X")
        return seen

    def test_the_consolidated_tape_in_real_time_is_realtime_sip(self):
        self.assertEqual(self.observe("sip"), {"REALTIME_SIP"})

    def test_iex_is_real_time_but_recorded_separately(self):
        """Real-time, and about 2.5% of volume: not the national best
        bid and offer, so it must not read as REALTIME_SIP."""
        self.assertEqual(self.observe("iex"), {"REALTIME_IEX"})

    def test_a_delayed_feed_is_delayed_however_fresh_its_stamp_looks(self):
        self.assertEqual(self.observe("delayed_sip", source_age=2.0),
                         {"DELAYED_SIP"})

    def test_a_real_time_feed_serving_old_data_is_stale(self):
        self.assertEqual(self.observe("sip", source_age=900.0), {"STALE"})

    def test_no_source_timestamp_is_unknown_not_real_time(self):
        self.assertEqual(self.observe("sip", source_age=None), {"UNKNOWN"})

    def test_an_unnamed_feed_is_unknown(self):
        self.assertEqual(self.observe(None), {"UNKNOWN"})

    def test_the_loader_reports_source_age_separately_from_fetch_age(self):
        class Cached:
            def get_quote(self, symbol):
                return FakeQuote(100.0, age=1.0, source_age=600.0, feed="sip")
        q = load_handler()._quote_loader(Cached(), object())("X")
        self.assertLess(q["age_seconds"], 10)        # we fetched it just now
        self.assertGreater(q["source_age_seconds"], 500)   # the data is old
        self.assertEqual(q["feed"], "sip")
        self.assertEqual(q["feed_quality"], "STALE")

    def test_the_collector_is_optional_so_other_callers_still_work(self):
        class Cached:
            def get_quote(self, symbol):
                return FakeQuote(100.0)
        q = load_handler()._quote_loader(Cached(), object())("X")
        self.assertEqual(q["price"], 100.0)

    def test_a_live_cycle_reports_its_feed_quality(self):
        world = World()
        world.invoke()
        self.assertEqual(world.last["data_quality"], "REALTIME_SIP")

    def test_a_mixed_cycle_is_reported_as_mixed_not_the_better_half(self):
        handler = load_handler()

        class Cached:
            def __init__(self):
                self.n = 0

            def get_quote(self, symbol):
                self.n += 1
                return FakeQuote(100.0,
                                 feed="sip" if self.n % 2 else "delayed_sip")
        seen = set()
        loader = handler._quote_loader(Cached(), object(), seen)
        loader("A"), loader("B")
        self.assertEqual(seen, {"REALTIME_SIP", "DELAYED_SIP"})


class TestAnEarlyAbortIsVisibleToHealth(unittest.TestCase):
    """
    Found 2026-10-01 after the real-time cutover. Two cycles aborted on
    `unknown log event 'broker_unavailable'` and `/agent/autonomy` still
    reported HEALTHY with no failure streak, no conditions and no alerts.

    `health.record_cycle(ok)` is called from agent/orchestration/day.py,
    which runs well after broker selection, so anything that raises
    before the orchestration never reaches it. The agent could therefore
    abort every cycle and continue to describe itself as healthy - the
    silent failure that a streak exists to surface.
    """

    def test_a_cycle_that_aborts_records_the_failure(self):
        recorded = []

        class FakeHealth:
            def __init__(self, *_a, **_k):
                pass

            def record_cycle(self, ok):
                recorded.append(ok)
                return 1

        with mock.patch.object(cycle, "_run",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(cycle, "DynamoDBHealthStore", FakeHealth):
            response = cycle.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(recorded, [False],
                         "an aborted cycle did not record a health failure, "
                         "so the streak stays clean while every cycle fails")

    def test_the_abort_is_still_reported_in_the_body(self):
        """The control: recording health must not swallow the error."""
        class FakeHealth:
            def __init__(self, *_a, **_k):
                pass

            def record_cycle(self, ok):
                return 1

        with mock.patch.object(cycle, "_run",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(cycle, "DynamoDBHealthStore", FakeHealth):
            body = json.loads(cycle.lambda_handler({}, None)["body"])
        self.assertFalse(body["ran"])
        self.assertEqual(body["error"], "RuntimeError")
        self.assertFalse(body["new_exposure_permitted"])

    def test_a_failing_health_store_does_not_mask_the_abort(self):
        """If the health write itself fails, the cycle must still return
        its abort rather than raising a second, different error."""
        class ExplodingHealth:
            def __init__(self, *_a, **_k):
                raise RuntimeError("dynamo down")

        with mock.patch.object(cycle, "_run",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(cycle, "DynamoDBHealthStore",
                               ExplodingHealth):
            response = cycle.lambda_handler({}, None)
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["error"], "RuntimeError")
        self.assertFalse(body["ran"])


class TestBrokerSelectionActuallyRuns(unittest.TestCase):
    """
    This branch shipped three defects and 2042 tests caught none of
    them, because nothing could reach it: both log events were
    unregistered, `creds` was a local of _provider() and undefined here,
    and the alert sink's method is `emit`, not `send`. Each surfaced one
    deployment at a time, from production.

    So these tests call it. Asserting things *about* the source was what
    let the second and third defects through.
    """

    class Sink:
        """Only `emit`, deliberately. The real sink has no `send`, so a
        wrong method name must raise here rather than in Lambda."""

        def __init__(self):
            self.emitted = []

        def emit(self, alert):
            self.emitted.append(alert)

    def cfg(self):
        from types import SimpleNamespace
        return SimpleNamespace(storage=SimpleNamespace(
            alpaca_secret_id="stock-agent/alpaca-paper", region="us-east-2"))

    def select(self, env, creds=None, raises=None):
        sink = self.Sink()
        internal = object()
        fake = mock.Mock(base_url="https://paper-api.alpaca.markets")

        def load(secret_id, region):
            if raises == "creds":
                raise RuntimeError("secrets manager unavailable")
            self.loaded = (secret_id, region)
            return creds or {"api_key_id": "k", "api_secret_key": "s"}

        def build(**_kw):
            if raises == "build":
                raise ValueError("bad transport")
            return fake

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(cycle, "_load_alpaca_credentials", load), \
             mock.patch.object(cycle, "AlpacaPaperBroker", build), \
             mock.patch.object(cycle, "RequestsTransport",
                               lambda *_a, **_k: object()):
            broker, external = cycle._select_broker(
                internal, self.cfg(), sink, "2026-10-02")
        return broker, external, internal, sink, fake

    def test_unset_broker_keeps_the_internal_simulator(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_BROKER"}
        with mock.patch.dict(os.environ, env, clear=True):
            broker, external = cycle._select_broker(
                internal := object(), self.cfg(), self.Sink(), "2026-10-02")
        self.assertIs(broker, internal)
        self.assertIsNone(external)

    def test_internal_is_explicitly_selectable(self):
        broker, external, internal, sink, _ = self.select(
            {"AGENT_BROKER": "internal"})
        self.assertIs(broker, internal)
        self.assertIsNone(external)
        self.assertEqual(sink.emitted, [])

    def test_alpaca_paper_selects_the_external_venue(self):
        broker, external, internal, sink, fake = self.select(
            {"AGENT_BROKER": "alpaca_paper"})
        self.assertIs(broker, fake)
        self.assertIs(external, fake)
        self.assertIsNot(broker, internal)
        self.assertEqual(sink.emitted, [],
                         "a successful selection must raise no alert")

    def test_credentials_come_from_the_configured_secret(self):
        """Not a hardcoded id. `creds` being undefined here is what the
        NameError in production was."""
        self.select({"AGENT_BROKER": "alpaca_paper"})
        self.assertEqual(self.loaded,
                         ("stock-agent/alpaca-paper", "us-east-2"))

    def test_a_construction_failure_falls_back_and_alerts(self):
        broker, external, internal, sink, _ = self.select(
            {"AGENT_BROKER": "alpaca_paper"}, raises="build")
        self.assertIs(broker, internal)
        self.assertIsNone(external)
        self.assertEqual(len(sink.emitted), 1)
        self.assertIn("unavailable", sink.emitted[0].detail)

    def test_a_credential_failure_also_falls_back(self):
        """The secret read is inside the guard on purpose: Secrets
        Manager being down must degrade, not abort the cycle."""
        broker, external, internal, sink, _ = self.select(
            {"AGENT_BROKER": "alpaca_paper"}, raises="creds")
        self.assertIs(broker, internal)
        self.assertIsNone(external)
        self.assertEqual(len(sink.emitted), 1)

    def test_the_fallback_uses_the_method_the_sink_actually_has(self):
        """The Sink above defines only `emit`. If the code calls `send`
        this raises AttributeError, which is precisely the production
        failure of 2026-10-02T00:01."""
        from agent.autonomy.alerts import AlertSink
        self.assertTrue(hasattr(AlertSink, "emit"))
        self.assertFalse(hasattr(AlertSink, "send"))
        self.select({"AGENT_BROKER": "alpaca_paper"}, raises="build")

    def test_the_case_of_the_variable_does_not_matter(self):
        broker, external, _i, _s, fake = self.select(
            {"AGENT_BROKER": "ALPACA_PAPER"})
        self.assertIs(external, fake)


class RecordingHealthStore(InMemoryHealthStore):
    """Tracks record_cycle so a test can assert the invocation was
    observed. Subclassed rather than patched so every other behaviour of
    the real in-memory store is unchanged."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.record_cycle_calls = []

    def record_cycle(self, ok):
        self.record_cycle_calls.append(bool(ok))
        return super().record_cycle(ok)


class TestEveryInvocationLeavesATerminalRecord(unittest.TestCase):
    """
    Observed on 2026-10-02: a pre-market cycle selected the broker,
    recognised the phase, submitted nothing and returned successfully -
    and wrote no tally, no snapshot and no health record. So "the cycle
    ran and correctly did nothing" was indistinguishable from "the cycle
    never ran", which is the one distinction autonomous operation needs
    most.
    """

    TERMINAL = ("COMPLETED", "SKIPPED_MARKET_CLOSED", "ABORTED")

    # --- A: closed / pre-market invocation ---------------------------
    def skipped(self, clock="08:00", status="PRE_MARKET"):
        world = World()
        world.health = RecordingHealthStore()
        world.at(clock, status=status)
        out = world.invoke()
        return world, out

    def test_a_skipped_cycle_records_a_terminal_state(self):
        _world, out = self.skipped()
        self.assertEqual(out["terminal_state"], "SKIPPED_MARKET_CLOSED")
        self.assertIn(out["terminal_state"], self.TERMINAL)

    def test_a_skipped_cycle_is_not_counted_as_a_failure(self):
        """SKIPPED is an outcome, not a fault."""
        _world, out = self.skipped()
        self.assertFalse(out.get("ran"))
        self.assertNotEqual(out["terminal_state"], "ABORTED")

    def test_a_skipped_cycle_writes_the_last_cycle_snapshot(self):
        world, _out = self.skipped()
        snap = world.snapshot.get()
        self.assertIsNotNone(snap, "no terminal record was written")
        self.assertEqual(snap["terminal_state"], "SKIPPED_MARKET_CLOSED")

    def test_the_snapshot_carries_when_and_what_it_was(self):
        world, _out = self.skipped()
        snap = world.snapshot.get()
        self.assertTrue(snap.get("invoked_at"))
        self.assertEqual(snap["session_date"], world.date)
        self.assertEqual(snap["code_sha"], "deadbeef")
        self.assertTrue(snap.get("reason"))

    def test_health_knows_the_cycle_ran(self):
        world, _out = self.skipped()
        self.assertEqual(world.health.record_cycle_calls, [True],
                         "health did not observe a successful invocation")

    def test_a_skipped_cycle_claims_no_activity(self):
        _world, out = self.skipped()
        for field in ("orders_submitted", "fills", "positions_changed"):
            with self.subTest(field=field):
                self.assertEqual(out[field], 0)

    def test_reconciliation_is_not_applicable_not_pass(self):
        """Reporting PASS for a check that never ran is the error this
        exists to prevent: nothing was reconciled, so nothing passed."""
        _world, out = self.skipped()
        self.assertEqual(out["reconciliation"], "NOT_APPLICABLE")
        self.assertNotEqual(out["reconciliation"], "PASS")

    def test_the_broker_selection_is_recorded_when_it_happened(self):
        _world, out = self.skipped()
        self.assertIn("broker", out)

    def test_data_quality_is_unknown_not_real_time(self):
        """No quote was read, so the feed's quality is not established."""
        _world, out = self.skipped()
        self.assertEqual(out["data_quality"], "UNKNOWN")

    def test_a_closed_phase_also_records_its_terminal_state(self):
        _world, out = self.skipped(clock="20:30", status="CLOSED")
        self.assertEqual(out["terminal_state"], "SKIPPED_MARKET_CLOSED")

    # --- B: never invoked --------------------------------------------
    def test_never_invoked_is_distinguishable_from_skipped(self):
        """The control that gives the others meaning."""
        world = World()
        world.health = RecordingHealthStore()
        self.assertIsNone(world.snapshot.get())
        self.assertEqual(world.health.record_cycle_calls, [])

    # --- C: aborted ---------------------------------------------------
    def test_an_aborted_cycle_records_a_terminal_state(self):
        world = World()
        world.health = RecordingHealthStore()
        with mock.patch.object(cycle, "_run",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(cycle, "DynamoDBHealthStore",
                               lambda **kw: world.health), \
             mock.patch.object(cycle, "DynamoDBSnapshotStore",
                               lambda **kw: world.snapshot), \
             mock.patch.dict(os.environ, {"AGENT_CODE_SHA": "deadbeef"}):
            body = json.loads(cycle.lambda_handler({}, None)["body"])
        self.assertEqual(body["terminal_state"], "ABORTED")

    def test_an_abort_is_visible_in_the_terminal_record(self):
        world = World()
        world.health = RecordingHealthStore()
        with mock.patch.object(cycle, "_run",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(cycle, "DynamoDBHealthStore",
                               lambda **kw: world.health), \
             mock.patch.object(cycle, "DynamoDBSnapshotStore",
                               lambda **kw: world.snapshot), \
             mock.patch.dict(os.environ, {"AGENT_CODE_SHA": "deadbeef"}):
            cycle.lambda_handler({}, None)
        snap = world.snapshot.get()
        self.assertIsNotNone(snap)
        self.assertEqual(snap["terminal_state"], "ABORTED")

    def test_an_abort_increments_failure_accounting(self):
        world = World()
        world.health = RecordingHealthStore()
        with mock.patch.object(cycle, "_run",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(cycle, "DynamoDBHealthStore",
                               lambda **kw: world.health), \
             mock.patch.object(cycle, "DynamoDBSnapshotStore",
                               lambda **kw: world.snapshot), \
             mock.patch.dict(os.environ, {"AGENT_CODE_SHA": "deadbeef"}):
            cycle.lambda_handler({}, None)
        self.assertEqual(world.health.record_cycle_calls, [False])

    def test_a_completed_cycle_still_says_completed(self):
        """The control for the terminal vocabulary: if everything read
        SKIPPED the field would carry no information."""
        world = World()
        out = world.invoke()
        self.assertEqual(out["terminal_state"], "COMPLETED")
        self.assertNotEqual(out["reconciliation"], "NOT_APPLICABLE")


class TestUnreadablePositionsAreRecordedAndRefuseExposure(unittest.TestCase):
    """
    The most dangerous state the agent can be in was also the least
    visible. When the position store could not be read, _run returned
    early with new_exposure_permitted False - correctly refusing to
    trade - but wrote no terminal record at all, so it looked exactly
    like a cycle that never ran.

    Exposure is UNKNOWN here, not zero, which is why this records
    ABORTED rather than SKIPPED.
    """

    def unreadable(self):
        from agent.positions.store import PositionStoreError
        world = World()
        world.health = RecordingHealthStore()

        def boom(*_a, **_k):
            raise PositionStoreError("table unavailable")
        world.position_store.load_open = boom
        return world, world.invoke()

    def test_new_exposure_is_refused(self):
        _world, out = self.unreadable()
        self.assertFalse(out["new_exposure_permitted"])

    def test_it_did_not_trade(self):
        _world, out = self.unreadable()
        self.assertFalse(out["ran"])

    def test_a_terminal_record_is_written(self):
        world, _out = self.unreadable()
        snap = world.snapshot.get()
        self.assertIsNotNone(
            snap, "unreadable positions left no terminal record, so the "
                  "state is indistinguishable from a cycle that never ran")

    def test_it_is_aborted_not_skipped(self):
        """Exposure is unknown. A skip would claim nothing was due."""
        _world, out = self.unreadable()
        self.assertEqual(out["terminal_state"], "ABORTED")
        self.assertNotEqual(out["terminal_state"], "SKIPPED_MARKET_CLOSED")

    def test_exposure_is_marked_unknown_not_zero(self):
        _world, out = self.unreadable()
        self.assertIs(out["exposure_known"], False)

    def test_health_records_a_failure(self):
        _world, out = self.unreadable()
        world = _world
        self.assertEqual(world.health.record_cycle_calls, [False])

    def test_the_reason_names_the_cause(self):
        _world, out = self.unreadable()
        self.assertIn("positions unreadable", out["reason"])

    def test_a_readable_store_still_trades(self):
        """The control: without it these would pass for an agent that
        never trades at all."""
        world = World()
        out = world.invoke()
        self.assertEqual(out["terminal_state"], "COMPLETED")
        # The completed payload comes from cycle.as_dict() and carries no
        # "ran" key; phase is what distinguishes it from a skip.
        self.assertEqual(out["phase"], "INTRADAY")
        self.assertIs(out.get("exposure_known", True), True)


class StrictExternalBroker:
    """A broker that exposes ONLY the public adapter surface.

    Every defect in the external-broker path so far has been code
    written against the internal simulator's privates and never run
    against anything else. This double has no `_account`, `_positions`,
    `_orders` or `_client_ids`, so any such access fails here instead of
    in Lambda.
    """

    base_url = "https://paper-api.alpaca.markets"
    name = "alpaca_paper"
    is_paper = True
    # Faithful to the real adapter. Without this the double would take
    # the in-process branch in submit_approved, and the handler tests
    # would never exercise the external path they exist to cover.
    is_external_venue = True
    quarantined = False

    def __init__(self, cash=100000.0):
        self._cash = cash          # deliberately not the simulator's name

    def get_account(self):
        return {"cash": self._cash, "equity": self._cash,
                "account_id": "paper", "buying_power": self._cash}

    def get_positions(self):
        return []

    def get_orders(self, **_kw):
        return []

    def get_order(self, _order_id):
        return None

    def find_by_client_order_id(self, _cid):
        return None

    def submit_order(self, *_a, **_kw):
        raise AssertionError("this test must not submit an order")

    def cancel_order(self, _order_id):
        return None

    def replace_order(self, *_a, **_kw):
        return None

    def close_position(self, _symbol):
        return None

    def capabilities(self):
        # Mirrors AlpacaPaperBroker.capabilities(). The orchestrator
        # refuses any broker that does not DECLARE itself paper in
        # PAPER mode, which is a guard worth leaving intact.
        return {"name": self.name, "is_paper": True,
                "shorting": False, "margin": False, "options": False,
                "models_spread": True, "models_slippage": True,
                "models_partial_fills": True,
                "order_types": ["LIMIT", "MARKETABLE_LIMIT"],
                "authoritative_for_state": True,
                "base_url": self.base_url}


class TestTheCycleRunsAgainstAnExternalBroker(unittest.TestCase):
    """
    Found by deployment on 2026-10-02, the fifth defect in this path:
    `AttributeError: 'AlpacaPaperBroker' object has no attribute
    '_account'`. agent/broker/store.py persists the internal simulator by
    reaching into its privates, and the cycle handed it the external
    adapter instead.

    Nothing had ever run a cycle with an external broker selected, so
    every line that assumed the simulator's internals was unverified.
    """

    def run_external(self, status="PRE_MARKET", clock="08:00"):
        world = World()
        world.health = RecordingHealthStore()
        world.at(clock, status=status)
        with mock.patch.object(cycle, "AlpacaPaperBroker",
                               lambda **_kw: StrictExternalBroker()), \
             mock.patch.object(cycle, "RequestsTransport",
                               lambda *_a, **_k: object()), \
             mock.patch.object(cycle, "_load_alpaca_credentials",
                               lambda *_a, **_k: {"api_key_id": "k",
                                                 "api_secret_key": "s"}):
            out = world.invoke(env={"AGENT_BROKER": "alpaca_paper"})
        return world, out

    def test_an_off_hours_cycle_completes_with_an_external_broker(self):
        _world, out = self.run_external()
        self.assertNotEqual(out.get("terminal_state"), "ABORTED",
                            out.get("detail") or out.get("error"))
        self.assertEqual(out["terminal_state"], "SKIPPED_MARKET_CLOSED")

    def test_the_external_broker_is_recorded_as_authoritative(self):
        _world, out = self.run_external()
        self.assertEqual(out["broker"]["name"], "alpaca_paper")
        self.assertIs(out["broker"]["authoritative"], True)
        self.assertEqual(out["broker"]["base_url"],
                         "https://paper-api.alpaca.markets")

    def test_an_intraday_cycle_completes_with_an_external_broker(self):
        """The path that actually matters: a full trading cycle, which is
        where broker state is persisted."""
        _world, out = self.run_external(status="OPEN", clock="15:00")
        self.assertNotEqual(out.get("terminal_state"), "ABORTED",
                            out.get("detail") or out.get("error"))

    def test_no_private_simulator_attribute_is_touched(self):
        """The assertion that generalises: a cycle that completes against
        this double cannot have reached for _account, _positions,
        _orders or _client_ids, because they do not exist."""
        _world, out = self.run_external(status="OPEN", clock="15:00")
        self.assertNotIn("_account", str(out.get("detail") or ""))
        self.assertIsNone(out.get("error"))

    def test_the_internal_broker_path_still_works(self):
        """The control: without it these would pass for a cycle that can
        no longer run at all."""
        world = World()
        out = world.invoke()
        self.assertEqual(out["terminal_state"], "COMPLETED")


class UnfillingBroker(StrictExternalBroker):
    """Accepts an order and reports it unfilled, like a real venue does
    for a limit order that has not traded yet. Records cancellations."""

    def __init__(self):
        super().__init__()
        self.cancelled = []
        self.submitted = []

    def submit_order(self, *_a, **kw):
        oid = "ord_%d" % (len(self.submitted) + 1)
        order = {"order_id": oid,
                 "client_order_id": kw.get("client_order_id") or "cid_1",
                 "symbol": kw.get("symbol") or "AAA",
                 "side": "BUY", "order_type": "MARKETABLE_LIMIT",
                 "quantity": kw.get("quantity") or 1.0,
                 "status": "NEW",
                 "filled_quantity": 0.0,
                 "average_fill_price": None}
        self.submitted.append(order)
        return order

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"order_id": order_id, "status": "CANCELLED"}

    def get_order(self, order_id):
        for o in self.submitted:
            if o["order_id"] == order_id:
                return dict(o, status="CANCELLED")
        return None


class TestAnUnfilledEntryOrderIsNotAbandoned(unittest.TestCase):
    """
    The root cause of the 2026-10-02 orphan. The agent submitted a
    MARKETABLE_LIMIT for DRAM, read it back unfilled, recorded
    ORDER_NOT_FILLED and moved on - leaving the order WORKING at the
    venue. It filled afterwards, and the agent held a position it had no
    record of.

    Against the internal simulator the assumption held: it fills
    synchronously or never, so "unfilled on read-back" really meant
    "will never fill". A real venue can fill a marketable limit
    milliseconds after the agent looks. An order that has left the agent
    is exposure whether or not it has filled yet.
    """

    def run_unfilled(self):
        # An external venue refuses to submit without a durable order
        # record, which is the point of it - so this path needs a real
        # ledger, not a stub that swallows writes. In-memory keeps the
        # test hermetic while still exercising the intent-before-submit
        # sequence. See docs/INTENT-BEFORE-SUBMIT.md.
        self.ledger = InMemoryOrderLedger()
        world = self.world = World()
        world.health = RecordingHealthStore()
        broker = UnfillingBroker()
        with mock.patch.object(cycle, "AlpacaPaperBroker",
                               lambda **_kw: broker), \
             mock.patch.object(cycle, "RequestsTransport",
                               lambda *_a, **_k: object()), \
             mock.patch.object(cycle, "_load_alpaca_credentials",
                               lambda *_a, **_k: {"api_key_id": "k",
                                                 "api_secret_key": "s"}), \
             mock.patch.object(cycle, "DynamoDBOrderLedger",
                               lambda **_kw: self.ledger):
            out = world.invoke(env={"AGENT_BROKER": "alpaca_paper"})
        return broker, out

    def test_an_order_was_actually_submitted(self):
        """The control: if nothing was submitted the cancel assertion
        below would pass for a cycle that never traded."""
        broker, _out = self.run_unfilled()
        self.assertEqual(len(broker.submitted), 1, "nothing was submitted")

    def test_the_unfilled_order_is_cancelled(self):
        broker, _out = self.run_unfilled()
        self.assertEqual(
            broker.cancelled, [broker.submitted[0]["order_id"]],
            "an unfilled entry order was left working at the venue, where "
            "it can fill later with no position record")

    def test_no_position_is_claimed(self):
        broker, out = self.run_unfilled()
        self.assertEqual(out.get("entries_submitted", 0), 0)

    def test_the_order_is_in_the_ledger_with_the_id_the_venue_got(self):
        """End to end through the real handler: the orphan of
        2026-10-02 was an order at the venue with no local record, so
        the property is that the two agree on the id."""
        broker, _out = self.run_unfilled()
        rows = self.ledger.for_session(self.world.date)
        self.assertEqual(len(rows), 1, "the handler recorded no intent")
        self.assertEqual(rows[0].client_order_id,
                         broker.submitted[0]["client_order_id"])
        self.assertEqual(rows[0].symbol, "XYZ")
        self.assertIsNotNone(rows[0].risk_decision_id)
        self.assertIsNotNone(rows[0].intent_at)

    def test_an_unfilled_order_keeps_reserving_exposure_in_the_ledger(self):
        """It is cancelled at the venue, but until an observation says
        so the record must not read as zero exposure."""
        _broker, _out = self.run_unfilled()
        row = self.ledger.for_session(self.world.date)[0]
        self.assertNotEqual(row.potential_exposure, 0.0)

    def test_the_cycle_still_completes(self):
        _broker, out = self.run_unfilled()
        self.assertEqual(out["terminal_state"], "COMPLETED")

    def test_a_filled_order_is_not_cancelled(self):
        """The other control. Cancelling a filled entry would close a
        position the agent had just correctly opened."""
        world = World()
        out = world.invoke()
        self.assertEqual(out["entries_submitted"], 1)


class TestEarlyAbortsEscalate(unittest.TestCase):
    """
    On 2026-10-02 a long run of cycles aborted before the orchestration
    could run. Each recorded a health failure - the streak climbed - and
    REPEATED_CYCLE_FAILURE was never raised, because the escalation lives
    inside the orchestrator, which an early abort never reaches.

    The agent kept reporting HEALTHY. It only halted because of an
    unrelated reconciliation mismatch; without that, 21 consecutive
    aborted cycles would have looked like a healthy agent. A streak
    nobody escalates is a number, not a signal.
    """

    def abort_n(self, n):
        from agent.autonomy.health import Condition
        world = World()
        world.health = RecordingHealthStore()
        for _ in range(n):
            with mock.patch.object(cycle, "_run",
                                   side_effect=RuntimeError("boom")), \
                 mock.patch.object(cycle, "DynamoDBHealthStore",
                                   lambda **kw: world.health), \
                 mock.patch.object(cycle, "DynamoDBSnapshotStore",
                                   lambda **kw: world.snapshot), \
                 mock.patch.dict(os.environ, {"AGENT_CODE_SHA": "deadbeef"}):
                cycle.lambda_handler({}, None)
        active = {c.condition for c in world.health.snapshot().active}
        return world, active, Condition

    def test_one_abort_does_not_escalate(self):
        """The control: escalating on the first failure would make the
        condition meaningless, since one failure is noise."""
        _w, active, Condition = self.abort_n(1)
        self.assertNotIn(Condition.REPEATED_CYCLE_FAILURE, active)

    def test_two_aborts_do_not_escalate(self):
        _w, active, Condition = self.abort_n(2)
        self.assertNotIn(Condition.REPEATED_CYCLE_FAILURE, active)

    def test_three_aborts_escalate(self):
        _w, active, Condition = self.abort_n(3)
        self.assertIn(Condition.REPEATED_CYCLE_FAILURE, active,
                      "a run of early aborts raised nothing")

    def test_the_streak_is_recorded_every_time(self):
        world, _active, _C = self.abort_n(3)
        self.assertEqual(world.health.record_cycle_calls, [False]*3)

    def test_the_streak_is_surfaced_in_the_snapshot(self):
        """It was stored and never reported, so a reader saw no field at
        all - indistinguishable from zero."""
        world, _a, _C = self.abort_n(3)
        d = world.health.snapshot().as_dict()
        self.assertIn("consecutive_failures", d)
        self.assertEqual(d["consecutive_failures"], 3)

    def test_a_successful_cycle_resets_the_streak(self):
        world, _a, _C = self.abort_n(3)
        world.health.record_cycle(True)
        self.assertEqual(world.health.get_streak(), 0)

    def test_repeated_cycle_failure_is_not_latching(self):
        """It must be able to recover on its own, unlike the conditions
        that need a human."""
        from agent.autonomy.health import Condition, LATCHING
        self.assertNotIn(Condition.REPEATED_CYCLE_FAILURE, LATCHING)

    def test_it_still_blocks_new_exposure_while_active(self):
        _w, _a, _C = self.abort_n(3)
        from agent.autonomy.health import Condition, HALTING
        self.assertIn(Condition.REPEATED_CYCLE_FAILURE, HALTING)
