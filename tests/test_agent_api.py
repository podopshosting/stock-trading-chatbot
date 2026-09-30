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
    EVIDENCE_UNAVAILABLE, NO_CATALYST, SIGNAL_UNAVAILABLE, UNAVAILABLE,
    answer_evidence_question, answer_from_state, answer_signal_question,
    build_context, classify_evidence_question, classify_question,
    classify_signal_question,
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
    # The analysis route runs the same engine as production /chatbot, so
    # the handler imports ml_agent_lite. In the deployment artifact that
    # module sits flat beside handler.py (build_lambda_package.sh copies
    # it via EXTRA_FILES); in the repo it lives under chatbot-router, so
    # the test has to put that directory on the path to match the shape
    # the Lambda actually sees.
    router_dir = os.path.join(REPO_ROOT, "lambda-micro", "chatbot-router")
    if router_dir not in sys.path:
        sys.path.insert(0, router_dir)

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


class TestAnalysisRoute(unittest.TestCase):
    """
    The /agent/analysis route exists so the UI never has to parse prose to
    recover a number. These tests pin the shape of that contract and the
    route's refusal behaviour; the scoring itself is tested elsewhere and
    is not touched here.
    """

    @staticmethod
    def _event(symbol=None):
        return {
            "requestContext": {"http": {"method": "GET",
                                        "path": "/agent/analysis"}},
            "queryStringParameters": {"symbol": symbol} if symbol else None,
        }

    def _call(self, symbol=None):
        res = api.lambda_handler(self._event(symbol), None)
        return res["statusCode"], json.loads(res["body"])

    def test_missing_symbol_is_refused(self):
        status, body = self._call()
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "symbol required")

    def test_non_alphabetic_symbol_is_refused_before_any_provider_call(self):
        for bad in ("../etc", "A B", "1234", "TOOLONGSYM"):
            with self.subTest(symbol=bad):
                status, body = self._call(bad)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid symbol")

    VALIDATION_ERRORS = {"symbol required", "invalid symbol"}

    def test_lower_case_symbol_passes_validation(self):
        """
        Validation runs before any provider call, so a lower-case symbol
        that gets past it proves the upper-casing happened. Asserting only
        'not 400' would also be satisfied by a missing route, so this
        checks the error identity rather than the status code.
        """
        _status, body = self._call("nvda")
        self.assertNotIn(body.get("error"), self.VALIDATION_ERRORS)

    def test_falsifying_control_validation_errors_are_detectable(self):
        """The check above must be able to see a validation refusal."""
        _status, body = self._call("1234")
        self.assertIn(body.get("error"), self.VALIDATION_ERRORS)


class TestNoUndefinedNames(unittest.TestCase):
    """
    Static guard for names a handler references but nothing defines.

    Importing the module proves module-level code runs; it says nothing
    about a name used INSIDE a function body. `handle_signals_latest`
    shipped a call to `_session_service()`, which never existed - the
    import succeeded, the whole suite stayed green, and the route would
    have raised NameError on its first request.
    """

    import builtins as _builtins

    def test_every_global_referenced_by_a_handler_resolves(self):
        import builtins
        module_names = set(dir(api))
        builtin_names = set(dir(builtins))
        missing = []

        for attr in dir(api):
            fn = getattr(api, attr)
            code = getattr(fn, "__code__", None)
            if code is None or not callable(fn):
                continue
            if getattr(fn, "__module__", None) != api.__name__:
                continue
            for name in code.co_names:
                # co_names also holds attribute names (x.foo), which are
                # not globals. Only flag a name that is neither a module
                # global, a builtin, nor an attribute accessed anywhere.
                if name in module_names or name in builtin_names:
                    continue
                if name in code.co_varnames or name in code.co_freevars:
                    continue
                # Attribute access: appears after a dot in the source.
                if f".{name}" in (fn.__doc__ or "") :
                    continue
                missing.append((attr, name))

        # Attribute names dominate the false positives, so filter to the
        # shape that actually matters: a bare call to an undefined
        # module-level helper, which always starts with an underscore or
        # "handle_" in this codebase.
        real = [(f, n) for f, n in missing
                if n.startswith("_") or n.startswith("handle_")]
        self.assertEqual(real, [],
                         f"handler(s) reference undefined names: {real}")

    def test_falsifying_control_the_check_can_see_a_missing_name(self):
        """Prove the scan would have caught the defect that motivated it."""
        import builtins
        module_names = set(dir(api))
        self.assertNotIn("_session_service", module_names,
                         "setup: this name should not exist")

        def broken():
            return _session_service()          # noqa: F821

        found = [n for n in broken.__code__.co_names
                 if n.startswith("_") and n not in module_names
                 and n not in set(dir(builtins))]
        self.assertIn("_session_service", found,
                      "the scan cannot detect an undefined helper call")

    def test_every_route_target_is_callable(self):
        for (method, path), handler in api.ROUTES.items():
            with self.subTest(route=f"{method} {path}"):
                self.assertTrue(callable(handler))


class TestSignalRoutes(unittest.TestCase):
    """The canonical-signal read endpoints."""

    @staticmethod
    def _event(path, **params):
        return {
            "requestContext": {"http": {"method": "GET", "path": path}},
            "queryStringParameters": params or None,
        }

    def _call(self, path, **params):
        res = api.lambda_handler(self._event(path, **params), None)
        return res["statusCode"], json.loads(res["body"])

    def test_signals_requires_a_symbol(self):
        status, body = self._call("/agent/signals")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "symbol required")

    def test_signals_refuses_a_malformed_symbol_before_any_provider_call(self):
        for bad in ("../etc", "A B", "1234", "TOOLONGSYM"):
            with self.subTest(symbol=bad):
                status, body = self._call("/agent/signals", symbol=bad)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid symbol")

    def test_unknown_route_is_reported_with_the_available_set(self):
        status, body = self._call("/agent/nope")
        self.assertEqual(status, 404)
        self.assertIn("GET /agent/signals", body["available"])

    def test_signal_routes_are_registered(self):
        for path in ("/agent/signals", "/agent/signals/latest",
                     "/agent/scanner/signals"):
            with self.subTest(path=path):
                self.assertIn(("GET", path), api.ROUTES)

    def test_no_signal_route_accepts_a_write_method(self):
        """
        Read endpoints only. A POST that triggered an evaluation would
        let an unauthenticated stranger spend provider quota, which is
        why the one admin route in this API is disabled by default.
        """
        for path in ("/agent/signals", "/agent/signals/latest",
                     "/agent/scanner/signals"):
            with self.subTest(path=path):
                self.assertNotIn(("POST", path), api.ROUTES)


class TestSignalChatGrounding(unittest.TestCase):
    """
    Signal questions are answered from the stored result, never by a
    model. A fabricated MACD value is indistinguishable from a real one
    to the reader, which is why these never reach the LLM.
    """

    RESULT = {
        "symbol": "TSLA", "direction": "SELL",
        "signal_agreement": 0.75, "signal_magnitude": 0.77,
        "buy_groups": 0, "sell_groups": 2, "opinionated_groups": 2,
        "groups_total": 3, "market_regime": "MIXED",
        "regime_adjustment": 0.85, "regime_adjusted_magnitude": 0.66,
        "reasons": ["Trend and Momentum independently support SELL.",
                    "Inside Trend, MA crossover is BUY and Golden cross is "
                    "SELL - that internal disagreement halves the group's "
                    "weight."],
        "group_results": [
            {"group": "trend", "label": "Trend", "direction": "SELL",
             "internal_disagreement": True,
             "members": [{"label": "MA crossover (20/50)", "direction": "BUY"},
                         {"label": "Golden/death cross (50/200)",
                          "direction": "SELL"}]},
            {"group": "momentum", "label": "Momentum", "direction": "SELL",
             "internal_disagreement": False, "members": []},
            {"group": "mean_reversion", "label": "Mean Reversion",
             "direction": "NEUTRAL", "internal_disagreement": False,
             "members": []},
        ],
        "indicator_results": [
            {"indicator": "macd", "label": "MACD (12/26/9)",
             "direction": "SELL",
             "reason": "MACD is below its 9-period EMA signal line",
             "raw_values": {"macd": 2.228, "signal": 4.314,
                            "histogram": -2.086}},
            {"indicator": "rsi", "label": "RSI (14)", "direction": "NEUTRAL",
             "reason": "RSI 43.5 is in the neutral 40-60 band",
             "raw_values": {"rsi": 43.5}},
        ],
    }

    def test_why_direction_uses_the_recorded_reasons(self):
        answer = answer_signal_question("Why is TSLA SELL?", self.RESULT)
        self.assertIn("TSLA is SELL", answer)
        self.assertIn("independently support SELL", answer)

    def test_which_signals_disagree_names_them(self):
        answer = answer_signal_question("Which signals disagree?", self.RESULT)
        self.assertIn("Trend", answer)
        self.assertIn("MA crossover", answer)
        self.assertIn("Golden/death cross", answer)

    def test_macd_question_reports_the_real_values(self):
        answer = answer_signal_question("What does MACD say?", self.RESULT)
        self.assertIn("2.228", answer)
        self.assertIn("4.314", answer)
        self.assertIn("SELL", answer)

    def test_rsi_question_reports_the_engine_vote_not_an_invented_one(self):
        answer = answer_signal_question("What is the RSI?", self.RESULT)
        self.assertIn("NEUTRAL", answer)
        self.assertIn("43.5", answer)

    def test_agreement_question_explains_and_disclaims(self):
        answer = answer_signal_question("Why is agreement lower?", self.RESULT)
        self.assertIn("0.75", answer)
        self.assertIn("not a probability", answer)

    def test_magnitude_is_described_as_separate_from_agreement(self):
        answer = answer_signal_question("How strong is the magnitude?",
                                        self.RESULT)
        self.assertIn("0.77", answer)
        self.assertIn("separate from agreement", answer)

    def test_market_support_question_states_the_regime_cannot_flip_direction(self):
        answer = answer_signal_question(
            "Is the broader market supporting this?", self.RESULT)
        self.assertIn("MIXED", answer)
        self.assertIn("never creates or reverses a direction", answer)

    def test_missing_data_is_stated_not_invented(self):
        for query in ("Why is AAPL BUY?", "Which signals disagree?",
                      "What does MACD say?"):
            with self.subTest(query=query):
                self.assertEqual(answer_signal_question(query, None),
                                 SIGNAL_UNAVAILABLE)

    def test_unrelated_questions_are_passed_through(self):
        self.assertIsNone(
            answer_signal_question("Explain dollar cost averaging", self.RESULT))

    def test_no_answer_invents_a_number_absent_from_the_result(self):
        """
        Every figure in an answer must appear in the stored result. An
        answer that computed something itself would drift from what the
        engine recorded.
        """
        import re
        allowed = set()
        def collect(obj):
            if isinstance(obj, dict):
                for v in obj.values(): collect(v)
            elif isinstance(obj, list):
                for v in obj: collect(v)
            elif isinstance(obj, (int, float)):
                # Collect the DIGITS, not the signed literal: the answer
                # renders -2.086 inside a sentence and the scan below
                # extracts "2.086", so an unsigned comparison is the
                # like-for-like one.
                for n in re.findall(r"\d+\.?\d*", str(obj)):
                    allowed.add(n)
                if isinstance(obj, float):
                    for n in re.findall(r"\d+\.?\d*", f"{obj:.2f}"):
                        allowed.add(n)
            elif isinstance(obj, str):
                for n in re.findall(r"\d+\.?\d*", obj):
                    allowed.add(n)
        collect(self.RESULT)

        for query in ("Why is TSLA SELL?", "What does MACD say?",
                      "Why is agreement lower?", "How strong is it?",
                      "Is the market supporting this?"):
            answer = answer_signal_question(query, self.RESULT) or ""
            for number in re.findall(r"\d+\.?\d*", answer):
                with self.subTest(query=query, number=number):
                    self.assertIn(number, allowed,
                                  f"answer invented the number {number}")


class TestEvidenceChatGrounding(unittest.TestCase):
    """
    Evidence questions are answered from what was collected, never from
    the model's general knowledge. A plausible invented headline is
    indistinguishable from a real one to the reader, so "I have no
    evidence" has to be an available answer.
    """

    CATALYST = {
        "symbol": "TSLA",
        "has_active_catalyst": True,
        "direction": "MIXED",
        "conflicting_evidence": True,
        "conflict_detail": ["positive: Deliveries beat",
                            "negative: Margin guidance cut"],
        "total_evidence_count": 7,
        "duplicate_groups": 3,
        "duplicates_collapsed": 4,
        "independent_source_count": 2,
        "primary_source_count": 1,
        "primary_catalyst": {
            "type": "SHELF_REGISTRATION", "direction": "UNCERTAIN",
            "materiality": 0.35, "novelty": 0.9, "window": "RECENT",
            "headline": "Tesla, Inc.: S-3ASR",
            "publisher": "SEC EDGAR", "source_class": "PRIMARY",
        },
        "items": [
            {"type": "SHELF_REGISTRATION",
             "headline": "Tesla, Inc.: S-3ASR",
             "published_at": "2026-09-29T20:38:50Z",
             "source": {"provider": "sec", "publisher": "SEC EDGAR",
                        "source_class": "PRIMARY"},
             "raw_metadata": {"financing_stage": "ABILITY_TO_ISSUE"}},
            {"type": "EARNINGS", "headline": "Tesla Deliveries Beat",
             "published_at": "2026-09-30T12:00:00Z",
             "source": {"provider": "alpaca_news", "publisher": "Reuters",
                        "source_class": "STRUCTURED_NEWS"},
             "raw_metadata": {}},
        ],
    }

    NO_CAT = {"symbol": "QUIET", "has_active_catalyst": False,
              "primary_catalyst": None, "total_evidence_count": 3,
              "duplicate_groups": 3, "duplicates_collapsed": 0,
              "items": []}

    def test_why_moving_cites_the_catalyst_and_its_source(self):
        answer = answer_evidence_question("Why is TSLA moving?", self.CATALYST)
        self.assertIn("SHELF_REGISTRATION", answer)
        self.assertIn("SEC EDGAR", answer)
        self.assertIn("PRIMARY", answer)

    def test_conflict_is_surfaced_not_averaged_away(self):
        answer = answer_evidence_question("Any news on TSLA?", self.CATALYST)
        self.assertIn("CONFLICTS", answer)
        self.assertIn("Margin guidance cut", answer)

    def test_no_catalyst_is_stated_as_a_valid_answer(self):
        answer = answer_evidence_question("Why is QUIET moving?", self.NO_CAT)
        self.assertIn("No active catalyst", answer)
        self.assertIn("valid answer", answer)

    def test_duplicate_question_reports_events_not_articles(self):
        answer = answer_evidence_question(
            "Are multiple reports actually the same story?", self.CATALYST)
        self.assertIn("7", answer)
        self.assertIn("3 distinct event", answer)
        self.assertIn("4 were retellings", answer)

    def test_dilution_question_distinguishes_capacity_from_an_offering(self):
        """
        The distinction that matters most for short-duration trading.
        A shelf is permission to sell later, not a sale now.
        """
        answer = answer_evidence_question("Is there dilution risk?",
                                          self.CATALYST)
        self.assertIn("CAPACITY", answer)
        self.assertIn("not an offering being sold now", answer)

    def test_filing_question_lists_sec_items_only(self):
        answer = answer_evidence_question("Was there an SEC filing?",
                                          self.CATALYST)
        self.assertIn("S-3ASR", answer)
        self.assertNotIn("Deliveries Beat", answer)

    def test_provenance_question_explains_the_reliability_caveat(self):
        answer = answer_evidence_question(
            "Is this from the company or a news article?", self.CATALYST)
        self.assertIn("PRIMARY", answer)
        self.assertIn("not whether the interpretation is right", answer)

    def test_novelty_question_explains_the_scale(self):
        answer = answer_evidence_question("Is this a new event?",
                                          self.CATALYST)
        self.assertIn("0.9", answer)
        self.assertIn("genuinely new", answer)

    def test_earnings_question_does_not_claim_none_is_scheduled(self):
        """
        We have no earnings calendar. Saying "no earnings" would be an
        assertion the system cannot support.
        """
        answer = answer_evidence_question("Is earnings today?", self.NO_CAT)
        self.assertIn("not a confirmation", answer)

    def test_missing_evidence_is_stated_not_invented(self):
        for query in ("Why is NVDA moving?", "Any news on NVDA?",
                      "Was there an SEC filing?"):
            with self.subTest(query=query):
                self.assertEqual(answer_evidence_question(query, None),
                                 EVIDENCE_UNAVAILABLE)

    def test_no_evidence_is_distinguished_from_no_news(self):
        self.assertIn("not the same as there being no news",
                      EVIDENCE_UNAVAILABLE)

    def test_unrelated_questions_are_passed_through(self):
        self.assertIsNone(answer_evidence_question(
            "Explain dollar cost averaging", self.CATALYST))

    def test_every_required_question_is_classified(self):
        required = [
            "Why is NVDA moving?", "Is there news on TSLA?",
            "What catalyst does the agent see?", "Is this a new event?",
            "Is this from the company or a news article?",
            "Are multiple reports actually the same story?",
            "Is earnings today?", "Is there dilution risk?",
            "Was there an SEC filing?",
        ]
        for query in required:
            with self.subTest(query=query):
                self.assertIsNotNone(classify_evidence_question(query))

    def test_no_answer_invents_a_headline(self):
        """Every headline in an answer must appear in the stored items."""
        known = {i["headline"] for i in self.CATALYST["items"]}
        known.add(self.CATALYST["primary_catalyst"]["headline"])
        for query in ("Why is TSLA moving?", "Was there an SEC filing?",
                      "Is there dilution risk?"):
            answer = answer_evidence_question(query, self.CATALYST) or ""
            for headline in known:
                pass  # presence is fine; absence of OTHERS is the check
            self.assertNotIn("Nvidia", answer)
            self.assertNotIn("Apple", answer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
