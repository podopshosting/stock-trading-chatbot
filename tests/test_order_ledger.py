"""
The order lifecycle, asynchronously.

Written against a real incident: an order was submitted, read back
unfilled, recorded as not filled, and filled by the venue afterwards.
Every case here is one the internal simulator cannot produce, because it
fills synchronously or never.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.broker.order_ledger import (                              # noqa: E402
    ExternalOrderRecord, InMemoryOrderLedger, OrderLedgerError,
    committed_exposure, TERMINAL,
    LIFECYCLE_CANCELLED_PARTIAL, LIFECYCLE_CANCELLED_UNFILLED,
    LIFECYCLE_EXPECTED_NOT_FILLED, LIFECYCLE_FILLED,
    LIFECYCLE_PARTIALLY_FILLED, LIFECYCLE_REJECTED, LIFECYCLE_UNKNOWN,
)

DAY = "2026-10-02"


def intent(cid="cli_1", notional=25.0, qty=None, symbol="AAA"):
    return ExternalOrderRecord(
        client_order_id=cid, session_date=DAY, symbol=symbol, side="BUY",
        requested_notional=notional, requested_quantity=qty,
        hypothesis_id="h1", risk_decision_id="r1", intent_at="T0")


def observed(status, filled=0.0, qty=1.0, avg=None, raw=None):
    return {"order_id": "brk_1", "status": status, "raw_status": raw or
            status.lower(), "filled_quantity": filled,
            "remaining_quantity": max(0.0, qty - filled),
            "average_fill_price": avg, "submitted_at": "T1"}


class TestAcceptedButUnfilled(unittest.TestCase):

    def setUp(self):
        self.led = InMemoryOrderLedger()
        self.led.record_intent(intent())
        self.row = self.led.record_observation("cli_1",
                                               observed("SUBMITTED"), "T1")

    def test_it_is_not_terminal(self):
        self.assertFalse(self.row.is_terminal)

    def test_it_reserves_its_whole_notional(self):
        """A second order sized as though the first were not live is how
        an account ends up twice as exposed as its limits allow."""
        ex = committed_exposure(self.led.for_session(DAY))
        self.assertAlmostEqual(ex["reserved"], 25.0)
        self.assertTrue(ex["known"])

    def test_it_counts_as_a_live_order(self):
        ex = committed_exposure(self.led.for_session(DAY))
        self.assertEqual(ex["live_orders"], ["cli_1"])

    def test_it_appears_in_non_terminal(self):
        self.assertEqual([r.client_order_id
                          for r in self.led.non_terminal(DAY)], ["cli_1"])


class TestLostSubmitResponse(unittest.TestCase):
    """The dangerous case: the request went out and no usable response
    came back. The broker may or may not hold the order."""

    def setUp(self):
        self.led = InMemoryOrderLedger()
        self.led.record_intent(intent())

    def test_the_outcome_is_unknown_not_absent(self):
        row = self.led.get("cli_1")
        self.assertFalse(row.submission_outcome_known)
        self.assertEqual(row.lifecycle, LIFECYCLE_UNKNOWN)

    def test_it_is_not_treated_as_terminal(self):
        self.assertFalse(self.led.get("cli_1").is_terminal)

    def test_exposure_is_still_reserved(self):
        """Uncertainty must cost capital, not free it."""
        ex = committed_exposure(self.led.for_session(DAY))
        self.assertAlmostEqual(ex["reserved"], 25.0)

    def test_recovery_is_by_client_order_id(self):
        """The handle that survives a lost response - the broker id is
        unknown at the moment of intent."""
        self.assertIsNotNone(self.led.get("cli_1"))
        row = self.led.record_observation("cli_1",
                                          observed("FILLED", 1.0, 1.0, 25.0),
                                          "T2")
        self.assertEqual(row.broker_order_id, "brk_1")
        self.assertEqual(row.lifecycle, LIFECYCLE_FILLED)

    def test_a_repeated_intent_does_not_overwrite_the_record(self):
        """Retrying the same deterministic id must not erase what has
        been observed about the first attempt."""
        self.led.record_observation("cli_1", observed("SUBMITTED"), "T1")
        self.led.record_intent(intent())
        self.assertEqual(self.led.get("cli_1").observations, 1)

    def test_an_observation_without_an_intent_is_refused(self):
        """An order observed with no recorded intent cannot be
        attributed, and silently inventing a record would fabricate
        provenance."""
        with self.assertRaises(OrderLedgerError):
            self.led.record_observation("cli_unknown",
                                        observed("FILLED", 1.0), "T2")


class TestFillProgression(unittest.TestCase):

    def ledger_with(self, *observations):
        led = InMemoryOrderLedger()
        led.record_intent(intent(qty=10.0, notional=None))
        row = None
        for i, o in enumerate(observations):
            row = led.record_observation("cli_1", o, f"T{i+1}")
        return led, row

    def test_a_partial_fill_is_tracked_cumulatively(self):
        _led, row = self.ledger_with(
            observed("PARTIALLY_FILLED", 3.0, 10.0, 10.0),
            observed("PARTIALLY_FILLED", 7.0, 10.0, 10.0))
        self.assertEqual(row.filled_quantity, 7.0)
        self.assertEqual(row.remaining_quantity, 3.0)
        self.assertEqual(row.lifecycle, LIFECYCLE_PARTIALLY_FILLED)

    def test_one_logical_order_stays_one_record(self):
        led, _row = self.ledger_with(
            observed("PARTIALLY_FILLED", 3.0, 10.0, 10.0),
            observed("PARTIALLY_FILLED", 7.0, 10.0, 10.0),
            observed("FILLED", 10.0, 10.0, 10.0))
        self.assertEqual(len(led.for_session(DAY)), 1)

    def test_an_out_of_order_observation_cannot_unfill(self):
        """A stale response reporting less filled than already seen must
        not un-fill a position."""
        _led, row = self.ledger_with(
            observed("PARTIALLY_FILLED", 7.0, 10.0, 10.0),
            observed("PARTIALLY_FILLED", 3.0, 10.0, 10.0))
        self.assertEqual(row.filled_quantity, 7.0)

    def test_a_duplicate_status_response_is_harmless(self):
        _led, row = self.ledger_with(
            observed("PARTIALLY_FILLED", 4.0, 10.0, 10.0),
            observed("PARTIALLY_FILLED", 4.0, 10.0, 10.0))
        self.assertEqual(row.filled_quantity, 4.0)
        self.assertEqual(row.observations, 2)

    def test_a_full_fill_is_terminal_and_reserves_nothing(self):
        led, row = self.ledger_with(observed("FILLED", 10.0, 10.0, 10.0))
        self.assertTrue(row.is_terminal)
        self.assertEqual(committed_exposure(led.for_session(DAY))["reserved"],
                         0.0)


class TestTerminalOutcomes(unittest.TestCase):

    def run_to(self, status, filled=0.0):
        led = InMemoryOrderLedger()
        led.record_intent(intent(qty=10.0, notional=None))
        return led.record_observation(
            "cli_1", observed(status, filled, 10.0,
                              10.0 if filled else None), "T1")

    def test_cancelled_unfilled(self):
        row = self.run_to("CANCELLED")
        self.assertEqual(row.lifecycle, LIFECYCLE_CANCELLED_UNFILLED)
        self.assertFalse(row.created_exposure)

    def test_cancelled_after_a_partial_fill_keeps_the_exposure(self):
        """Cancellation is not the same as no exposure: the filled
        portion is real and must survive the cancel."""
        row = self.run_to("CANCELLED", filled=4.0)
        self.assertEqual(row.lifecycle, LIFECYCLE_CANCELLED_PARTIAL)
        self.assertTrue(row.created_exposure)
        self.assertEqual(row.filled_quantity, 4.0)

    def test_rejected(self):
        self.assertEqual(self.run_to("REJECTED").lifecycle,
                         LIFECYCLE_REJECTED)

    def test_expired_unfilled_reads_as_cancelled_unfilled(self):
        self.assertEqual(self.run_to("EXPIRED").lifecycle,
                         LIFECYCLE_CANCELLED_UNFILLED)

    def test_every_terminal_status_is_in_the_terminal_set(self):
        for s in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
            with self.subTest(s=s):
                self.assertIn(s, TERMINAL)


class TestUnknownStatusIsNeverTerminal(unittest.TestCase):
    """An order we cannot classify must not be treated as finished."""

    def test_a_status_nobody_recognises_stays_live(self):
        led = InMemoryOrderLedger()
        led.record_intent(intent())
        row = led.record_observation(
            "cli_1", observed("SOMETHING_NEW", 0.0, 1.0), "T1")
        self.assertFalse(row.is_terminal)
        self.assertEqual([r.client_order_id for r in led.non_terminal(DAY)],
                         ["cli_1"])

    def test_it_still_reserves_exposure(self):
        led = InMemoryOrderLedger()
        led.record_intent(intent())
        led.record_observation("cli_1", observed("SOMETHING_NEW"), "T1")
        self.assertAlmostEqual(
            committed_exposure(led.for_session(DAY))["reserved"], 25.0)


class TestUnknownExposureBlocksNewEntries(unittest.TestCase):
    """An order whose size cannot be established makes the total
    UNKNOWN, and unknown must not read as zero."""

    def test_an_unsizeable_order_makes_the_total_unknown(self):
        led = InMemoryOrderLedger()
        led.record_intent(ExternalOrderRecord(
            client_order_id="cli_x", session_date=DAY, symbol="AAA",
            side="BUY"))                        # no notional, no quantity
        ex = committed_exposure(led.for_session(DAY))
        self.assertFalse(ex["known"])
        self.assertEqual(ex["unestablished"], ["cli_x"])

    def test_a_sizeable_order_is_known(self):
        """The control: without it, `known` could be constantly False."""
        led = InMemoryOrderLedger()
        led.record_intent(intent())
        self.assertTrue(committed_exposure(led.for_session(DAY))["known"])

    def test_two_live_orders_sum(self):
        led = InMemoryOrderLedger()
        led.record_intent(intent("cli_1", 25.0))
        led.record_intent(intent("cli_2", 50.0))
        self.assertAlmostEqual(
            committed_exposure(led.for_session(DAY))["reserved"], 75.0)


class TestRestartRecovery(unittest.TestCase):
    """The new process must reconstruct truth from persisted ids, not
    from in-memory objects."""

    def test_a_pending_order_survives_a_round_trip(self):
        led = InMemoryOrderLedger()
        led.record_intent(intent())
        led.record_observation("cli_1", observed("SUBMITTED"), "T1")
        wire = led.get("cli_1").as_dict()
        revived = ExternalOrderRecord.from_dict(wire)
        self.assertEqual(revived.client_order_id, "cli_1")
        self.assertFalse(revived.is_terminal)
        self.assertEqual(revived.hypothesis_id, "h1")

    def test_a_filled_order_survives_a_round_trip(self):
        led = InMemoryOrderLedger()
        led.record_intent(intent(qty=2.0, notional=None))
        led.record_observation("cli_1", observed("FILLED", 2.0, 2.0, 11.0),
                               "T1")
        revived = ExternalOrderRecord.from_dict(led.get("cli_1").as_dict())
        self.assertTrue(revived.is_terminal)
        self.assertEqual(revived.lifecycle, LIFECYCLE_FILLED)
        self.assertEqual(revived.filled_quantity, 2.0)

    def test_the_derived_fields_are_not_stored_back(self):
        """as_dict carries derived values for readers; from_dict must
        ignore them rather than letting a stale derived value override
        the computation."""
        d = ExternalOrderRecord.from_dict(
            {"client_order_id": "c", "session_date": DAY, "symbol": "A",
             "side": "BUY", "status": "FILLED", "is_terminal": False,
             "lifecycle": "NONSENSE"})
        self.assertTrue(d.is_terminal)


class TestExpectedNotFilled(unittest.TestCase):
    def test_a_terminal_unfilled_entry_is_named_as_such(self):
        led = InMemoryOrderLedger()
        led.record_intent(intent(qty=1.0, notional=None))
        row = led.record_observation("cli_1", observed("SUBMITTED"), "T1")
        self.assertEqual(row.lifecycle, LIFECYCLE_EXPECTED_NOT_FILLED)


class TestAnUnreadableLedgerIsNotAnEmptyOne(unittest.TestCase):
    """
    The whole point of the ledger is to establish whether exposure
    exists. A read that fails and returns [] would say the opposite of
    what is true, so these reads raise.
    """

    def ledger(self, fail=False, items=None):
        from unittest import mock
        from agent.broker.order_ledger import DynamoDBOrderLedger
        fake = mock.Mock()
        if fail:
            fake.query.side_effect = RuntimeError("table unavailable")
            fake.get_item.side_effect = RuntimeError("table unavailable")
        else:
            fake.query.return_value = {"Items": items or []}
            fake.get_item.return_value = {}
        return DynamoDBOrderLedger(table_name="t", client=fake)

    def test_a_failed_session_read_raises(self):
        with self.assertRaises(OrderLedgerError):
            self.ledger(fail=True).for_session(DAY)

    def test_a_failed_lookup_raises(self):
        with self.assertRaises(OrderLedgerError):
            self.ledger(fail=True).get("cli_1")

    def test_a_successful_empty_read_is_empty(self):
        """The control: empty means a read proved empty."""
        self.assertEqual(self.ledger().for_session(DAY), [])

    def test_a_malformed_row_raises_rather_than_vanishing(self):
        """One unreadable row must not silently reduce the apparent
        exposure - a partial ledger cannot establish exposure at all."""
        led = self.ledger(items=[{"payload": {"S": "not json"}}])
        with self.assertRaises(OrderLedgerError):
            led.for_session(DAY)

    def test_a_readable_row_is_returned(self):
        import json as _json
        row = {"client_order_id": "cli_9", "session_date": DAY,
               "symbol": "AAA", "side": "BUY", "requested_notional": 10.0}
        led = self.ledger(items=[{"payload": {"S": _json.dumps(row)}}])
        got = led.for_session(DAY)
        self.assertEqual([r.client_order_id for r in got], ["cli_9"])

    def test_a_repeated_intent_is_not_an_error(self):
        """The conditional write fails closed on a duplicate id, and that
        is the expected path for a retry, not a fault."""
        from unittest import mock
        from agent.broker.order_ledger import DynamoDBOrderLedger

        class ConditionalCheckFailedException(Exception):
            pass
        fake = mock.Mock()
        fake.put_item.side_effect = ConditionalCheckFailedException("exists")
        led = DynamoDBOrderLedger(table_name="t", client=fake)
        led.record_intent(intent())          # must not raise

    def test_a_real_write_failure_does_raise(self):
        """The control for the one above: only the duplicate case is
        tolerated."""
        from unittest import mock
        from agent.broker.order_ledger import DynamoDBOrderLedger
        fake = mock.Mock()
        fake.put_item.side_effect = RuntimeError("throughput exceeded")
        led = DynamoDBOrderLedger(table_name="t", client=fake)
        with self.assertRaises(OrderLedgerError):
            led.record_intent(intent())
