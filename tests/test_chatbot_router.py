"""
Behavioural tests for the chatbot router Lambda.

These assert on response *structure and behaviour*, never on frozen market
prices or dates, so they stay valid as the market moves.

`requests` is stubbed, so the suite runs with no third-party dependencies and
never spends Alpha Vantage quota.
"""
import json
import os
import sys
import types
import unittest

ROUTER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lambda-micro", "chatbot-router",
)


# --- stub `requests` before importing the handler -------------------------
class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequests:
    """Records calls and replays queued Alpha Vantage payloads per function."""

    def __init__(self):
        self.calls = []
        self.queues = {}

    def queue(self, function_name, *payloads):
        self.queues.setdefault(function_name, []).extend(payloads)

    def get(self, url, params=None, timeout=None):
        params = params or {}
        fn = params.get("function")
        self.calls.append(fn)
        queue = self.queues.get(fn) or []
        payload = queue.pop(0) if queue else {}
        return _FakeResponse(payload)

    def post(self, url, headers=None, json=None, timeout=None):  # OpenAI
        self.calls.append("openai")
        return _FakeResponse(
            {"choices": [{"message": {"content": "Stubbed advisory text."}}]}
        )


_fake_requests = _FakeRequests()
sys.modules.setdefault("requests", types.SimpleNamespace())
sys.modules["requests"] = _fake_requests
if ROUTER_DIR not in sys.path:
    sys.path.insert(0, ROUTER_DIR)

import handler  # noqa: E402  (import after stubbing)

handler.requests = _fake_requests


THROTTLE = {
    "Information": (
        "Thank you for using Alpha Vantage! Please consider spreading out your "
        "free API requests more sparingly (1 request per second). You may "
        "subscribe to any of the premium plans ... rate limit (25 requests per day)"
    )
}


def quote_payload(price="100.0000"):
    return {
        "Global Quote": {
            "01. symbol": "AAPL",
            "05. price": price,
            "06. volume": "1000000",
            "09. change": "1.0000",
            "10. change percent": "1.0000%",
        }
    }


def daily_payload(days=100):
    """Ascending price series long enough for the ML agent (needs >= 50)."""
    series = {}
    for i in range(days):
        series[f"2026-01-{i + 1:03d}"] = {"4. close": f"{100 + i}.0000"}
    return {"Time Series (Daily)": series}


def body_of(result):
    return json.loads(result["body"])


class RouterTestBase(unittest.TestCase):
    def setUp(self):
        _fake_requests.calls = []
        _fake_requests.queues = {}
        # Never touch Secrets Manager or the real clock in tests.
        handler.get_secret = lambda name: "test-key"
        if hasattr(handler, "time"):
            handler.time.sleep = lambda _s: None
        self._reset_rate_limiter()

    def _reset_rate_limiter(self):
        if hasattr(handler, "_av_last_call"):
            handler._av_last_call = 0.0


class TestThrottleHandling(RouterTestBase):
    """Alpha Vantage free tier allows ~1 request/second.

    The handler issues several calls per query, so back-to-back calls get
    throttled. A throttle is a transient condition and must not be reported
    to the user as missing data or as an invalid ticker.
    """

    def test_throttled_history_recovers_and_ml_runs(self):
        _fake_requests.queue("GLOBAL_QUOTE", quote_payload())
        # First history attempt throttled, retry succeeds.
        _fake_requests.queue("TIME_SERIES_DAILY", THROTTLE, daily_payload())

        result = handler.lambda_handler(
            {"query": "What do you think about AAPL?"}, None
        )
        body = body_of(result)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["symbol"], "AAPL")
        self.assertIn(
            "ml_confidence", body["data"],
            msg="ML analysis must run after a throttled history call is retried",
        )
        self.assertNotIn("requires more historical data", body["response"])

    def test_throttled_quote_is_not_reported_as_invalid_symbol(self):
        # Every quote attempt throttled; no data at all.
        _fake_requests.queue("GLOBAL_QUOTE", THROTTLE, THROTTLE, THROTTLE, THROTTLE)

        result = handler.lambda_handler({"query": "How is AAPL doing?"}, None)
        body = body_of(result)

        self.assertEqual(result["statusCode"], 200)
        self.assertNotIn(
            "Invalid stock symbol", body["response"],
            msg="A rate limit must never be presented as an invalid ticker",
        )
        self.assertIn("temporarily unavailable", body["response"].lower())

    def test_persistent_history_throttle_reports_data_unavailable(self):
        _fake_requests.queue("GLOBAL_QUOTE", quote_payload())
        _fake_requests.queue(
            "TIME_SERIES_DAILY", THROTTLE, THROTTLE, THROTTLE, THROTTLE
        )

        body = body_of(
            handler.lambda_handler({"query": "Analyze AAPL"}, None)
        )

        # Live quote is still real data and may be shown.
        self.assertEqual(body["data"]["price"], 100.0)
        # But the ML gap must be explained honestly, not blamed on the stock.
        self.assertNotIn("try another stock", body["response"].lower())
        self.assertIn("temporarily unavailable", body["response"].lower())


class TestQuotaEfficiency(RouterTestBase):
    def test_no_redundant_alpha_vantage_calls(self):
        """Free tier is 25 requests/day. Every wasted call costs a user query."""
        _fake_requests.queue("GLOBAL_QUOTE", quote_payload())
        _fake_requests.queue("TIME_SERIES_DAILY", daily_payload())

        handler.lambda_handler({"query": "AAPL analysis"}, None)

        av_calls = [c for c in _fake_requests.calls if c and c != "openai"]
        self.assertEqual(
            sorted(av_calls), ["GLOBAL_QUOTE", "TIME_SERIES_DAILY"],
            msg=f"Unexpected Alpha Vantage calls: {av_calls}",
        )


class TestCoreBehaviour(RouterTestBase):
    def test_general_query_makes_no_market_data_calls(self):
        body = body_of(
            handler.lambda_handler({"query": "Explain diversification"}, None)
        )
        self.assertIn("response", body)
        self.assertNotIn("symbol", body)
        self.assertEqual(
            [c for c in _fake_requests.calls if c != "openai"], [],
            msg="General questions must not spend market-data quota",
        )

    def test_general_prompt_forbids_fabricating_security_data(self):
        """The general path has no market data, so the model must be told not
        to invent any. Without this guard an unrecognised token such as a
        6-letter ticker produces confident, fabricated 'analysis'."""
        captured = {}
        original_post = _fake_requests.post

        def spy(url, headers=None, json=None, timeout=None):
            captured['payload'] = json
            return original_post(url, headers=headers, json=json, timeout=timeout)

        _fake_requests.post = spy
        try:
            handler.lambda_handler({"query": "What about ZZZZQQ?"}, None)
        finally:
            _fake_requests.post = original_post

        sent = json.dumps(captured.get('payload', {})).lower()
        self.assertTrue(
            "do not" in sent and (
                "price" in sent or "market data" in sent or "fabricat" in sent
            ),
            msg="General-query prompt must forbid inventing security specifics",
        )

    def test_invalid_symbol_is_handled_without_crashing(self):
        # Empty payload = symbol genuinely not found (not a throttle).
        _fake_requests.queue("GLOBAL_QUOTE", {"Global Quote": {}})

        result = handler.lambda_handler({"query": "What about ZZZZQQ?"}, None)
        body = body_of(result)

        self.assertEqual(result["statusCode"], 200)
        self.assertIn("response", body)

    def test_missing_query_returns_400(self):
        result = handler.lambda_handler({}, None)
        self.assertEqual(result["statusCode"], 400)

    def test_api_gateway_string_body_is_parsed(self):
        _fake_requests.queue("GLOBAL_QUOTE", quote_payload())
        _fake_requests.queue("TIME_SERIES_DAILY", daily_payload())

        result = handler.lambda_handler(
            {"body": json.dumps({"query": "AAPL"})}, None
        )
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body_of(result)["symbol"], "AAPL")

    def test_cors_header_on_every_response(self):
        for event in ({}, {"query": "Explain index funds"}):
            result = handler.lambda_handler(event, None)
            self.assertEqual(
                result["headers"].get("Access-Control-Allow-Origin"), "*"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
