"""
Every log event a handler emits must be registered.

Found by deployment on 2026-10-01. `AGENT_BROKER=alpaca_paper` was
selected for the first time and the cycle died with
`ValueError: unknown log event 'broker_unavailable'`. Neither
`broker_selected` nor `broker_unavailable` was in observability.EVENTS,
so the branch crashed whichever way it went - and because the success
log sat inside the try, the except clause caught its own logging failure
and reported it as a broker failure before dying on the fallback log.

The logger fails closed on an unknown event, which is right. What was
missing is anything that checks the two sides agree.
"""
import ast
import os
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent.observability import EVENTS, log_event                  # noqa: E402

HANDLERS = [
    REPO / "lambda-micro" / "agent-cycle" / "handler.py",
    REPO / "lambda-micro" / "agent-api" / "handler.py",
]

# Everything that logs, not just the handlers. The defect was reachable
# only on a branch's first production run, so the guard has to cover
# every emitter rather than the two that happened to break.
ALL_EMITTERS = sorted(
    [p for p in (REPO / "agent").rglob("*.py")
     if "__pycache__" not in str(p)]
    + HANDLERS)


def logged_events(path):
    """Every literal name passed to log_event() in a module."""
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "log_event"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            names.add(node.args[0].value)
    return names


class TestEveryLoggedEventIsRegistered(unittest.TestCase):

    def test_the_broker_selection_events_are_registered(self):
        """The two that actually broke the deploy."""
        for name in ("broker_selected", "broker_unavailable"):
            with self.subTest(name=name):
                self.assertIn(name, EVENTS)

    def test_logging_them_does_not_raise(self):
        """Membership in a frozenset is not the claim; being loggable is.
        This executes the real logger rather than inspecting its table."""
        log_event("broker_selected", broker="alpaca_paper",
                  base_url="https://paper-api.alpaca.markets",
                  authoritative=True)
        log_event("broker_unavailable", broker="alpaca_paper",
                  error="ValueError: test")

    def test_every_event_any_handler_emits_is_registered(self):
        """The general guard. Two name assertions would not have caught
        the next unregistered event, and this class of defect is only
        reachable in production, on the first run of a new branch."""
        for path in HANDLERS:
            with self.subTest(handler=path.name):
                missing = sorted(logged_events(path) - set(EVENTS))
                self.assertEqual(
                    missing, [],
                    f"{path.relative_to(REPO)} emits unregistered "
                    f"event(s): {missing}")

    def test_every_event_anywhere_in_the_codebase_is_registered(self):
        """Widest form. A handler is not the only thing that logs."""
        offenders = {}
        for path in ALL_EMITTERS:
            missing = sorted(logged_events(path) - set(EVENTS))
            if missing:
                offenders[str(path.relative_to(REPO))] = missing
        self.assertEqual(offenders, {})

    def test_the_scan_finds_events_at_all(self):
        """The control. If the AST walk silently found nothing, the
        assertions above would pass for a codebase emitting a hundred
        unknown events. The cycle handler logs exactly 7 directly - the
        rest come from the modules it calls - so the count is asserted
        as a floor, not a guess."""
        found = logged_events(HANDLERS[0])
        self.assertGreaterEqual(len(found), 7)
        self.assertIn("cycle_aborted", found)

    def test_the_scan_sees_a_deliberately_unregistered_name(self):
        """Proves the comparison can fail, not just that it passes."""
        import ast as _ast
        tmp = REPO / "tests" / "_scan_fixture.py"
        tmp.write_text('log_event("definitely_not_a_real_event_xyz")\n')
        try:
            found = logged_events(tmp)
            self.assertIn("definitely_not_a_real_event_xyz", found)
            self.assertNotIn("definitely_not_a_real_event_xyz", EVENTS)
        finally:
            tmp.unlink()


class TestALoggingFailureIsNotABrokerFailure(unittest.TestCase):
    """
    The structural half. With the success log inside the try, a logging
    error is caught by `except Exception` and reported as
    `broker_unavailable`, which silently downgrades to the internal
    simulator and files external-labelled evidence as though the venue
    had refused. That is the outcome the comment above it says it exists
    to prevent.
    """

    def broker_try_block(self):
        src = (REPO / "lambda-micro" / "agent-cycle" / "handler.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)
                     and n.func.id == "AlpacaPaperBroker"]
            if calls:
                return node
        return None

    def test_the_adapter_construction_is_guarded(self):
        """The control: the try must still exist and still wrap the
        construction, or the assertion below is vacuous."""
        self.assertIsNotNone(self.broker_try_block())

    def test_no_logging_happens_inside_the_guarded_block(self):
        node = self.broker_try_block()
        inside = [n.args[0].value for n in ast.walk(ast.Module(body=node.body,
                                                               type_ignores=[]))
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name)
                  and n.func.id == "log_event"
                  and n.args and isinstance(n.args[0], ast.Constant)]
        self.assertEqual(
            inside, [],
            f"log_event({inside}) is inside the try that falls back to "
            f"the internal simulator, so a logging failure would be "
            f"reported as a broker failure")


if __name__ == "__main__":
    unittest.main()
