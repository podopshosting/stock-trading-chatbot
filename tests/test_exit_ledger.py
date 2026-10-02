"""
An exit order is as durable as an entry order.

Until now it was not. Exits went through `broker.close_position`, which
on Alpaca is DELETE /v2/positions/{symbol} - an endpoint that accepts no
client order id at all. That made an exit impossible to make idempotent:
if the response was lost there was no handle to ask the venue about, so
the only choices were to leave the position open or risk closing it
twice. An exit now goes out as a deterministic SELL through the same
chokepoint an entry uses.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker.execution import (                              # noqa: E402
    ExecutionRefused, MAX_EXIT_ATTEMPTS, build_exit_proposal,
    exit_client_order_id, exit_intent_name, submit_exit,
)
from agent.broker.order_ledger import (                           # noqa: E402
    InMemoryOrderLedger, OrderLedgerError,
)
from agent.broker.provenance import (                             # noqa: E402
    DEFAULT_INTENTS, client_order_id_for,
)

DECISION = "risk_exittest01"
SESSION = "2026-10-02"


class Venue:
    name = "fake_external"
    is_paper = True
    is_external_venue = True

    def __init__(self, events=None, existing=None, fill=1.0):
        self.events = events if events is not None else []
        self.submits = []
        self._existing = existing
        self._fill = fill

    def submit_order(self, **kw):
        self.events.append("submit")
        self.submits.append(kw)
        filled = kw["quantity"] * self._fill
        return {"order_id": f"brk_{len(self.submits)}",
                "client_order_id": kw["client_order_id"],
                "status": "FILLED" if self._fill >= 1 else "PARTIALLY_FILLED",
                "raw_status": "filled" if self._fill >= 1
                else "partially_filled",
                "filled_quantity": filled,
                "remaining_quantity": kw["quantity"] - filled,
                "average_fill_price": 61.0,
                "submitted_at": "2026-10-02T19:00:00+00:00"}

    def find_by_client_order_id(self, cli):
        self.events.append("lookup")
        return self._existing


class RecordingLedger(InMemoryOrderLedger):
    def __init__(self, events, intent_raises=None):
        super().__init__()
        self.events = events
        self._intent_raises = intent_raises

    def record_intent(self, record):
        if self._intent_raises:
            raise self._intent_raises
        self.events.append("intent")
        return super().record_intent(record)

    def record_observation(self, cli, order, observed_at):
        self.events.append("observation")
        return super().record_observation(cli, order, observed_at)


class TestTheExitIdIsDeterministic(unittest.TestCase):

    def test_the_first_attempt_matches_the_provenance_reconstruction(self):
        """Adoption has to be able to recognise an exit order as the
        agent's own, which means both sides must derive the same id."""
        self.assertEqual(exit_client_order_id(DECISION, 1),
                         client_order_id_for(DECISION, "EXIT"))

    def test_later_attempts_match_their_suffixed_intents(self):
        for attempt in (2, 3):
            with self.subTest(attempt=attempt):
                self.assertEqual(
                    exit_client_order_id(DECISION, attempt),
                    client_order_id_for(DECISION, f"EXIT#{attempt}"))

    def test_every_exit_intent_is_recognised_by_provenance(self):
        """A suffixed intent missing from DEFAULT_INTENTS would mean a
        position closed on a second attempt could not be proved ours."""
        for attempt in range(1, MAX_EXIT_ATTEMPTS + 1):
            self.assertIn(exit_intent_name(attempt), DEFAULT_INTENTS)

    def test_an_exit_id_differs_from_the_entry_id(self):
        self.assertNotEqual(exit_client_order_id(DECISION, 1),
                            client_order_id_for(DECISION, "ENTRY"))

    def test_a_retry_of_one_attempt_reuses_its_id(self):
        self.assertEqual(exit_client_order_id(DECISION, 1),
                         exit_client_order_id(DECISION, 1))

    def test_attempts_do_not_collide(self):
        ids = {exit_client_order_id(DECISION, a)
               for a in range(1, MAX_EXIT_ATTEMPTS + 1)}
        self.assertEqual(len(ids), MAX_EXIT_ATTEMPTS)

    def test_no_decision_id_is_refused_rather_than_randomised(self):
        for missing in (None, ""):
            with self.subTest(value=missing):
                with self.assertRaises(ExecutionRefused) as caught:
                    exit_client_order_id(missing, 1)
                self.assertIn("idempotent", str(caught.exception))

    def test_an_attempt_beyond_the_bound_is_refused(self):
        with self.assertRaises(ExecutionRefused):
            build_exit_proposal("XYZ", 1.0, 100.0, DECISION,
                                attempt=MAX_EXIT_ATTEMPTS + 1)


class TestTheExitProposal(unittest.TestCase):

    def test_the_limit_reaches_down_through_the_spread(self):
        """An exit that does not fill is worse than one that pays a
        little, because the position stays live."""
        proposal = build_exit_proposal("XYZ", 1.0, 100.0, DECISION)
        self.assertEqual(proposal.side, "SELL")
        self.assertLess(proposal.limit_price, 100.0)

    def test_a_non_positive_quantity_is_refused(self):
        for bad in (0, -1.0, None):
            with self.subTest(quantity=bad):
                with self.assertRaises(ExecutionRefused):
                    build_exit_proposal("XYZ", bad, 100.0, DECISION)

    def test_a_missing_reference_price_is_refused(self):
        with self.assertRaises(ExecutionRefused):
            build_exit_proposal("XYZ", 1.0, None, DECISION)


class TestIntentBeforeSubmitAppliesToExitsToo(unittest.TestCase):

    def _exit(self, venue, ledger, **kw):
        return submit_exit(venue, symbol="XYZ", quantity=1.0,
                           reference_price=100.0, risk_decision_id=DECISION,
                           ledger=ledger, session_date=SESSION, **kw)

    def test_the_intent_is_recorded_before_the_exit_is_sent(self):
        events = []
        venue = Venue(events)
        self._exit(venue, RecordingLedger(events))
        self.assertEqual(events, ["intent", "submit", "observation"])

    def test_a_failed_intent_write_prevents_the_exit_submission(self):
        """Fails CLOSED, which for an exit means the position stays
        open and is reported - not that it is closed untracked."""
        events = []
        venue = Venue(events)
        ledger = RecordingLedger(events,
                                 intent_raises=OrderLedgerError("table gone"))
        with self.assertRaises(ExecutionRefused):
            self._exit(venue, ledger)
        self.assertEqual(venue.submits, [])

    def test_an_external_exit_without_a_ledger_is_refused(self):
        with self.assertRaises(ExecutionRefused) as caught:
            submit_exit(Venue(), symbol="XYZ", quantity=1.0,
                        reference_price=100.0, risk_decision_id=DECISION,
                        ledger=None, session_date=SESSION)
        self.assertIn("ledger", str(caught.exception))

    def test_an_external_exit_without_a_session_date_is_refused(self):
        with self.assertRaises(ExecutionRefused):
            submit_exit(Venue(), symbol="XYZ", quantity=1.0,
                        reference_price=100.0, risk_decision_id=DECISION,
                        ledger=InMemoryOrderLedger(), session_date=None)

    def test_the_record_carries_the_exit_intent(self):
        events = []
        ledger = RecordingLedger(events)
        order = self._exit(Venue(events), ledger)
        row = ledger.get(order["client_order_id"])
        self.assertEqual(row.intent, "EXIT")
        self.assertEqual(row.side, "SELL")
        self.assertEqual(row.status, "FILLED")
        self.assertEqual(row.risk_decision_id, DECISION)

    def test_a_lost_exit_response_is_recovered_not_resubmitted(self):
        """The property DELETE /v2/positions could never provide."""
        events = []
        existing = {"order_id": "brk_prior", "status": "FILLED",
                    "raw_status": "filled", "filled_quantity": 1.0,
                    "average_fill_price": 61.0}
        venue = Venue(events, existing=existing)
        ledger = RecordingLedger(events)
        cli = exit_client_order_id(DECISION, 1)
        from agent.broker.order_ledger import ExternalOrderRecord
        ledger.record_intent(ExternalOrderRecord(
            client_order_id=cli, session_date=SESSION, symbol="XYZ",
            side="SELL", requested_quantity=1.0, intent="EXIT"))
        order = self._exit(venue, ledger)
        self.assertEqual(order["order_id"], "brk_prior")
        self.assertEqual(venue.submits, [],
                         "a second exit order would sell twice")

    def test_a_partially_filled_exit_leaves_a_remainder_recorded(self):
        events = []
        ledger = RecordingLedger(events)
        order = submit_exit(Venue(events, fill=0.4), symbol="XYZ",
                            quantity=1.0, reference_price=100.0,
                            risk_decision_id=DECISION, ledger=ledger,
                            session_date=SESSION)
        row = ledger.get(order["client_order_id"])
        self.assertEqual(row.filled_quantity, 0.4)
        self.assertFalse(row.is_terminal,
                         "a partially filled exit is not finished")
        self.assertGreater(row.potential_exposure, 0.0)

    def test_the_remainder_is_closed_under_a_distinguishable_id(self):
        """A remainder is a NEW logical order. Reusing the first id
        would be suppressed by the venue and the remainder would stay
        open."""
        events = []
        ledger = RecordingLedger(events)
        venue = Venue(events, fill=0.4)
        first = submit_exit(venue, symbol="XYZ", quantity=1.0,
                            reference_price=100.0, risk_decision_id=DECISION,
                            ledger=ledger, session_date=SESSION, attempt=1)
        second = submit_exit(venue, symbol="XYZ", quantity=0.6,
                             reference_price=100.0, risk_decision_id=DECISION,
                             ledger=ledger, session_date=SESSION, attempt=2)
        self.assertNotEqual(first["client_order_id"],
                            second["client_order_id"])
        self.assertEqual(len(venue.submits), 2)

    def test_the_simulator_needs_no_ledger_for_an_exit(self):
        class Internal(Venue):
            is_external_venue = False

        venue = Internal()
        order = submit_exit(venue, symbol="XYZ", quantity=1.0,
                            reference_price=100.0,
                            risk_decision_id=DECISION)
        self.assertEqual(order["status"], "FILLED")


class TestRestartRecoveryForExits(unittest.TestCase):
    """A fresh process must reconstruct a pending exit from the ledger
    and the venue, with nothing held in memory."""

    def test_a_pending_exit_is_visible_to_a_new_process(self):
        from agent.broker.order_poller import poll_outstanding
        ledger = InMemoryOrderLedger()
        venue = Venue(fill=0.0)
        submit_exit(venue, symbol="XYZ", quantity=1.0, reference_price=100.0,
                    risk_decision_id=DECISION, ledger=ledger,
                    session_date=SESSION)
        cli = exit_client_order_id(DECISION, 1)

        # A new process: a different poller, the same durable ledger.
        outstanding = ledger.non_terminal(SESSION)
        self.assertEqual([r.client_order_id for r in outstanding], [cli])
        self.assertEqual(outstanding[0].intent, "EXIT")

        venue._existing = {"order_id": "brk_1", "status": "FILLED",
                           "raw_status": "filled", "filled_quantity": 1.0,
                           "average_fill_price": 61.0}
        out = poll_outstanding(venue, ledger, SESSION)
        self.assertEqual(out["observed"], 1)
        self.assertEqual(out["became_terminal"], 1)
        self.assertEqual(ledger.get(cli).status, "FILLED")

    def test_an_exit_whose_outcome_is_unknown_reserves_exposure(self):
        ledger = InMemoryOrderLedger()
        venue = Venue(fill=0.0)
        submit_exit(venue, symbol="XYZ", quantity=1.0, reference_price=100.0,
                    risk_decision_id=DECISION, ledger=ledger,
                    session_date=SESSION)
        from agent.broker.order_ledger import committed_exposure
        exposure = committed_exposure(ledger.non_terminal(SESSION))
        self.assertTrue(exposure["known"])
        self.assertGreater(exposure["reserved"], 0.0)
