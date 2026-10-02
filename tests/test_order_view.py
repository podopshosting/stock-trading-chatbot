"""
The read-only order view, and why it is a separate implementation.

The read API may not import anything from `agent/broker/`, because the
submission path lives there. So `agent/autonomy/order_view.py` reads the
same rows and does its own arithmetic.

That is duplication. Duplication drifts, and this drift would be
invisible: both sides are internally consistent, so every other test
would still pass while the API reported a different exposure from the
one the cycle reserves against - or looked in the wrong DynamoDB
partition and reported zero outstanding orders, which is the one answer
a reader would act on.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.autonomy.order_view import (                           # noqa: E402
    PARTITION_PREFIX, TERMINAL, OrderLedgerView, OrderViewError,
    is_settled, is_terminal, potential_exposure, summarise,
)
from agent.broker import order_ledger as ledger_module             # noqa: E402
from agent.broker.order_ledger import ExternalOrderRecord          # noqa: E402

SESSION = "2026-10-02"


def row(**kw):
    base = {"client_order_id": "cli_a", "session_date": SESSION,
            "symbol": "XYZ", "side": "BUY", "intent": "ENTRY",
            "requested_quantity": 1.0, "requested_notional": 100.0,
            "filled_quantity": 0.0, "average_fill_price": None,
            "status": None, "submission_outcome_known": False,
            "intent_at": "2026-10-02T18:00:00+00:00"}
    base.update(kw)
    return base


class TestTheTwoViewsCannotDrift(unittest.TestCase):
    """Pinned against agent/broker/order_ledger.py."""

    CASES = [
        row(),
        row(status="SUBMITTED", submission_outcome_known=True),
        row(status="FILLED", submission_outcome_known=True,
            filled_quantity=1.0, average_fill_price=100.0),
        row(status="PARTIALLY_FILLED", submission_outcome_known=True,
            filled_quantity=0.4, average_fill_price=100.0),
        row(status="CANCELLED", submission_outcome_known=True),
        row(status="NEVER_PLACED", submission_outcome_known=True),
        row(status="REJECTED", submission_outcome_known=True),
        row(status="SOMETHING_NEW", submission_outcome_known=True),
        row(requested_notional=None, requested_quantity=None),
        row(requested_notional=None, requested_quantity=1.0,
            average_fill_price=50.0),
        row(status="FILLED", submission_outcome_known=False,
            filled_quantity=1.0, average_fill_price=100.0),
    ]

    def test_the_terminal_sets_are_identical(self):
        self.assertEqual(TERMINAL, ledger_module.TERMINAL)

    def test_the_partition_prefix_matches_the_writer(self):
        """A reader looking in the wrong partition reports zero
        outstanding orders rather than failing."""
        source = (ledger_module.__file__ or "")
        with open(source) as handle:
            written = handle.read()
        self.assertIn(f'f"{PARTITION_PREFIX}{{record.session_date}}"',
                      written,
                      "the view's partition prefix is not the one the "
                      "ledger writes")

    def test_is_terminal_agrees_for_every_case(self):
        for case in self.CASES:
            with self.subTest(status=case["status"]):
                record = ExternalOrderRecord.from_dict(case)
                self.assertEqual(is_terminal(case), record.is_terminal)

    def test_potential_exposure_agrees_for_every_case(self):
        for case in self.CASES:
            with self.subTest(status=case["status"],
                              notional=case["requested_notional"]):
                record = ExternalOrderRecord.from_dict(case)
                mine = potential_exposure(case)
                theirs = record.potential_exposure
                if mine is None or theirs is None:
                    self.assertEqual(mine, theirs)
                else:
                    self.assertAlmostEqual(mine, theirs, places=6)

    def test_the_settled_judgement_agrees(self):
        for case in self.CASES:
            with self.subTest(status=case["status"]):
                record = ExternalOrderRecord.from_dict(case)
                self.assertEqual(
                    is_settled(case),
                    record.is_terminal and record.submission_outcome_known)

    def test_the_reserved_total_agrees_with_committed_exposure(self):
        mine = summarise(self.CASES)
        theirs = ledger_module.committed_exposure(
            [ExternalOrderRecord.from_dict(c) for c in self.CASES])
        self.assertEqual(mine["committed_exposure_known"], theirs["known"])
        self.assertEqual(sorted(mine["exposure_unestablished_for"]),
                         sorted(theirs["unestablished"]))
        if theirs["known"]:
            self.assertAlmostEqual(mine["committed_exposure"],
                                   theirs["reserved"], places=6)

    def test_the_reserved_total_agrees_on_a_knowable_set(self):
        """The set above is deliberately unknowable, so this covers the
        arithmetic itself rather than only the unknown flag."""
        knowable = [c for c in self.CASES
                    if not (c["requested_notional"] is None
                            and c["requested_quantity"] is None)]
        mine = summarise(knowable)
        theirs = ledger_module.committed_exposure(
            [ExternalOrderRecord.from_dict(c) for c in knowable])
        self.assertTrue(theirs["known"])
        self.assertAlmostEqual(mine["committed_exposure"],
                               theirs["reserved"], places=6)


class TestTheSummaryKeepsUnknownsUnknown(unittest.TestCase):

    def test_an_unsizeable_order_nulls_the_exposure(self):
        out = summarise([row(requested_notional=None,
                             requested_quantity=None)])
        self.assertIsNone(out["committed_exposure"],
                          "unknown must not render as 0")
        self.assertFalse(out["committed_exposure_known"])
        self.assertEqual(out["exposure_unestablished_for"], ["cli_a"])

    def test_nothing_outstanding_is_a_known_zero(self):
        out = summarise([row(status="FILLED",
                             submission_outcome_known=True,
                             filled_quantity=1.0,
                             average_fill_price=100.0)])
        self.assertTrue(out["committed_exposure_known"])
        self.assertEqual(out["committed_exposure"], 0.0)
        self.assertEqual(out["outstanding"], 0)

    def test_an_unrecognised_status_still_reserves(self):
        out = summarise([row(status="SOMETHING_NEW",
                             submission_outcome_known=True)])
        self.assertEqual(out["outstanding"], 1)
        self.assertGreater(out["committed_exposure"], 0.0)

    def test_an_unresolved_submission_is_counted(self):
        out = summarise([row(submission_outcome_known=False)])
        self.assertEqual(out["unresolved_submissions"], 1)

    def test_partial_fills_are_counted_separately(self):
        out = summarise([row(status="PARTIALLY_FILLED",
                             submission_outcome_known=True,
                             filled_quantity=0.4,
                             average_fill_price=100.0)])
        self.assertEqual(out["partial_fills"], 1)

    def test_orders_are_grouped_by_intent(self):
        out = summarise([row(intent="ENTRY"),
                         row(client_order_id="cli_b", intent="EXIT")])
        self.assertEqual(out["by_intent"], {"ENTRY": 1, "EXIT": 1})

    def test_the_oldest_pending_age_is_reported(self):
        out = summarise([row(intent_at="2020-01-01T00:00:00+00:00")])
        self.assertGreater(out["oldest_pending_age_seconds"], 0)


class TestReadsRaiseRatherThanReturningEmpty(unittest.TestCase):

    class Boom:
        def query(self, **_kw):
            raise RuntimeError("table gone")

    class NoPayload:
        def query(self, **_kw):
            return {"Items": [{"PK": {"S": "EXTORDERS#x"},
                               "SK": {"S": "cli_a"}}]}

    class Malformed:
        def query(self, **_kw):
            return {"Items": [{"payload": {"S": "{not json"}}]}

    def test_a_failed_query_raises(self):
        with self.assertRaises(OrderViewError):
            OrderLedgerView(table_name="t",
                            client=self.Boom()).for_session(SESSION)

    def test_a_row_with_no_payload_raises(self):
        with self.assertRaises(OrderViewError):
            OrderLedgerView(table_name="t",
                            client=self.NoPayload()).for_session(SESSION)

    def test_a_malformed_row_raises_rather_than_being_skipped(self):
        """A partial ledger cannot establish exposure at all."""
        with self.assertRaises(OrderViewError):
            OrderLedgerView(table_name="t",
                            client=self.Malformed()).for_session(SESSION)

    def test_the_view_queries_the_written_partition(self):
        seen = {}

        class Spy:
            def query(self, **kw):
                seen.update(kw)
                return {"Items": []}

        OrderLedgerView(table_name="t", client=Spy()).for_session(SESSION)
        self.assertEqual(
            seen["ExpressionAttributeValues"][":pk"]["S"],
            f"EXTORDERS#{SESSION}")
