"""The canonical session-date parameter, and the conflict it refuses.

This exists because of a real confusion: a request carrying
`session_date` hit a route that only read `date`, got today's empty
partition, and the response echoed `session_date: <today>`. "I ignored
your parameter" and "nothing happened on the day you asked about" were
indistinguishable, and the second reading is the one a person takes.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load_agent_api():
    """Load the API handler under a UNIQUE module name.

    `import handler` after a sys.path insert makes whichever test runs
    first win, and the other silently exercises the wrong module -
    agent-api and agent-cycle both define handler.py. A guard in
    test_agent_dashboard.py forbids the bare name and caught this file
    doing it, which is the guard earning its place.
    """
    import importlib.util
    path = os.path.join(REPO, "lambda-micro", "agent-api", "handler.py")
    spec = importlib.util.spec_from_file_location("agent_api_handler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_api_handler"] = module
    spec.loader.exec_module(module)
    return module


api = _load_agent_api()                                      # noqa: E402


def event(**params):
    return {"queryStringParameters": params or None,
            "requestContext": {"http": {"method": "GET", "path": "/x"}}}


class TestCanonicalParameter(unittest.TestCase):

    def test_session_date_is_honoured(self):
        self.assertEqual(
            api._requested_session_date(event(session_date="2026-10-01")),
            "2026-10-01")

    def test_date_is_still_accepted(self):
        # Ten routes read it and links exist, so dropping it would break
        # saved URLs for no gain.
        self.assertEqual(api._requested_session_date(event(date="2026-10-01")),
                         "2026-10-01")

    def test_both_agreeing_is_fine(self):
        self.assertEqual(
            api._requested_session_date(
                event(date="2026-10-01", session_date="2026-10-01")),
            "2026-10-01")

    def test_both_disagreeing_raises_rather_than_choosing(self):
        with self.assertRaises(api._DateConflict) as caught:
            api._requested_session_date(
                event(date="2026-10-01", session_date="2026-10-02"))
        # Both values survive onto the error so the 400 can name them.
        self.assertEqual(caught.exception.date_value, "2026-10-01")
        self.assertEqual(caught.exception.session_value, "2026-10-02")

    def test_neither_falls_back_to_the_supplied_default(self):
        self.assertEqual(api._requested_session_date(event(), "1999-12-31"),
                         "1999-12-31")

    def test_an_empty_parameter_is_absent_not_a_date(self):
        # "?session_date=" is a caller who supplied nothing, not a
        # caller asking about the empty-string session.
        self.assertEqual(
            api._requested_session_date(event(session_date=""), "2026-01-01"),
            "2026-01-01")

    def test_whitespace_only_is_also_absent(self):
        self.assertEqual(
            api._requested_session_date(event(date="   "), "2026-01-01"),
            "2026-01-01")

    def test_whitespace_is_stripped_rather_than_passed_through(self):
        # A padded date would miss its partition and read as an empty day.
        self.assertEqual(
            api._requested_session_date(event(session_date=" 2026-10-01 ")),
            "2026-10-01")

    def test_no_parameters_at_all_still_returns_a_date(self):
        got = api._requested_session_date(event())
        self.assertRegex(got, r"^\d{4}-\d{2}-\d{2}$")


class TestTheConflictBecomesA400(unittest.TestCase):

    def test_the_router_turns_a_conflict_into_400_not_500(self):
        resp = api.lambda_handler({
            "requestContext": {"http": {"method": "GET",
                                        "path": "/agent/journal"}},
            "queryStringParameters": {"date": "2026-10-01",
                                      "session_date": "2026-10-02"}}, None)
        self.assertEqual(resp["statusCode"], 400)
        import json
        body = json.loads(resp["body"])
        self.assertEqual(body["canonical"], "session_date")
        # Both values named, so the caller can see WHICH two questions
        # they asked rather than being told to try again.
        self.assertEqual(body["date"], "2026-10-01")
        self.assertEqual(body["session_date"], "2026-10-02")


class TestFalsifyingControls(unittest.TestCase):
    """The checks above must be capable of failing."""

    def test_the_conflict_check_is_reachable(self):
        # If _DateConflict could never be raised, every test above would
        # pass by being unreachable. Proven by raising it.
        with self.assertRaises(api._DateConflict):
            api._requested_session_date(event(date="a", session_date="b"))

    def test_the_helper_is_actually_wired_into_the_handlers(self):
        # The helper existing is not the same as the routes using it. A
        # route still reading params.get("date") directly would be
        # unaffected by all of the above.
        with open(os.path.join(
                REPO, "lambda-micro", "agent-api", "handler.py")) as fh:
            src = fh.read()
        self.assertGreaterEqual(src.count("_requested_session_date(event"), 10)
        # And no handler may read "date" directly any more. The helper's
        # own line is the single legitimate occurrence.
        direct = src.count('get("date")')
        self.assertEqual(
            direct, 1,
            "a route still reads the date parameter directly, so the "
            "canonical helper does not govern it")


if __name__ == "__main__":
    unittest.main()
