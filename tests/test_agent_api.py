"""
Tests for the agent API and chat grounding (Milestone 3).

No AWS, no brokerage, no network: the store, provider and config are
injected. The point of these tests is the safety surface - that the API
reports no execution capability, that the admin endpoint is closed by
default, and that chat cannot invent state it does not have.
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.chat_grounding import (  # noqa: E402
    UNAVAILABLE, answer_from_state, build_context, classify_question,
)
from agent.config import AgentConfig  # noqa: E402
from agent.state import (  # noqa: E402
    AgentState, AgentStateService, InMemoryStateStore, MarketSession,
)


def _load_agent_api():
    """Load lambda-micro/agent-api/handler.py under a UNIQUE module name.

    Both agent-api and chatbot-router ship a module called `handler`, as
    they must - the Lambda handler configuration names the file. Importing
    either as plain `handler` puts it in sys.modules under that key, and
    whichever test file imports first wins: the chatbot tests then ran
    against the agent API's handler and failed, but only when the whole
    suite ran together. Loading by path under a distinct name keeps the
    two apart.
    """
    import importlib.util
    path = os.path.join(REPO_ROOT, "lambda-micro", "agent-api", "handler.py")
    spec = importlib.util.spec_from_file_location("agent_api_handler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_api_handler"] = module
    spec.loader.exec_module(module)
    return module


api = _load_agent_api()  # noqa: E402


CLOCK = {"is_open": True, "timestamp": "2026-09-30T12:00:00-04:00",
         "next_open": "2026-10-01T09:30:00-04:00",
         "next_close": "2026-09-30T16:00:00-04:00"}
CALENDAR = [{"date": "2026-09-30", "open": "09:30", "close": "16:00",
             "session_open": "0400", "session_close": "2000"}]


class FakeProvider:
    name = "fake"

    def get_clock(self):
        return dict(CLOCK)

    def get_calendar(self, start, end):
        return list(CALENDAR)

    def get_snapshot(self, symbols):
        return {}

    def get_quote(self, symbol):
        raise NotImplementedError

    def get_bars(self, symbol, timeframe="1day", limit=100):
        raise NotImplementedError


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStateStore()
        self.config = AgentConfig()
        self.state = AgentStateService(self.store, self.config,
                                       clock=lambda: "2026-09-30")
        self.provider = FakeProvider()

        # Inject fakes in place of AWS and the broker.
        api._CONFIG = self.config
        api._PROVIDER = (self.provider, self.provider)
        self._orig_state_service = api._state_service
        api._state_service = lambda: self.state
        os.environ.pop("AGENT_ADMIN_ENABLED", None)

    def tearDown(self):
        api._state_service = self._orig_state_service
        api._CONFIG = None
        api._PROVIDER = None
        os.environ.pop("AGENT_ADMIN_ENABLED", None)

    def call(self, method, path, body=None):
        event = {"httpMethod": method, "path": path}
        if body is not None:
            event["body"] = json.dumps(body)
        resp = api.lambda_handler(event, None)
        return resp["statusCode"], json.loads(resp["body"] or "{}"), resp


class TestRouting(ApiTestBase):
    def test_unknown_route_is_404_and_lists_what_exists(self):
        status, body, _ = self.call("GET", "/agent/nope")
        self.assertEqual(status, 404)
        self.assertIn("available", body)

    def test_options_preflight(self):
        _status, _body, resp = self.call("OPTIONS", "/agent/status")
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(resp["headers"]["Access-Control-Allow-Origin"], "*")

    def test_cors_headers_on_every_response(self):
        for method, path in (("GET", "/agent/status"),
                             ("GET", "/agent/market-regime"),
                             ("GET", "/agent/nope")):
            _s, _b, resp = self.call(method, path)
            self.assertEqual(resp["headers"]["Access-Control-Allow-Origin"], "*")

    def test_http_api_event_shape_is_accepted(self):
        event = {"requestContext": {"http": {"method": "GET",
                                             "path": "/agent/status"}}}
        resp = api.lambda_handler(event, None)
        self.assertEqual(resp["statusCode"], 200)

    def test_trailing_slash_is_tolerated(self):
        status, _b, _r = self.call("GET", "/agent/status/")
        self.assertEqual(status, 200)


class TestStatusEndpoint(ApiTestBase):
    def test_returns_real_state(self):
        status, body, _ = self.call("GET", "/agent/status")

        self.assertEqual(status, 200)
        self.assertEqual(body["session_date"], "2026-09-30")
        self.assertEqual(body["market_session"], "OPEN")
        self.assertIn("agent_state", body)
        self.assertIn("revision", body)

    def test_reports_that_no_execution_exists(self):
        """The most important field on this endpoint."""
        _s, body, _ = self.call("GET", "/agent/status")
        self.assertFalse(body["execution_available"])
        self.assertFalse(body["trading_enabled"])
        self.assertIn("no broker adapter", body["execution_note"])

    def test_includes_capital_and_pnl_even_at_zero(self):
        _s, body, _ = self.call("GET", "/agent/status")
        self.assertEqual(body["pnl"]["realized"], 0.0)
        self.assertEqual(body["pnl"]["unrealized"], 0.0)
        self.assertEqual(body["capital"]["capital_deployed"], 0.0)
        self.assertEqual(body["capital"]["daily_capital_limit"], 50.0)
        self.assertEqual(body["positions"]["open"], 0)

    def test_confidence_meaning_is_stated(self):
        _s, body, _ = self.call("GET", "/agent/status")
        self.assertIn("NOT a probability", body["regime"]["confidence_meaning"])

    def test_open_market_drives_the_agent_to_scanning(self):
        self.call("GET", "/agent/status")
        self.assertIs(self.state.get_session().agent_state, AgentState.SCANNING)

    def test_safety_states_are_never_overridden_by_the_market(self):
        """An emergency stop must survive a status poll on an open market."""
        self.state.trigger_emergency_stop("test")
        self.call("GET", "/agent/status")
        session = self.state.get_session()
        self.assertIs(session.agent_state, AgentState.EMERGENCY_STOP)
        self.assertTrue(session.emergency_stop)

    def test_risk_lock_is_never_overridden_by_the_market(self):
        self.state.set_daily_risk_lock("loss limit")
        self.call("GET", "/agent/status")
        self.assertIs(self.state.get_session().agent_state,
                      AgentState.DAILY_RISK_LOCK)


class TestMarketRegimeEndpoint(ApiTestBase):
    def test_says_so_when_nothing_has_been_evaluated(self):
        status, body, _ = self.call("GET", "/agent/market-regime")
        self.assertEqual(status, 200)
        self.assertFalse(body["evaluated"])
        self.assertEqual(body["regime"], "UNKNOWN")

    def test_returns_detail_once_recorded(self):
        self.state.record_regime(
            regime="BULLISH", score=0.42, confidence=0.81,
            risk_posture="NORMAL",
            detail={"trend": "UP", "volatility": "NORMAL",
                    "breadth_proxy": "BROAD_POSITIVE",
                    "reasons": ["SPY above SMA20"], "weights": {"spy_trend": 0.3},
                    "inputs": {"SPY": {"price": 768.67, "provider": "alpaca"}}},
        )
        status, body, _ = self.call("GET", "/agent/market-regime")

        self.assertEqual(status, 200)
        self.assertTrue(body["evaluated"])
        self.assertEqual(body["trend"], "UP")
        self.assertIn("reasons", body)
        self.assertIn("weights", body)

    def test_read_endpoint_spends_no_provider_quota(self):
        """It must serve stored state, not trigger a fetch."""
        calls = {"n": 0}

        def counting_snapshot(symbols):
            calls["n"] += 1
            return {}

        self.provider.get_snapshot = counting_snapshot
        self.call("GET", "/agent/market-regime")
        self.assertEqual(calls["n"], 0)


class TestAdminEvaluateEndpoint(ApiTestBase):
    def test_disabled_by_default(self):
        """The API has no auth, and this endpoint spends market-data quota,
        so a stranger must not be able to drain it."""
        status, body, _ = self.call("POST", "/agent/regime/evaluate")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "disabled")
        self.assertIn("authentication", body["message"])

    def test_enabled_only_by_explicit_flag(self):
        os.environ["AGENT_ADMIN_ENABLED"] = "true"
        status, body, _ = self.call("POST", "/agent/regime/evaluate")
        self.assertEqual(status, 200)
        self.assertTrue(body["evaluated"])
        # No usable data from the fake provider, so it must say UNKNOWN
        # rather than inventing a regime.
        self.assertEqual(body["regime"], "UNKNOWN")
        self.assertEqual(body["confidence"], 0.0)

    def test_falsy_flag_values_do_not_enable_it(self):
        for value in ("", "0", "false", "no", "off", "maybe"):
            with self.subTest(value=value):
                os.environ["AGENT_ADMIN_ENABLED"] = value
                status, _b, _r = self.call("POST", "/agent/regime/evaluate")
                self.assertEqual(status, 403)


class TestErrorHandling(ApiTestBase):
    def test_internal_errors_do_not_leak_details(self):
        def boom():
            raise RuntimeError("secret internal path /var/secret/key")

        api._state_service = boom
        status, body, _ = self.call("GET", "/agent/status")

        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "internal error")
        self.assertNotIn("secret", json.dumps(body))


class TestChatGroundingClassification(unittest.TestCase):
    def test_state_questions_are_recognised(self):
        cases = {
            "Is the market open?": "market_open",
            "What is the market regime?": "regime",
            "Why do you think the market is bullish?": "regime_why",
            "What is the agent doing?": "agent_state",
            "How fresh is your market data?": "freshness",
            "What positions are open?": "positions",
            "How much have we made today?": "pnl",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(classify_question(query), expected)

    def test_unrelated_questions_are_not_claimed(self):
        for query in ("Explain diversification", "What is an ETF?",
                      "Should I buy AAPL?", ""):
            with self.subTest(query=query):
                self.assertIsNone(classify_question(query))


class TestChatGroundingAnswers(unittest.TestCase):
    def setUp(self):
        store = InMemoryStateStore()
        self.svc = AgentStateService(store, AgentConfig(),
                                     clock=lambda: "2026-09-30")
        self.svc.set_market_session(MarketSession.OPEN)
        self.svc.record_regime(
            regime="MIXED", score=0.213, confidence=0.219,
            risk_posture="CAUTIOUS",
            detail={
                "trend": "UP", "volatility": "NORMAL",
                "breadth_proxy": "NARROW_LARGE_CAP",
                "reasons": ["instruments disagree sharply (dispersion 1.56)"],
                "data_quality": {"instruments_requested": 3, "fresh": 3,
                                 "stale": 0, "missing": 0},
                "inputs": {"SPY": {"price": 768.67, "provider": "alpaca",
                                   "age_seconds": 0.5,
                                   "as_of": "2026-09-30T16:01:49Z"}},
            },
        )
        self.session = self.svc.get_session().as_dict()

    def test_regime_answer_includes_the_confidence_caveat(self):
        answer = answer_from_state("What is the market regime?", self.session)
        self.assertIn("MIXED", answer)
        self.assertIn("not a probability", answer.lower())

    def test_why_answer_uses_recorded_reasons(self):
        answer = answer_from_state("Why is the market mixed?", self.session)
        self.assertIn("disagree sharply", answer)

    def test_market_open_answer(self):
        answer = answer_from_state("Is the market open?", self.session)
        self.assertIn("open for regular trading", answer)

    def test_freshness_answer_reports_provider_and_age(self):
        answer = answer_from_state("How fresh is your market data?",
                                   self.session)
        self.assertIn("SPY", answer)
        self.assertIn("alpaca", answer)

    def test_pnl_answer_explains_why_it_is_zero(self):
        answer = answer_from_state("How much have we made today?",
                                   self.session)
        self.assertIn("cannot place orders", answer)

    def test_trading_answer_denies_execution_capability(self):
        answer = answer_from_state("Are you actually trading real money?",
                                   self.session)
        self.assertIn("no order execution path", answer)

    def test_no_state_yields_an_explicit_unavailable(self):
        """The rule this module exists for: never invent state."""
        for query in ("What is the market regime?", "Is the market open?",
                      "What is the agent doing?"):
            with self.subTest(query=query):
                self.assertEqual(answer_from_state(query, None), UNAVAILABLE)

    def test_unknown_regime_is_not_dressed_up_as_a_direction(self):
        svc = AgentStateService(InMemoryStateStore(), AgentConfig(),
                                clock=lambda: "2026-09-30")
        answer = answer_from_state("What is the market regime?",
                                   svc.get_session().as_dict())
        self.assertIn("UNKNOWN", answer)
        self.assertIn("won't guess", answer)

    def test_non_state_questions_are_passed_through(self):
        self.assertIsNone(
            answer_from_state("Explain dollar cost averaging", self.session)
        )


class TestChatGroundingContext(unittest.TestCase):
    def test_context_forbids_invention(self):
        ctx = build_context(None)
        self.assertIn("unavailable", ctx)
        self.assertIn("Do not guess", ctx)

    def test_context_carries_state_and_rules(self):
        svc = AgentStateService(InMemoryStateStore(), AgentConfig(),
                                clock=lambda: "2026-09-30")
        svc.record_regime("BULLISH", 0.4, 0.8, "NORMAL",
                          detail={"trend": "UP", "reasons": ["SPY strong"]})
        ctx = build_context(svc.get_session().as_dict())

        self.assertIn("market_regime: BULLISH", ctx)
        self.assertIn("NOT a probability", ctx)
        self.assertIn("state only what appears above", ctx)
        self.assertIn("Never describe this system as placing trades", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
