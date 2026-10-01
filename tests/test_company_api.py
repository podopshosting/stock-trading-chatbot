import json, os, subprocess, sys, unittest
from unittest import mock
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO); sys.path.insert(0, os.path.join(REPO, "tests"))
from test_agent_dashboard import handler as api                      # noqa
from test_company_service import svc, FakeActions, QUARTERLY, FakeFacts  # noqa


def call(path, method="GET", service=None):
    service = service or svc(FakeActions(QUARTERLY))[0]
    path, _, query = path.partition("?")
    # A real Function URL event carries DECODED query values; building
    # them encoded hid a ticker behind "%20GIS" with no word boundary.
    from urllib.parse import unquote_plus
    params = ({k: unquote_plus(v) for k, v in
               (p.split("=", 1) for p in query.split("&"))}
              if query else None)
    with mock.patch.object(api, "_company_service", lambda: service):
        r = api.lambda_handler({"requestContext": {"http": {
            "method": method, "path": path}},
            "queryStringParameters": params}, None)
    return r["statusCode"], json.loads(r["body"])


class TestCompanyEndpoints(unittest.TestCase):
    def test_every_section_answers(self):
        for sec in ("", "/dividends", "/splits", "/corporate-actions",
                    "/earnings", "/fundamentals", "/peers",
                    "/peer-comparison", "/holding-context"):
            with self.subTest(section=sec):
                code, body = call(f"/agent/company/GIS{sec}")
                self.assertEqual(code, 200, body)
                self.assertEqual(body["execution"]["real_money"], "DISABLED")

    def test_dividend_answer(self):
        _c, b = call("/agent/company/GIS/dividends")
        self.assertEqual(b["dividend_stock"], "YES")

    def test_writes_are_refused(self):
        for m in ("POST", "PUT", "DELETE"):
            with self.subTest(method=m):
                self.assertEqual(call("/agent/company/GIS/dividends", m)[0], 405)

    def test_unknown_section_and_bad_symbol(self):
        self.assertEqual(call("/agent/company/GIS/trade")[0], 404)
        self.assertEqual(call("/agent/company/GI$")[0], 400)
        self.assertEqual(call("/agent/company/")[0], 404)

    def test_still_exactly_one_post_route(self):
        posts = [k for k in api.ROUTES if k[0] != "GET"]
        self.assertEqual(posts, [("POST", "/agent/regime/evaluate")])

    def test_provider_error_is_a_502_not_a_fabricated_answer(self):
        from agent.providers.base import DataUnavailable
        class Boom:
            def dividends(self, s): raise DataUnavailable("down")
        code, body = call("/agent/company/GIS/dividends", service=Boom())
        self.assertEqual(code, 502)


class TestIsolationFromTheTradingPath(unittest.TestCase):
    """Company intelligence must not be importable by anything that runs
    the paper-trading cycle."""

    def _loaded(self, handler_dir):
        code = (
            "import sys, importlib.util;"
            f"sys.path.insert(0, {REPO!r});"
            f"sys.path.insert(0, {os.path.join(REPO, 'lambda-micro', 'chatbot-router')!r});"
            f"spec = importlib.util.spec_from_file_location('h', {os.path.join(REPO, 'lambda-micro', handler_dir, 'handler.py')!r});"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
            "print('BAD:' + ','.join(sorted(k for k in sys.modules if k.startswith('agent.company'))))")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                             capture_output=True, text=True, timeout=60)
        line = [l for l in out.stdout.splitlines() if l.startswith("BAD:")]
        self.assertTrue(line, out.stderr[-400:])
        return line[-1]

    def test_the_cycle_handler_loads_no_company_code(self):
        self.assertEqual(self._loaded("agent-cycle"), "BAD:")

    def test_the_scanner_handler_loads_no_company_code(self):
        self.assertEqual(self._loaded("agent-scanner"), "BAD:")

    def test_falsifying_control_the_check_sees_company_code_in_the_api(self):
        self.assertIn("agent.company", self._loaded("agent-api"))


if __name__ == "__main__":
    unittest.main()


class TestRefreshIsStillReadOnly(unittest.TestCase):
    """`?refresh=1` re-reads the provider. It must not become a write
    surface, and it must only reach sections that accept it."""

    def test_refresh_reaches_the_provider_again(self):
        from test_company_service import FakeActions, QUARTERLY, svc
        acts = FakeActions(QUARTERLY)
        service, _store = svc(acts)
        code, _b = call("/agent/company/GIS/dividends", service=service)
        self.assertEqual(code, 200)
        first = acts.calls
        call("/agent/company/GIS/dividends", service=service)
        self.assertEqual(acts.calls, first)            # cached
        call("/agent/company/GIS/dividends?refresh=1", service=service)
        self.assertEqual(acts.calls, first + 1)

    def test_refresh_on_a_section_that_does_not_take_it_is_harmless(self):
        code, body = call("/agent/company/GIS/fundamentals?refresh=1")
        self.assertEqual(code, 200)
        self.assertEqual(body["execution"]["real_money"], "DISABLED")

    def test_refresh_does_not_turn_a_write_method_into_a_route(self):
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(
                    call("/agent/company/GIS/dividends?refresh=1", method)[0],
                    405)


class TestAskAnswersCompanyQuestionsToo(unittest.TestCase):
    """`/agent/ask` routes a company question to company intelligence and
    an operational one to the session records. Still one POST, still no
    language model."""

    def ask(self, question, service=None):
        from urllib.parse import quote
        code, body = call(f"/agent/ask?q={quote(question)}", service=service)
        self.assertEqual(code, 200, body)
        return body

    def test_a_company_question_is_answered_from_company_records(self):
        out = self.ask("Does GIS pay a dividend?")
        self.assertEqual(out["intent"], "DIVIDEND")
        self.assertIn("dividends", out["sources"])
        self.assertFalse(out["llm_used"])

    def test_an_operational_question_still_reaches_the_session_records(self):
        out = self.ask("Are you healthy?")
        self.assertEqual(out["intent"], "HEALTH")

    def test_a_split_question_is_company_not_operational(self):
        self.assertEqual(self.ask("Has GIS split before?")["intent"], "SPLITS")

    def test_an_unknown_question_is_still_declined(self):
        out = self.ask("tell me a joke")
        self.assertFalse(out["grounded"])

    def test_ask_is_still_a_GET_and_requires_a_question(self):
        self.assertEqual(call("/agent/ask")[0], 400)
