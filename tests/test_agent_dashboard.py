"""
The agent dashboard and its read API.

Two things this suite protects.

First, that the API cannot place an order. Not "does not by default" -
cannot, because no code path exists. The switches are read and reported,
but there is no broker adapter imported anywhere in the Lambda, so
flipping every environment variable to true would still not produce an
order.

Second, that the dashboard does not hide the pipeline behind a single
number. Each stage is shown separately so a reader can see WHICH stage
drove an outcome. A blended score would make a strong signal with no
catalyst look identical to a mediocre signal with a good one, and would
make the whole system impossible to distrust selectively.
"""
import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _load_agent_api():
    """Load the agent API handler under a DISTINCT module name.

    Both Lambdas have a module called `handler`, and sys.modules caches
    the first one imported - so putting either directory on sys.path and
    doing `import handler` makes whichever test runs first win, and the
    other silently tests the wrong module. That failure only appears
    when the whole suite runs together, which is exactly how it slipped
    through here: this file originally did the naive import and broke
    nine production chatbot-router tests.

    test_agent_api.py already solved this the same way, so the module
    name is shared and the module is loaded at most once.
    """
    import importlib.util
    if "agent_api_handler" in sys.modules:
        return sys.modules["agent_api_handler"]

    # The handler imports ml_agent_lite, which sits flat beside
    # handler.py in the deployment artifact but under chatbot-router in
    # the repo.
    router_dir = os.path.join(REPO_ROOT, "lambda-micro", "chatbot-router")
    if router_dir not in sys.path:
        sys.path.insert(0, router_dir)

    path = os.path.join(REPO_ROOT, "lambda-micro", "agent-api", "handler.py")
    spec = importlib.util.spec_from_file_location("agent_api_handler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_api_handler"] = module
    spec.loader.exec_module(module)
    return module


handler = _load_agent_api()                                       # noqa: E402

DASHBOARD = os.path.join(REPO_ROOT, "web", "agent", "index.html")
HANDLER_SRC = os.path.join(REPO_ROOT, "lambda-micro", "agent-api",
                           "handler.py")


def call(path, method="GET", **query):
    event = {"requestContext": {"http": {"method": method, "path": path}},
             "queryStringParameters": query or None}
    response = handler.lambda_handler(event, None)
    return response["statusCode"], json.loads(response["body"] or "{}")


class TestNoExecutionPath(unittest.TestCase):
    """The strongest guarantee available: the capability is absent."""

    def test_the_api_reports_that_it_cannot_place_orders(self):
        code, body = call("/agent/switches")
        self.assertEqual(code, 200)
        self.assertFalse(body["can_place_orders"])
        self.assertTrue(body["is_paper_only"])

    def test_no_broker_adapter_is_imported(self):
        """
        Not a convention - an absence. If nothing that can reach a
        broker is imported, no configuration change can make this
        Lambda trade.
        """
        source = open(HANDLER_SRC).read()
        # Import statements and call sites, not bare substrings. A
        # looser scan matched "place_order" inside the field name
        # "can_place_orders": False - a DENIAL of the capability being
        # reported as evidence of it. Same trap as scanning prose for
        # claims without stripping denials first.
        for forbidden in ("from agent.broker", "import agent.broker",
                          "PaperBroker(", "submit_approved(",
                          ".submit_order(", ".place_order("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_falsifying_control_the_call_site_scan_is_not_vacuous(self):
        """
        The patterns above end in "(" so they match calls rather than
        mentions. Prove that form still matches something real, or the
        scan could pass by being unmatchable.
        """
        source = open(HANDLER_SRC).read()
        self.assertIn("evaluate_risk(", source)
        self.assertIn("generate_hypothesis(", source)

    def test_no_orchestrator_is_imported(self):
        """The orchestrator is what would run a trading cycle."""
        source = open(HANDLER_SRC).read()
        self.assertNotIn("MarketDayOrchestrator", source)
        self.assertNotIn("from agent.orchestration", source)

    def test_falsifying_control_the_import_scan_can_fail(self):
        """
        The two tests above would pass against an empty file, so prove
        the scan actually detects what it looks for.
        """
        source = open(HANDLER_SRC).read()
        self.assertIn("from agent.risk", source)
        self.assertIn("generate_hypothesis", source)

    def test_every_route_is_a_get_except_the_guarded_admin_post(self):
        """
        A write endpoint is how execution would arrive. There is exactly
        one POST and it only forces a regime evaluation.
        """
        posts = [(m, p) for m, p in handler.ROUTES if m != "GET"]
        self.assertEqual(posts, [("POST", "/agent/regime/evaluate")])

    def test_the_switches_default_to_false(self):
        """A missing environment variable is not permission."""
        for name in ("AGENT_TRADING_ENABLED", "AGENT_EXECUTION_AVAILABLE"):
            with self.subTest(name=name):
                previous = os.environ.pop(name, None)
                try:
                    self.assertFalse(handler._switch(name))
                finally:
                    if previous is not None:
                        os.environ[name] = previous

    def test_a_switch_can_be_turned_on(self):
        """The falsifying control: the reader must actually read."""
        os.environ["AGENT_TEST_SWITCH"] = "true"
        try:
            self.assertTrue(handler._switch("AGENT_TEST_SWITCH"))
        finally:
            os.environ.pop("AGENT_TEST_SWITCH", None)

    def test_an_unreadable_halt_state_is_reported_as_halted(self):
        """
        Fail closed in the REPORT as well as in the engine. Showing it
        as "unknown" would invite someone to assume it was clear.

        There is no DynamoDB table in the test environment, so this
        exercises the real unreadable path - but note that the store
        itself fails closed internally, so this test alone does not
        prove the HANDLER would. The next test covers that layer.
        """
        code, body = call("/agent/switches")
        self.assertEqual(code, 200)
        self.assertTrue(body["global_halt"])
        self.assertIn("unreadable", body["global_halt_detail"])

    def test_the_handler_fails_closed_when_the_store_itself_raises(self):
        """
        DynamoDBHaltStore.get() already fails closed internally, which
        means the handler's own except block almost never runs - and a
        test that only exercises the store reads as coverage of the
        handler without being any. My original test passed purely
        because both layers happen to use the word "unreadable".

        Here the store RAISES, which is the only way to reach the
        handler's layer.
        """
        class Raising:
            def __init__(self, *args, **kwargs):
                pass

            def get(self):
                raise RuntimeError("connection reset")

        original = handler.DynamoDBHaltStore
        handler.DynamoDBHaltStore = Raising
        try:
            _code, body = call("/agent/switches")
        finally:
            handler.DynamoDBHaltStore = original

        self.assertTrue(body["global_halt"])
        self.assertIn("assuming halted", body["global_halt_detail"])
        self.assertIn("connection reset", body["global_halt_detail"])

    def test_falsifying_control_a_clear_halt_is_reported_as_clear(self):
        """
        The two tests above would pass against a handler hardcoded to
        say "halted". Prove it can report the other answer.
        """
        class Clear:
            def __init__(self, *args, **kwargs):
                pass

            def get(self):
                class State:
                    halted = False
                    reason = ""
                return State()

        original = handler.DynamoDBHaltStore
        handler.DynamoDBHaltStore = Clear
        try:
            _code, body = call("/agent/switches")
        finally:
            handler.DynamoDBHaltStore = original
        self.assertFalse(body["global_halt"])


class TestReadEndpoints(unittest.TestCase):

    def test_risk_limits_are_reported_with_a_version(self):
        code, body = call("/agent/risk/limits")
        self.assertEqual(code, 200)
        self.assertTrue(body["version"])
        self.assertIn("ceiling", body["note"].lower())

    def test_the_limits_note_says_capital_is_a_ceiling(self):
        """
        "Available capital is a ceiling, not a target." A dashboard that
        shows unused capital as a gap to close would push exactly the
        wrong behaviour.
        """
        _code, body = call("/agent/risk/limits")
        self.assertIn("not a target", body["note"])

    def test_positions_distinguishes_none_from_unreadable(self):
        """
        "No positions" and "the store could not be read" look identical
        unless the reason is stated. The endpoint used to return a fixed
        empty state saying nothing was scheduled, which became a lie once
        the cycle ran: it reported zero while two positions were open.
        """
        code, body = call("/agent/positions")
        self.assertEqual(code, 200)
        if body["source"] == "unreadable":
            self.assertIsNone(body["open_count"])
            self.assertIn("not a report of zero", body["note"])
        else:
            self.assertEqual(body["source"], "position store")
            self.assertIsInstance(body["open_count"], int)
            self.assertEqual(len(body["positions"]), body["open_count"])

    def test_total_open_risk_is_none_not_zero_when_unknown(self):
        """
        Zero would claim there is no risk. None says we do not know,
        and an understated risk total is worse than no total.
        """
        _code, body = call("/agent/positions")
        self.assertIsNone(body["total_open_risk"])

    def test_hypothesis_requires_a_symbol(self):
        code, body = call("/agent/hypothesis")
        self.assertEqual(code, 400)
        self.assertIn("symbol", body["error"])

    def test_risk_preview_requires_a_symbol(self):
        code, _body = call("/agent/risk/preview")
        self.assertEqual(code, 400)

    def test_pipeline_requires_a_symbol(self):
        code, _body = call("/agent/pipeline")
        self.assertEqual(code, 400)

    def test_an_unknown_route_lists_what_exists(self):
        code, body = call("/agent/nope")
        self.assertEqual(code, 404)
        self.assertIn("available", body)

    def test_a_journal_read_failure_degrades_rather_than_500s(self):
        """
        A dashboard that 500s on a cold table is indistinguishable from
        a broken agent.
        """
        code, body = call("/agent/journal")
        self.assertEqual(code, 200)
        self.assertIn(body["source"], ("dynamodb", "unavailable"))

    def test_performance_reports_a_verdict_even_when_unavailable(self):
        code, body = call("/agent/performance")
        self.assertEqual(code, 200)
        self.assertIn("verdict", body)


class TestNoCompositeScore(unittest.TestCase):
    """
    The pipeline must stay decomposed. A single number would hide which
    stage drove the outcome.
    """

    def test_the_pipeline_response_has_no_composite_score(self):
        source = open(HANDLER_SRC).read()
        self.assertIn('"composite_score": None', source)
        self.assertIn("composite_score_detail", source)

    def test_the_dashboard_renders_each_stage_separately(self):
        page = open(DASHBOARD).read()
        for stage in ("stageQuant", "stageEvidence", "stageRegime",
                      "stageHypothesis", "stageRisk"):
            with self.subTest(stage=stage):
                self.assertIn(stage, page)

    def test_the_dashboard_shows_agreement_and_magnitude_separately(self):
        """
        Unanimous but weak is not the same as split but strong, and one
        number cannot say which it is.
        """
        page = open(DASHBOARD).read()
        self.assertIn("signal agreement", page)
        self.assertIn("signal magnitude", page)

    def test_the_dashboard_shows_materiality_and_novelty_separately(self):
        page = open(DASHBOARD).read()
        self.assertIn("materiality", page)
        self.assertIn("novelty", page)

    def test_hypothesis_strength_is_itemised_not_left_as_one_number(self):
        """
        Strength IS a combination, so its contributions are broken out -
        otherwise it would be exactly the opaque score this dashboard
        exists to avoid.
        """
        page = open(DASHBOARD).read()
        self.assertIn("strength_components", page)

    def test_the_dashboard_does_not_invent_a_blended_score(self):
        page = open(DASHBOARD).read()
        for invented in ("overallScore", "totalScore", "aiScore",
                         "confidenceScore", "compositeScore"):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, page)


class TestDashboardHonesty(unittest.TestCase):
    """
    Prose claims, checked with denials stripped first - a page that says
    "this is not a prediction" must not be reported as claiming to
    predict.
    """

    BANNED = [
        "guaranteed", "risk free", "riskless", "sure thing",
        "cannot lose", "will profit", "proven edge",
    ]

    def _text(self, html: str) -> str:
        without_script = re.sub(r"<script\b.*?</script>", " ", html,
                                flags=re.S | re.I)
        without_style = re.sub(r"<style\b.*?</style>", " ", without_script,
                               flags=re.S | re.I)
        without_tags = re.sub(r"<[^>]+>", " ", without_style)
        return re.sub(r"\s+", " ", without_tags).strip().lower()

    def _strip_denials(self, text: str) -> str:
        """Remove denials so only ASSERTIONS remain.

        The pattern keeps the negation adjacent to its noun, because a
        looser version once matched across sentence boundaries and
        reported a correct disclaimer as a banned claim.
        """
        return re.sub(
            r"\b(?:no|not|never|cannot|nothing|without)\b[\w\s,'-]{0,40}?"
            r"(?=guarantee|risk|profit|proven|predict|certain)",
            " ", text)

    def _claims(self) -> str:
        return self._strip_denials(self._text(open(DASHBOARD).read()))

    def test_the_page_makes_no_banned_claim(self):
        claims = self._claims()
        for banned in self.BANNED:
            with self.subTest(banned=banned):
                self.assertNotIn(banned, claims)

    def test_falsifying_control_the_banned_scan_can_fire(self):
        """
        The test above would pass on an empty string, so prove the scan
        detects a claim that contains no denial.
        """
        planted = self._strip_denials("this strategy is a sure thing")
        self.assertIn("sure thing", planted)

    def test_falsifying_control_denial_stripping_is_targeted(self):
        """Stripping must remove only the denial, not every occurrence."""
        text = ("there is no guarantee here but the guarantee of a stop "
                "is discussed")
        stripped = self._strip_denials(text)
        self.assertIn("guarantee of a stop", stripped)

    def test_the_page_says_paper_prominently(self):
        """
        Before any stripping: the banner is an assertion, not a denial,
        and it must be in the markup rather than only in a tooltip.
        """
        page = open(DASHBOARD).read()
        self.assertIn("PAPER ONLY", page)
        self.assertIn("paper-banner", page)

    def test_the_paper_banner_is_sticky_so_it_cannot_scroll_away(self):
        page = open(DASHBOARD).read()
        banner = page[page.index(".paper-banner"):page.index(".paper-banner")
                      + 400]
        self.assertIn("sticky", banner)

    def test_the_page_discloses_what_a_polled_stop_does_not_guarantee(self):
        """
        The most common way a paper record overstates a strategy. If the
        dashboard shows a stop price, it must say what that price does
        not promise.
        """
        page = open(DASHBOARD).read()
        self.assertIn("ENGINE_POLLED", page)
        self.assertIn("that stop does not exist", page)


class TestPerformancePresentation(unittest.TestCase):
    """
    A striking number from a handful of trades must not look like a
    finding.
    """

    def test_the_metrics_table_shows_the_sample_size(self):
        page = open(DASHBOARD).read()
        self.assertIn("m.sample_size", page)

    def test_the_metrics_table_shows_the_confidence_interval(self):
        page = open(DASHBOARD).read()
        self.assertIn("m.ci_low", page)
        self.assertIn("m.ci_high", page)

    def test_the_metrics_table_shows_adequacy(self):
        page = open(DASHBOARD).read()
        self.assertIn("m.adequacy", page)

    def test_the_metrics_table_shows_whether_it_is_evidence(self):
        """
        The only cell that should gate a decision to increase size.

        Pins the rendering expression, not merely a mention of the
        field. A bare substring check passed even with the cell replaced
        by a dash, because the field name also appears on the adjacent
        line that picks the colour.
        """
        page = open(DASHBOARD).read()
        self.assertIn('pill(m.is_evidence ? "YES" : "no"', page)

    def test_the_metrics_table_renders_the_sample_size_value(self):
        """Same weakness, same fix: pin the rendering."""
        page = open(DASHBOARD).read()
        self.assertIn('el("td", "num", m.sample_size)', page)

    def test_the_page_explains_that_both_conditions_are_required(self):
        page = open(DASHBOARD).read()
        self.assertIn("Both are required", page)

    def test_stop_integrity_is_surfaced(self):
        page = open(DASHBOARD).read()
        self.assertIn("stop_integrity", page)
        self.assertIn("Stop integrity", page)


class TestModuleNameCollision(unittest.TestCase):
    """
    Both Lambdas have a module called `handler`. This has now broken the
    suite twice, so it gets a guard rather than a third comment.
    """

    def test_no_test_file_imports_handler_by_its_bare_name(self):
        """
        `import handler` after a sys.path insert makes whichever test
        runs first win, and the other silently exercises the wrong
        module. The symptom appears only when the whole suite runs
        together, which is what makes it expensive to find.

        test_chatbot_router.py is the one legitimate exception: it owns
        that name for the production router and runs in a process where
        nothing else should claim it.
        """
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        allowed = {"test_chatbot_router.py"}
        offenders = []
        for name in sorted(os.listdir(tests_dir)):
            if not name.endswith(".py") or name in allowed:
                continue
            body = open(os.path.join(tests_dir, name)).read()
            if re.search(r"^import handler\b", body, flags=re.M):
                offenders.append(name)
        self.assertEqual(offenders, [], (
            "these test files import `handler` by its bare name, which "
            "collides across Lambdas; load by path under a distinct "
            "module name instead"))

    def test_falsifying_control_the_scan_finds_a_bare_import(self):
        """The test above would pass against an empty directory."""
        self.assertTrue(
            re.search(r"^import handler\b", "import handler\n", flags=re.M))

    def test_the_agent_api_is_loaded_under_a_distinct_name(self):
        self.assertEqual(handler.__name__, "agent_api_handler")

    def test_the_router_handler_is_a_different_module(self):
        """
        If both names resolved to the same object the guard above would
        be pointless.
        """
        router = sys.modules.get("handler")
        if router is None:
            self.skipTest("the router handler is not loaded in this run")
        self.assertIsNot(router, handler)


class TestPackaging(unittest.TestCase):

    def test_the_dashboard_has_no_external_dependency(self):
        """
        A dashboard that cannot render without a CDN is a dashboard that
        fails when it is most needed.
        """
        page = open(DASHBOARD).read()
        for pattern in ("https://cdn", "http://cdn", "unpkg.com",
                        "jsdelivr", "googleapis.com"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, page)

    def test_the_api_base_comes_from_config_not_a_hardcoded_url(self):
        page = open(DASHBOARD).read()
        self.assertIn('src="config.js"', page)
        self.assertIn("window.AGENT_API_BASE", page)

    def test_a_missing_api_base_is_reported_not_silent(self):
        page = open(DASHBOARD).read()
        self.assertIn("AGENT_API_BASE is not configured", page)

    def test_the_page_declares_a_viewport_for_mobile(self):
        page = open(DASHBOARD).read()
        self.assertIn('name="viewport"', page)

    def test_wide_tables_scroll_rather_than_overflow(self):
        page = open(DASHBOARD).read()
        self.assertIn("scroll-x", page)

    def test_the_page_supports_both_colour_schemes(self):
        page = open(DASHBOARD).read()
        self.assertIn('data-theme="light"', page)
        self.assertIn("color-scheme", page)


if __name__ == "__main__":
    unittest.main()
