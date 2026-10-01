"""
The autonomy surface: what the dashboard and the chat can see.

These tests close the loop. The cycle Lambda WRITES records; the API
Lambda READS them; the chat answers FROM them. Everything runs against
the same in-memory stores, so a record the cycle forgets to write or the
API reads wrongly shows up here rather than in front of the user.

The chat is deterministic and names the records each answer was built
from. The tests check both that it answers from the record and that,
when the record is silent, it says so instead of inventing a reason.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

from agent.autonomy import (                                      # noqa: E402
    CYCLE_CRON, Condition, classify_question, extract_symbol, explain,
    next_cycle_time,
)
from agent.state.service import AgentStateService                 # noqa: E402
from test_agent_dashboard import handler as api                    # noqa: E402
from test_cycle_handler import FakeQuote, World                    # noqa: E402
from datetime import datetime, timezone                            # noqa: E402


def call(world, path, **query):
    """Invoke the real API handler against the world's stores."""
    patches = {
        "DynamoDBSnapshotStore": lambda **kw: world.snapshot,
        "DynamoDBHealthStore": lambda **kw: world.health,
        "DynamoDBSessionStore": lambda **kw: world.sessions,
        "DynamoDBDecisionLog": lambda **kw: world.decisions,
        "DynamoDBAlertSink": lambda **kw: world.alerts,
        "DynamoDBPositionStore": lambda **kw: world.position_store,
        "_journal": lambda: world.journal,
        "_state_service": lambda: AgentStateService(world.state_store),
        "today_market_date": lambda: world.date,
    }
    stack = [mock.patch.object(api, k, v) for k, v in patches.items()]
    for p in stack:
        p.start()
    try:
        event = {"requestContext": {"http": {"method": "GET", "path": path}},
                 "queryStringParameters": query or None}
        r = api.lambda_handler(event, None)
    finally:
        for p in reversed(stack):
            p.stop()
    return r["statusCode"], json.loads(r["body"] or "{}")


def ask(world, question):
    code, body = call(world, "/agent/ask", q=question)
    assert code == 200, body
    return body


def traded_day():
    """A day with a real entry and a real stop-out."""
    world = World()
    world.invoke()
    world.quotes["XYZ"] = FakeQuote(96.0)
    world.invoke()
    world.quotes["XYZ"] = FakeQuote(100.0)
    return world


class TestClassification(unittest.TestCase):

    CASES = {
        "What are you doing right now?": "NOW",
        "what are you up to": "NOW",
        "Why haven't you traded?": "WHY_NO_TRADE",
        "why no trades today": "WHY_NO_TRADE",
        "Why did you reject AAPL?": "WHY_REJECTED",
        "why did you pass on TSLA": "WHY_REJECTED",
        "Why did you open NVDA?": "WHY_OPENED",
        "why did you buy NVDA": "WHY_OPENED",
        "Why did you exit TSLA?": "WHY_EXITED",
        "why did you sell XYZ": "WHY_EXITED",
        "How much risk remains today?": "RISK_REMAINING",
        "Are you healthy?": "HEALTH",
        "Did broker reconciliation pass?": "RECONCILIATION",
        "How did today go?": "HOW_TODAY",
    }

    def test_every_supported_question_is_recognised(self):
        for question, intent in self.CASES.items():
            with self.subTest(question=question):
                self.assertEqual(classify_question(question), intent)

    def test_an_unrelated_question_is_not_forced_into_an_intent(self):
        for question in ("tell me a joke", "what is the weather", "", "  "):
            with self.subTest(question=question):
                self.assertIsNone(classify_question(question))

    def test_ordinary_words_are_not_mistaken_for_tickers(self):
        for question in ("How did today go?", "What are you doing now?",
                         "Are you healthy?", "Did it pass?"):
            with self.subTest(question=question):
                self.assertIsNone(extract_symbol(question))

    def test_a_named_ticker_is_extracted(self):
        self.assertEqual(extract_symbol("Why did you reject AAPL?"), "AAPL")
        self.assertEqual(extract_symbol("why did you exit TSLA"), "TSLA")


class TestAnswersComeFromTheRecord(unittest.TestCase):

    def test_why_opened_cites_the_stored_decision(self):
        world = traded_day()
        out = ask(world, "Why did you open XYZ?")
        self.assertTrue(out["grounded"])
        self.assertIn("Opened XYZ", out["answer"])
        self.assertIn("decisions", out["sources"])
        row = world.decisions.for_session(world.date)[0]
        self.assertEqual(out["decision_row_id"], row["decision_row_id"])
        self.assertIn(row["hypothesis"]["strategy"], out["answer"])

    def test_why_opened_states_the_versions_it_was_made_under(self):
        out = ask(traded_day(), "Why did you open XYZ?")
        self.assertIn("deadbeef", out["answer"])

    def test_why_exited_reports_every_reason_that_fired(self):
        world = traded_day()
        out = ask(world, "Why did you exit XYZ?")
        [trade] = world.journal.list_trades(world.date)
        self.assertIn(trade.exit_reason, out["answer"])
        self.assertEqual(out["trade_id"], trade.trade_id)
        self.assertIn("Every rule that fired is recorded", out["answer"])

    def test_why_rejected_lists_every_governor_reason_not_just_one(self):
        world = World()
        world.quotes["XYZ"] = FakeQuote(100.0)
        world.quotes["XYZ"].bid, world.quotes["XYZ"].ask = 99.0, 101.0
        world.invoke()
        out = ask(world, "Why did you reject XYZ?")
        row = world.decisions.for_session(world.date)[0]
        self.assertEqual(row["outcome"], "REFUSED")
        self.assertIn("Risk Governor refused", out["answer"])
        for reason in row["risk"]["reasons"]:
            self.assertIn(reason, out["answer"])

    def test_why_rejected_a_cooldown_symbol_says_cooldown(self):
        world = traded_day()
        world.invoke()                                    # re-offered
        out = ask(world, "Why did you reject XYZ?")
        self.assertIn("COOLDOWN", out["answer"])

    def test_why_no_trade_summarises_the_day_honestly(self):
        world = World()
        world.set_regime(fresh=False)                     # stale regime
        world.invoke()
        out = ask(world, "Why haven't you traded?")
        self.assertIn("entered none", out["answer"])
        self.assertIn("ceiling, not a target", out["answer"])

    def test_why_no_trade_after_trading_says_it_has_traded(self):
        out = ask(traded_day(), "Why haven't you traded?")
        self.assertIn("I have traded today", out["answer"])

    def test_risk_remaining_uses_the_stored_limits_and_counters(self):
        world = traded_day()
        world.invoke()
        out = ask(world, "How much risk remains today?")
        self.assertTrue(out["grounded"])
        self.assertIn("ceilings, not targets", out["answer"])
        self.assertIn("cumulative gross", out["answer"])

    def test_health_reports_blocking_conditions(self):
        world = World()
        # Latching: a non-latching condition is correctly cleared by the
        # next healthy cycle, which would make this assertion vacuous.
        world.health.raise_condition(Condition.UNCERTAIN_ORDER_STATE, "x")
        world.invoke()
        out = ask(world, "Are you healthy?")
        self.assertIn("blocked", out["answer"])
        self.assertIn("UNCERTAIN_ORDER_STATE", out["answer"])

    def test_health_says_exits_are_always_permitted(self):
        world = World()
        world.invoke()
        self.assertIn("exits are always permitted",
                      ask(world, "Are you healthy?")["answer"])

    def test_reconciliation_reports_pass_and_the_days_count(self):
        world = World()
        world.invoke()
        out = ask(world, "Did broker reconciliation pass?")
        self.assertIn("PASSED", out["answer"])
        self.assertIn("1 check", out["answer"])

    def test_reconciliation_reports_a_failure(self):
        world = World()
        world.invoke()
        snap, _ = world.broker_store.load()
        snap["positions"] = []
        world.broker_store._snapshots["paper"] = snap
        world.invoke()
        out = ask(world, "Did broker reconciliation pass?")
        self.assertIn("FAILED", out["answer"])

    def test_what_am_i_doing_reports_state_mode_and_real_money(self):
        world = World()
        world.invoke()
        out = ask(world, "What are you doing right now?")
        self.assertIn("real money disabled", out["answer"])
        self.assertIn("PAPER", out["answer"])

    def test_how_did_today_go_before_the_close_says_no_report_yet(self):
        world = World()
        world.invoke()
        self.assertIn("written after the close",
                      ask(world, "How did today go?")["answer"])

    def test_how_did_today_go_after_the_close_reports_the_checks(self):
        world = World()
        world.invoke()
        world.at("15:40")
        world.invoke()
        world.at("20:30", status="CLOSED")
        world.invoke()
        out = ask(world, "How did today go?")
        self.assertIn("ALL CHECKS PASSED", out["answer"])
        self.assertIn("cannot demonstrate an edge", out["answer"])


class TestTheRecordIsSilentSoTheChatSaysSo(unittest.TestCase):
    """
    "I have no decision recorded for AAPL" is a fact. A plausible reason
    that was never stored is a fabrication.
    """

    def test_a_symbol_never_evaluated_gets_no_invented_reason(self):
        world = traded_day()
        out = ask(world, "Why did you reject AAPL?")
        self.assertFalse(out["grounded"])
        self.assertIn("no record", out["answer"])
        self.assertIn("will not guess", out["answer"])

    def test_opening_a_symbol_never_opened_is_refused(self):
        out = ask(traded_day(), "Why did you open NVDA?")
        self.assertFalse(out["grounded"])
        self.assertIn("no record", out["answer"])

    def test_exiting_a_symbol_never_held_is_refused(self):
        out = ask(traded_day(), "Why did you exit TSLA?")
        self.assertFalse(out["grounded"])

    def test_an_empty_day_has_no_cycle_to_describe(self):
        out = ask(World(), "What are you doing right now?")
        self.assertFalse(out["grounded"])

    def test_a_symbol_question_without_a_symbol_asks_which(self):
        out = ask(traded_day(), "Why did you reject it?")
        self.assertFalse(out["grounded"])
        self.assertIn("Which symbol", out["answer"])

    def test_rejecting_a_symbol_that_was_entered_is_corrected(self):
        world = World()
        world.invoke()                       # one entry, nothing after it
        out = ask(world, "Why did you reject XYZ?")
        self.assertIn("I did not reject XYZ", out["answer"])

    def test_an_unsupported_question_lists_what_can_be_answered(self):
        out = ask(World(), "tell me a joke")
        self.assertFalse(out["grounded"])
        self.assertIn("why I haven't traded", out["answer"])

    def test_no_answer_ever_claims_to_use_a_language_model(self):
        """The authority is the stored row, never a generation."""
        world = traded_day()
        for q in TestClassification.CASES:
            with self.subTest(question=q):
                self.assertFalse(ask(world, q)["llm_used"])

    def test_the_explainer_does_not_import_a_language_model_client(self):
        body = open(os.path.join(REPO_ROOT, "agent", "autonomy",
                                 "explain.py")).read().lower()
        for banned in ("import openai", "from openai", "chat.completions",
                       "gpt-", "anthropic"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, body)

    def test_ask_requires_a_question(self):
        code, _ = call(World(), "/agent/ask")
        self.assertEqual(code, 400)


class TestTheAutonomyEndpoint(unittest.TestCase):

    def test_it_states_paper_mode_and_real_money_disabled(self):
        world = World()
        world.invoke()
        _code, body = call(world, "/agent/autonomy")
        self.assertEqual(body["banner"], "AUTONOMOUS PAPER MODE")
        self.assertEqual(body["real_money"], "DISABLED")
        self.assertEqual(body["overnight_positions"], "DISABLED")
        self.assertFalse(body["live_trading_enabled"])
        self.assertFalse(body["can_place_orders"])

    def test_it_reports_what_the_agent_is_actually_doing(self):
        world = traded_day()
        world.invoke()
        _code, body = call(world, "/agent/autonomy")
        self.assertEqual(body["execution_mode"], "PAPER")
        self.assertIsNotNone(body["last_cycle"])
        self.assertIsNotNone(body["tally"])
        self.assertTrue(body["decisions"])
        self.assertIn("limits", body)
        self.assertEqual(body["health"]["state"], "HEALTHY")

    def test_positions_are_visible(self):
        world = World()
        world.invoke()
        _code, body = call(world, "/agent/autonomy")
        self.assertEqual([p["symbol"] for p in body["positions"]], ["XYZ"])

    def test_the_next_cycle_is_stated(self):
        _code, body = call(World(), "/agent/autonomy")
        self.assertTrue(body["next_cycle_at"])

    def test_one_unreadable_table_degrades_one_panel_not_the_page(self):
        """A dashboard that 500s on a cold table looks like a broken
        agent."""
        world = World()
        world.invoke()

        class Broken:
            def for_session(self, *a, **k):
                raise RuntimeError("dynamo read failed")
        world.decisions = Broken()
        code, body = call(world, "/agent/autonomy")
        self.assertEqual(code, 200)
        self.assertIn("decisions", body["read_errors"])
        self.assertIsNotNone(body["last_cycle"])

    def test_an_unreadable_health_record_is_reported_as_halted(self):
        """Fail closed in the REPORT as well as in the engine."""
        world = World()
        world.invoke()

        class Broken:
            def snapshot(self):
                raise RuntimeError("dynamo read failed")
        world.health = Broken()
        _code, body = call(world, "/agent/autonomy")
        self.assertEqual(body["health"]["state"], "HALTED")
        self.assertFalse(body["health"]["entries_permitted"])

    def test_decisions_can_be_filtered_by_symbol(self):
        world = traded_day()
        _c, body = call(world, "/agent/decisions", symbol="xyz")
        self.assertTrue(body["decisions"])
        self.assertTrue(all(d["symbol"] == "XYZ" for d in body["decisions"]))
        _c, none = call(world, "/agent/decisions", symbol="AAPL")
        self.assertEqual(none["count"], 0)


class TestTheApiStillCannotTrade(unittest.TestCase):
    """The new endpoints must not have widened the API's authority."""

    def test_there_is_still_exactly_one_post(self):
        posts = [(m, p) for m, p in api.ROUTES if m != "GET"]
        self.assertEqual(posts, [("POST", "/agent/regime/evaluate")])

    def test_importing_the_handler_loads_no_broker_or_orchestrator(self):
        """
        Checked on the MODULE GRAPH in a clean interpreter, which is
        stronger than scanning the source: a transitive import would not
        show up in handler.py at all.
        """
        code = (
            "import sys, importlib.util;"
            "sys.path.insert(0, %r); sys.path.insert(0, %r);"
            "spec = importlib.util.spec_from_file_location('h', %r);"
            "m = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(m);"
            "bad = sorted(k for k in sys.modules if k.startswith("
            "('agent.broker', 'agent.orchestration')));"
            "print('BAD:' + ','.join(bad))"
        ) % (REPO_ROOT, os.path.join(REPO_ROOT, "lambda-micro",
                                     "chatbot-router"),
             os.path.join(REPO_ROOT, "lambda-micro", "agent-api",
                          "handler.py"))
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)
        self.assertIn("BAD:", out.stdout, out.stderr[-400:])
        line = [l for l in out.stdout.splitlines()
                if l.startswith("BAD:")][-1]
        self.assertEqual(line, "BAD:", f"loaded: {line}")

    def test_falsifying_control_the_module_graph_check_can_see_a_broker(self):
        """The check above would pass if it never looked at sys.modules."""
        code = ("import sys; sys.path.insert(0, %r);"
                "import agent.broker;"
                "print('BAD:' + ','.join(sorted(k for k in sys.modules "
                "if k.startswith('agent.broker'))))") % REPO_ROOT
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)
        line = [l for l in out.stdout.splitlines()
                if l.startswith("BAD:")][-1]
        self.assertIn("agent.broker", line)


class TestReadinessAccumulatesEvidenceHonestly(unittest.TestCase):

    def _full_day(self, world):
        world.invoke()
        world.at("15:40")
        world.invoke()
        world.at("20:30", status="CLOSED")
        world.invoke()

    def test_a_finished_session_counts_but_the_gate_stays_closed(self):
        """
        Evidence accumulates; the gates are NOT changed to make them
        pass. One clean session is one session of twenty.
        """
        world = World()
        self._full_day(world)
        _c, body = call(world, "/agent/readiness")
        self.assertFalse(body["ready_for_real_money"])
        self.assertEqual(body["evidence"]["sessions_completed"], 1)
        self.assertTrue(body["evidence"]["live_data_path_exercised"])
        gate = {g["name"]: g for g in body["unmet"]}
        self.assertEqual(gate["pilot_ran_unattended"]["status"], "UNMET")
        self.assertIn("sessions_completed=1",
                      gate["pilot_ran_unattended"]["detail"])

    def test_the_live_data_gate_is_met_by_a_real_session(self):
        world = World()
        self._full_day(world)
        _c, body = call(world, "/agent/readiness")
        names = {g["name"] for g in body["unmet"]}
        self.assertNotIn("live_data_path_exercised", names)

    def test_no_evidence_yet_leaves_the_gates_unknown_not_met(self):
        _c, body = call(World(), "/agent/readiness")
        self.assertFalse(body["ready_for_real_money"])
        statuses = {g["name"]: g["status"] for g in body["unmet"]}
        self.assertEqual(statuses["pilot_ran_unattended"], "UNKNOWN")

    def test_a_session_from_another_cohort_is_not_pooled(self):
        """
        Different behaviour is a different experiment. Adding its
        sessions would produce a number describing neither.
        """
        world = World()
        self._full_day(world)
        from agent.autonomy import SessionTally
        other = SessionTally(session_date="2026-09-01", cohort="cohort-old",
                             finalized=True, cycles_live_market=40,
                             live_cycles_with_real_quotes=40)
        world.sessions.put(other)
        _c, body = call(world, "/agent/readiness")
        self.assertEqual(body["evidence"]["sessions_completed"], 1)
        self.assertEqual(body["evidence"]["sessions_in_other_cohorts"], 1)

    def test_the_zero_trade_day_still_counts_as_a_completed_session(self):
        world = World()
        world.set_scan([])
        self._full_day(world)
        _c, body = call(world, "/agent/readiness")
        self.assertEqual(body["evidence"]["sessions_completed"], 1)
        self.assertEqual(body["evidence"]["zero_trade_days"], 1)
        self.assertEqual(body["evidence"]["paper_trades"], 0)

    def test_the_sample_gate_is_unchanged_by_a_good_looking_day(self):
        """A single winning day must not read as an edge."""
        world = World()
        self._full_day(world)
        _c, body = call(world, "/agent/readiness")
        edge = {g["name"]: g for g in body["unmet"]}["demonstrated_edge"]
        self.assertNotEqual(edge["status"], "MET")


class TestSchedule(unittest.TestCase):

    def test_the_cron_matches_what_the_dashboard_describes(self):
        self.assertEqual(CYCLE_CRON, "cron(2/5 13-21 ? * MON-FRI *)")

    def test_the_cycle_runs_after_the_scanner(self):
        """Two minutes behind it, so it reads the freshest scan."""
        self.assertTrue(CYCLE_CRON.startswith("cron(2/5"))

    def test_the_window_covers_both_edt_and_est_sessions(self):
        """
        EDT: 13:30-20:00 UTC. EST: 14:30-21:00 UTC. A 13-20 window
        silently misses the last hour after the clocks change.
        """
        for hour in (13, 14, 20, 21):
            with self.subTest(hour=hour):
                t = next_cycle_time(datetime(2026, 10, 1, hour, 0,
                                             tzinfo=timezone.utc))
                self.assertEqual(t.hour, hour)

    def test_friday_evening_rolls_to_monday(self):
        t = next_cycle_time(datetime(2026, 10, 2, 21, 58,
                                     tzinfo=timezone.utc))
        self.assertEqual(t.weekday(), 0)


class TestStoresUseTablesWithTheirKeySchema(unittest.TestCase):
    """
    The state table is keyed on session_date alone; the autonomy stores
    write PK/SK records. Pointing them at it passed every in-memory test
    and failed with a ValidationException only against the real table,
    which the API then (correctly) reported as HALTED.
    """

    def test_no_autonomy_store_defaults_to_the_session_keyed_state_table(self):
        import glob
        for path in glob.glob(os.path.join(REPO_ROOT, "agent", "autonomy",
                                           "*.py")):
            body = open(path).read()
            with self.subTest(file=os.path.basename(path)):
                self.assertNotIn("stock-agent-dev-state", body)
                self.assertNotIn("AGENT_STATE_TABLE", body)


if __name__ == "__main__":
    unittest.main()
