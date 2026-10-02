"""
Following up orders whose outcome is not yet known.

The property that matters: the three ways an order can fail to resolve
are not interchangeable. "The venue says it does not exist", "the venue
would not answer", and "the venue has forgotten an order it
acknowledged" release exposure in different directions, and collapsing
them is how a reservation gets dropped against a live order.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker.order_ledger import (                          # noqa: E402
    NEVER_PLACED, ExternalOrderRecord, InMemoryOrderLedger, OrderLedgerError,
)
from agent.broker.order_poller import (                           # noqa: E402
    ABSENCE_GRACE_SECONDS, INTEGRITY_COMPLETE, INTEGRITY_PARTIAL,
    INTEGRITY_UNKNOWN, poll_outstanding,
)

SESSION = "2026-10-02"
NOW = datetime(2026, 10, 2, 18, 0, 0, tzinfo=timezone.utc)


def old_stamp(seconds=600):
    return (NOW - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def intent(cli="cli_a", symbol="XYZ", notional=100.0, age=600, **kw):
    row = ExternalOrderRecord(
        client_order_id=cli, session_date=SESSION, symbol=symbol,
        side="BUY", requested_quantity=1.0, requested_notional=notional,
        intent_at=old_stamp(age))
    for k, v in kw.items():
        setattr(row, k, v)
    return row


class Venue:
    """Only the lookup surface the poller is allowed to use."""

    def __init__(self, by_id=None, by_cli=None, raises=None):
        self._by_id = by_id or {}
        self._by_cli = by_cli or {}
        self._raises = raises
        self.id_calls, self.cli_calls = [], []

    def get_order(self, order_id):
        self.id_calls.append(order_id)
        if self._raises:
            raise self._raises
        return self._by_id.get(order_id)

    def find_by_client_order_id(self, cli):
        self.cli_calls.append(cli)
        if self._raises:
            raise self._raises
        return self._by_cli.get(cli)


class NoLookupVenue:
    pass


def order(status="FILLED", filled=1.0, avg=100.0, oid="brk_1", raw=None):
    return {"order_id": oid, "status": status, "raw_status": raw or
            status.lower(), "filled_quantity": filled,
            "remaining_quantity": max(0.0, 1.0 - filled),
            "average_fill_price": avg,
            "submitted_at": old_stamp(590)}


class TestResolvingWhatCanBeResolved(unittest.TestCase):

    def test_a_fill_is_folded_in_and_becomes_terminal(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent())
        venue = Venue(by_cli={"cli_a": order()})
        out = poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(out["observed"], 1)
        self.assertEqual(out["became_terminal"], 1)
        self.assertEqual(out["integrity"], INTEGRITY_COMPLETE)
        row = ledger.get("cli_a")
        self.assertEqual(row.status, "FILLED")
        self.assertEqual(row.filled_quantity, 1.0)
        self.assertTrue(row.submission_outcome_known)

    def test_a_transition_is_reported_and_an_unchanged_status_is_not(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent())
        venue = Venue(by_cli={"cli_a": order(status="SUBMITTED", filled=0.0,
                                             avg=None)})
        first = poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(len(first["transitions"]), 1)
        self.assertEqual(first["transitions"][0]["to"], "SUBMITTED")
        self.assertEqual(first["still_open"], 1)
        second = poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(second["transitions"], [])

    def test_the_broker_id_is_preferred_once_the_venue_has_one(self):
        """The venue's own handle cannot collide with a client id."""
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent(broker_order_id="brk_9"))
        venue = Venue(by_id={"brk_9": order(oid="brk_9")})
        poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(venue.id_calls, ["brk_9"])
        self.assertEqual(venue.cli_calls, [])

    def test_a_terminal_order_is_not_polled_again(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent())
        venue = Venue(by_cli={"cli_a": order()})
        poll_outstanding(venue, ledger, SESSION, now=NOW)
        venue.cli_calls.clear()
        out = poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(out["polled"], 0)
        self.assertEqual(venue.cli_calls, [])

    def test_filled_quantity_never_goes_backwards_across_polls(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent(cli="cli_p", notional=200.0))
        venue = Venue(by_cli={"cli_p": order(status="PARTIALLY_FILLED",
                                             filled=0.6, avg=100.0)})
        poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(ledger.get("cli_p").filled_quantity, 0.6)
        # A stale response reporting less filled must not un-fill it.
        venue._by_cli["cli_p"] = order(status="PARTIALLY_FILLED",
                                       filled=0.2, avg=100.0)
        poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(ledger.get("cli_p").filled_quantity, 0.6)


class TestAbsenceIsNotTheSameAsSilence(unittest.TestCase):

    def test_a_confirmed_absence_after_the_grace_period_releases_it(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent(age=ABSENCE_GRACE_SECONDS + 10))
        out = poll_outstanding(Venue(), ledger, SESSION, now=NOW)
        self.assertEqual(out["never_placed"], 1)
        row = ledger.get("cli_a")
        self.assertEqual(row.status, NEVER_PLACED)
        self.assertTrue(row.is_terminal)
        self.assertEqual(row.potential_exposure, 0.0)
        self.assertIsNone(row.raw_status, "the venue never said this")

    def test_a_confirmed_absence_inside_the_grace_period_does_not(self):
        """An order list that has not caught up looks exactly like an
        order that was never placed."""
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent(age=ABSENCE_GRACE_SECONDS - 10))
        out = poll_outstanding(Venue(), ledger, SESSION, now=NOW)
        self.assertEqual(out["never_placed"], 0)
        self.assertEqual(out["still_open"], 1)
        self.assertNotEqual(ledger.get("cli_a").potential_exposure, 0.0)

    def test_an_intent_with_no_timestamp_is_never_concluded_absent(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent(intent_at=None))
        out = poll_outstanding(Venue(), ledger, SESSION, now=NOW)
        self.assertEqual(out["never_placed"], 0)
        self.assertEqual(out["still_open"], 1)

    def test_a_lookup_failure_is_not_an_absence(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent())
        out = poll_outstanding(Venue(raises=RuntimeError("venue down")),
                               ledger, SESSION, now=NOW)
        self.assertEqual(out["never_placed"], 0)
        self.assertEqual(out["unresolved"], 1)
        self.assertEqual(out["integrity"], INTEGRITY_PARTIAL)
        # The reservation stands.
        self.assertTrue(out["exposure_known"])
        self.assertGreater(out["exposure"]["reserved"], 0.0)

    def test_an_acknowledged_order_the_venue_forgets_keeps_its_exposure(self):
        """Not a confirmed absence: we have the venue's own id for it."""
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent(broker_order_id="brk_gone"))
        out = poll_outstanding(Venue(by_id={}), ledger, SESSION, now=NOW)
        self.assertEqual(out["vanished"], 1)
        self.assertEqual(out["never_placed"], 0)
        self.assertEqual(out["integrity"], INTEGRITY_PARTIAL)
        self.assertGreater(out["exposure"]["reserved"], 0.0)

    def test_an_order_seen_once_is_never_marked_never_placed(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent())
        venue = Venue(by_cli={"cli_a": order(status="SUBMITTED", filled=0.0,
                                             avg=None)})
        poll_outstanding(venue, ledger, SESSION, now=NOW)
        venue._by_cli.clear()
        out = poll_outstanding(venue, ledger, SESSION, now=NOW)
        self.assertEqual(out["never_placed"], 0)
        self.assertEqual(out["vanished"], 1)
        self.assertNotEqual(ledger.get("cli_a").status, NEVER_PLACED)

    def test_a_filled_order_cannot_be_marked_never_placed(self):
        ledger = InMemoryOrderLedger()
        row = intent()
        row.filled_quantity = 0.5
        ledger.record_intent(row)
        with self.assertRaises(OrderLedgerError) as caught:
            ledger.record_never_placed("cli_a", "2026-10-02T18:00:00+00:00")
        self.assertIn("filled", str(caught.exception))


class TestIntegrityIsReportedNotAssumed(unittest.TestCase):

    def test_an_unreadable_ledger_makes_exposure_unknown(self):
        class Broken(InMemoryOrderLedger):
            def for_session(self, session_date):
                raise OrderLedgerError("table gone")

        out = poll_outstanding(Venue(), Broken(), SESSION, now=NOW)
        self.assertEqual(out["integrity"], INTEGRITY_UNKNOWN)
        self.assertFalse(out["exposure_known"])
        self.assertIsNone(out["exposure"], "unknown must not render as 0")

    def test_a_broker_with_no_lookup_is_unknown_not_a_clean_sweep(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent())
        out = poll_outstanding(NoLookupVenue(), ledger, SESSION, now=NOW)
        self.assertEqual(out["integrity"], INTEGRITY_UNKNOWN)
        self.assertEqual(out["polled"], 0)
        self.assertFalse(out["exposure_known"])

    def test_the_bound_is_reported_as_partial_not_complete(self):
        ledger = InMemoryOrderLedger()
        for i in range(5):
            ledger.record_intent(intent(cli=f"cli_{i}"))
        out = poll_outstanding(Venue(), ledger, SESSION, now=NOW,
                               max_orders=2)
        self.assertEqual(out["polled"], 2)
        self.assertEqual(out["integrity"], INTEGRITY_PARTIAL)
        self.assertTrue(any("not polled" in e for e in out["errors"]))

    def test_an_unstorable_observation_leaves_the_row_outstanding(self):
        class Stubborn(InMemoryOrderLedger):
            def record_observation(self, *_a, **_k):
                raise OrderLedgerError("write failed")

        ledger = Stubborn()
        ledger.record_intent(intent())
        out = poll_outstanding(Venue(by_cli={"cli_a": order()}), ledger,
                               SESSION, now=NOW)
        self.assertEqual(out["unresolved"], 1)
        self.assertEqual(out["integrity"], INTEGRITY_PARTIAL)
        self.assertEqual(out["outstanding"], 1)

    def test_the_oldest_pending_age_is_reported(self):
        ledger = InMemoryOrderLedger()
        ledger.record_intent(intent(cli="cli_new", age=30))
        ledger.record_intent(intent(cli="cli_old", age=3000))
        out = poll_outstanding(Venue(raises=RuntimeError("x")), ledger,
                               SESSION, now=NOW)
        self.assertGreaterEqual(out["oldest_pending_age_seconds"], 3000)

    def test_an_unsizeable_order_makes_exposure_unknown(self):
        ledger = InMemoryOrderLedger()
        row = intent(cli="cli_u")
        row.requested_notional = None
        row.requested_quantity = None
        ledger.record_intent(row)
        out = poll_outstanding(Venue(raises=RuntimeError("x")), ledger,
                               SESSION, now=NOW)
        self.assertFalse(out["exposure_known"])
        self.assertIn("cli_u", out["exposure"]["unestablished"])


class TestAnEmptySweepIsDistinctFromAnUnknownOne(unittest.TestCase):

    def test_nothing_outstanding_reports_complete_and_zero(self):
        out = poll_outstanding(Venue(), InMemoryOrderLedger(), SESSION,
                               now=NOW)
        self.assertEqual(out["integrity"], INTEGRITY_COMPLETE)
        self.assertEqual(out["polled"], 0)
        self.assertEqual(out["exposure"]["reserved"], 0.0)
        self.assertTrue(out["exposure_known"],
                        "a successful read proving nothing is outstanding "
                        "is knowledge, not ignorance")
