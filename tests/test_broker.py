"""
Paper broker and the execution gate.

The property that matters most: the simulation must not flatter the
strategy. A paper record produced by filling at the signal price is
worse than no record, because it will be compared with live results and
the gap will be blamed on the market.
"""
import copy
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    BrokerAdapter, ExecutionRefused, OrderRejected, OrderStatus, PaperBroker,
    PaperBrokerConfig, Quote, RejectReason, build_proposal, submit_approved,
)
from agent.broker.order_ledger import (                           # noqa: E402
    ExternalOrderRecord, InMemoryOrderLedger, OrderLedgerError,
)
from agent.hypothesis import generate                             # noqa: E402
from agent.risk import (                                          # noqa: E402
    RejectionCode, RiskContext, RiskLimits, evaluate,
)           # noqa: E402
from tests.test_hypothesis import catalyst, regime, signal        # noqa: E402

BID, ASK, MID = 99.90, 100.10, 100.00


def broker(cash=5000.0, partial=0.0, slippage=5.0, seed=7):
    b = PaperBroker(PaperBrokerConfig(starting_cash=cash, seed=seed,
                                      partial_fill_probability=partial,
                                      slippage_bps=slippage))
    b.set_quote(Quote(symbol="XYZ", bid=BID, ask=ASK, last=MID))
    return b


class TestFillRealism(unittest.TestCase):
    """The simulation must cost what trading costs."""

    def test_a_buy_lifts_the_ask_and_never_fills_at_the_mid(self):
        """
        Filling at the mid hands the strategy half the spread on entry
        and half on exit. On three round trips a day that is larger than
        most edges.
        """
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0)
        price = order["fills"][0]["price"]
        self.assertGreaterEqual(price, ASK)
        self.assertGreater(price, MID)

    def test_a_sell_hits_the_bid(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        order = b.close_position("XYZ")
        self.assertLessEqual(order["fills"][0]["price"], BID)

    def test_slippage_is_applied_beyond_the_spread(self):
        tight = broker(slippage=0.0).submit_order("XYZ", "BUY", 1.0)
        wide = broker(slippage=50.0).submit_order("XYZ", "BUY", 1.0)
        self.assertGreater(wide["fills"][0]["price"],
                           tight["fills"][0]["price"])

    def test_slippage_always_works_against_the_trader(self):
        b = broker(slippage=25.0)
        buy = b.submit_order("XYZ", "BUY", 1.0)
        sell = b.close_position("XYZ")
        self.assertGreater(buy["fills"][0]["price"], ASK)
        self.assertLess(sell["fills"][0]["price"], BID)

    def test_a_flat_round_trip_loses_money(self):
        """
        Buying and selling at an unchanged price must LOSE the spread
        plus slippage. A simulation where this breaks even is lying.
        """
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        b.close_position("XYZ")
        self.assertLess(b.get_account()["realized_pnl"], 0.0)

    def test_slippage_is_recorded_on_every_fill(self):
        order = broker().submit_order("XYZ", "BUY", 1.0)
        self.assertIsNotNone(order["fills"][0]["slippage_bps"])


class TestOrderLifecycle(unittest.TestCase):

    def test_a_filled_order_reaches_filled(self):
        order = broker().submit_order("XYZ", "BUY", 1.0)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["remaining_quantity"], 0.0)

    def test_partial_fills_are_possible(self):
        b = broker(partial=1.0)
        order = b.submit_order("XYZ", "BUY", 10.0)
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        self.assertGreater(order["remaining_quantity"], 0)
        self.assertGreater(order["filled_quantity"], 0)

    def test_an_open_order_can_be_cancelled(self):
        b = broker(partial=1.0)
        order = b.submit_order("XYZ", "BUY", 10.0)
        cancelled = b.cancel_order(order["order_id"])
        self.assertEqual(cancelled["status"], "CANCELLED")

    def test_a_terminal_order_cannot_be_cancelled(self):
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0)
        with self.assertRaises(OrderRejected):
            b.cancel_order(order["order_id"])

    def test_an_unknown_order_cannot_be_cancelled(self):
        with self.assertRaises(OrderRejected):
            broker().cancel_order("ord_nope")

    def test_replace_is_cancel_then_new(self):
        """Two orders in the audit trail, not one that changed shape."""
        b = broker(partial=1.0)
        first = b.submit_order("XYZ", "BUY", 10.0)
        second = b.replace_order(first["order_id"], quantity=2.0)
        self.assertNotEqual(first["order_id"], second["order_id"])
        self.assertEqual(b.get_order(first["order_id"])["status"], "CANCELLED")

    def test_day_orders_expire_at_the_close(self):
        b = broker(partial=1.0)
        b.submit_order("XYZ", "BUY", 10.0)
        expired = b.expire_day_orders()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["status"], "EXPIRED")

    def test_a_fill_or_kill_that_cannot_fill_fully_is_cancelled(self):
        b = broker(partial=1.0)
        order = b.submit_order("XYZ", "BUY", 10.0, time_in_force="FOK")
        self.assertEqual(order["status"], "CANCELLED")
        self.assertEqual(order["filled_quantity"], 0.0)

    def test_an_ioc_remainder_is_cancelled(self):
        b = broker(partial=1.0)
        order = b.submit_order("XYZ", "BUY", 10.0, time_in_force="IOC")
        self.assertEqual(order["status"], "CANCELLED")
        self.assertGreater(order["filled_quantity"], 0.0)

    def test_a_resting_limit_below_the_market_does_not_fill(self):
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0, order_type="LIMIT",
                               limit_price=90.0)
        self.assertEqual(order["status"], "SUBMITTED")
        self.assertEqual(order["filled_quantity"], 0.0)

    def test_a_marketable_limit_fills_immediately(self):
        order = broker().submit_order("XYZ", "BUY", 1.0,
                                      order_type="MARKETABLE_LIMIT")
        self.assertEqual(order["status"], "FILLED")


class TestRejections(unittest.TestCase):

    def test_market_closed(self):
        b = broker()
        b.set_market_open(False)
        order = b.submit_order("XYZ", "BUY", 1.0)
        self.assertEqual(order["status"], "REJECTED")
        self.assertEqual(order["reject_reason"], "MARKET_CLOSED")

    def test_no_quote(self):
        order = broker().submit_order("NOPE", "BUY", 1.0)
        self.assertEqual(order["reject_reason"], "NO_QUOTE")

    def test_insufficient_buying_power(self):
        order = broker(cash=50.0).submit_order("XYZ", "BUY", 100.0)
        self.assertEqual(order["reject_reason"], "INSUFFICIENT_BUYING_POWER")

    def test_shorting_is_refused(self):
        """Selling what is not held is shorting, whatever it is called."""
        order = broker().submit_order("XYZ", "SELL", 1.0)
        self.assertEqual(order["reject_reason"], "SHORTING_NOT_PERMITTED")

    def test_selling_more_than_held_is_refused(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        order = b.submit_order("XYZ", "SELL", 5.0)
        self.assertEqual(order["reject_reason"], "INSUFFICIENT_POSITION")

    def test_invalid_quantity(self):
        for quantity in (0, -1.0):
            with self.subTest(quantity=quantity):
                order = broker().submit_order("XYZ", "BUY", quantity)
                self.assertEqual(order["reject_reason"], "INVALID_QUANTITY")

    def test_a_limit_order_without_a_price_is_refused(self):
        order = broker().submit_order("XYZ", "BUY", 1.0, order_type="LIMIT")
        self.assertEqual(order["reject_reason"], "INVALID_PRICE")

    def test_a_rejection_is_recorded_not_raised(self):
        """
        A rejected order is a fact worth keeping: it is evidence about
        what the agent tried to do. Raising would tempt callers to
        discard it, and the rejection would never reach the journal.
        """
        b = broker()
        order = b.submit_order("NOPE", "BUY", 1.0)
        self.assertEqual(order["status"], "REJECTED")
        # The order must be retrievable from the SAME broker afterwards.
        stored = [o["order_id"] for o in b.get_orders()]
        self.assertIn(order["order_id"], stored)
        self.assertIsNotNone(b.get_order(order["order_id"]))


class TestCashAccounting(unittest.TestCase):

    def test_cash_falls_by_the_fill_value(self):
        b = broker(cash=5000.0)
        order = b.submit_order("XYZ", "BUY", 2.0)
        spent = order["fills"][0]["value"]
        self.assertAlmostEqual(b.get_account()["cash"], 5000.0 - spent,
                               places=2)

    def test_working_orders_reserve_cash(self):
        """
        Two orders must not be sized against the same dollar - the
        classic way a paper broker flatters itself.
        """
        b = broker(cash=200.0)
        b.submit_order("XYZ", "BUY", 1.0, order_type="LIMIT",
                       limit_price=90.0)          # rests, does not fill
        account = b.get_account()
        self.assertGreater(account["reserved_cash"], 0.0)
        self.assertLess(account["buying_power"], account["cash"])

    def test_cancelling_releases_the_reservation(self):
        b = broker(cash=200.0)
        order = b.submit_order("XYZ", "BUY", 1.0, order_type="LIMIT",
                               limit_price=90.0)
        b.cancel_order(order["order_id"])
        self.assertAlmostEqual(b.get_account()["reserved_cash"], 0.0, places=6)

    def test_realised_pnl_accrues_on_the_sell(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        self.assertEqual(b.get_account()["realized_pnl"], 0.0)
        b.set_quote(Quote(symbol="XYZ", bid=109.9, ask=110.1, last=110.0))
        b.close_position("XYZ")
        self.assertGreater(b.get_account()["realized_pnl"], 0.0)

    def test_equity_accounts_for_open_positions(self):
        b = broker(cash=5000.0)
        b.submit_order("XYZ", "BUY", 1.0)
        account = b.get_account()
        self.assertAlmostEqual(account["equity"],
                               account["cash"] + account["positions_value"],
                               places=4)

    def test_no_margin_buying_power_never_exceeds_cash(self):
        b = broker(cash=100.0)
        self.assertLessEqual(b.get_account()["buying_power"],
                             b.get_account()["cash"])


class TestPositions(unittest.TestCase):

    def test_a_buy_opens_a_position(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.5)
        position = b.get_position("XYZ")
        self.assertAlmostEqual(position["quantity"], 1.5, places=6)

    def test_fractional_quantities_are_supported(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 0.137)
        self.assertAlmostEqual(b.get_position("XYZ")["quantity"], 0.137,
                               places=6)

    def test_a_second_buy_averages_the_entry(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        b.set_quote(Quote(symbol="XYZ", bid=199.9, ask=200.1, last=200.0))
        b.submit_order("XYZ", "BUY", 1.0)
        entry = b.get_position("XYZ")["average_entry_price"]
        self.assertGreater(entry, 100.0)
        self.assertLess(entry, 200.2)

    def test_closing_removes_the_position(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        b.close_position("XYZ")
        self.assertIsNone(b.get_position("XYZ"))

    def test_unrealised_pnl_tracks_the_quote(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        b.set_quote(Quote(symbol="XYZ", bid=119.9, ask=120.1, last=120.0))
        self.assertGreater(b.get_position("XYZ")["unrealized_pnl"], 0.0)

    def test_closing_an_absent_position_raises(self):
        with self.assertRaises(OrderRejected):
            broker().close_position("XYZ")


class TestIdempotency(unittest.TestCase):

    def test_a_duplicate_client_order_id_does_not_place_a_second_order(self):
        """
        A retried Lambda invocation must not double the position. This
        is the single most expensive failure mode in an automated
        system.
        """
        b = broker()
        first = b.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        second = b.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertAlmostEqual(b.get_position("XYZ")["quantity"], 1.0,
                               places=6)

    def test_different_client_ids_place_different_orders(self):
        b = broker()
        first = b.submit_order("XYZ", "BUY", 1.0, client_order_id="a")
        second = b.submit_order("XYZ", "BUY", 1.0, client_order_id="b")
        self.assertNotEqual(first["order_id"], second["order_id"])


def approved_pair():
    """A hypothesis and an approved decision for it.

    Module level so that two test classes can share it without one
    inheriting the other - inheritance would silently re-run the parent
    class's tests under the child's name, which inflates the count and
    makes it unclear which class is actually covering what.
    """
    h = generate("XYZ", signal(), catalyst(), regime())
    ctx = RiskContext(session_date="2026-09-30", market_session="OPEN",
                      minutes_to_close=180.0, trading_enabled=True,
                      execution_available=True, price=100.0,
                      spread_pct=0.05, dollar_volume=5e8,
                      quote_age_seconds=10.0,
                      source_age_seconds=10.0,
                      feed_quality="REALTIME_SIP")
    return h, evaluate(h, ctx)


class TestExecutionGate(unittest.TestCase):
    """The only path from a decision to an order."""

    def _approved(self):
        return approved_pair()

    def test_execution_unavailable_refuses(self):
        h, d = self._approved()
        with self.assertRaises(ExecutionRefused):
            submit_approved(broker(), d, h, 100.0, execution_available=False)

    def test_an_unapproved_decision_refuses(self):
        h, _ = self._approved()
        rejected = evaluate(h, RiskContext(session_date="x",
                                           market_session="CLOSED"))
        with self.assertRaises(ExecutionRefused):
            submit_approved(broker(), rejected, h, 100.0,
                            execution_available=True)

    def test_a_mismatched_hypothesis_refuses(self):
        """
        An approval is for ONE hypothesis. Reusing it is how an approval
        for a good setup ends up executing a different one.
        """
        h, d = self._approved()
        other = generate("AAA", signal(), catalyst(), regime())
        with self.assertRaises(ExecutionRefused):
            submit_approved(broker(), d, other, 100.0,
                            execution_available=True)

    def test_an_approved_decision_reaches_the_broker(self):
        h, d = self._approved()
        b = broker()
        order = submit_approved(b, d, h, 100.0, execution_available=True)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["risk_decision_id"], d.decision_id)
        self.assertEqual(order["hypothesis_id"], h.hypothesis_id)

    def test_the_client_order_id_is_derived_from_the_decision(self):
        """So a retry submits the same id and cannot double the position."""
        _h, d = self._approved()
        first = build_proposal(d, 100.0)
        second = build_proposal(d, 100.0)
        self.assertEqual(first.client_order_id, second.client_order_id)

    def test_a_rebuilt_decision_yields_the_same_client_order_id(self):
        """
        The retry that matters happens in a NEW Lambda invocation, where
        the decision is deserialised from storage rather than reused.
        That is a different object carrying the same decision_id, so an
        id derived from object identity would silently stop deduping and
        the retry would double the position.
        """
        _h, d = self._approved()
        rebuilt = copy.deepcopy(d)
        self.assertIsNot(rebuilt, d)
        self.assertEqual(build_proposal(d, 100.0).client_order_id,
                         build_proposal(rebuilt, 100.0).client_order_id)

    def test_a_different_decision_yields_a_different_client_order_id(self):
        """The converse: two real decisions must not collide into one."""
        _h, first = self._approved()
        _h2, second = self._approved()
        self.assertNotEqual(first.decision_id, second.decision_id)
        self.assertNotEqual(build_proposal(first, 100.0).client_order_id,
                            build_proposal(second, 100.0).client_order_id)

    def test_the_submit_gate_checks_approval_itself(self):
        """
        `build_proposal` also refuses an unapproved decision, so the
        check inside `submit_approved` is a second layer that the normal
        path never reaches. Exercise it directly: a layer nothing tests
        reads as coverage without being any.
        """
        h, d = self._approved()
        unapproved = copy.deepcopy(d)
        unapproved.reason_codes = [RejectionCode.HYPOTHESIS_TOO_WEAK]
        self.assertFalse(unapproved.approved)
        with self.assertRaises(ExecutionRefused) as caught:
            submit_approved(broker(), unapproved, h, 100.0,
                            execution_available=True)
        # Pin WHICH layer refused. Asserting only that something was
        # raised would pass even with this gate removed, because
        # build_proposal refuses too - the assertion would read as
        # coverage of a check it never actually exercised.
        self.assertIn("risk decision is not approved", str(caught.exception))

    def test_a_missing_decision_is_refused_distinctly(self):
        h, _d = self._approved()
        with self.assertRaises(ExecutionRefused) as caught:
            submit_approved(broker(), None, h, 100.0,
                            execution_available=True)
        self.assertIn("no risk decision supplied", str(caught.exception))

    def test_a_retried_submission_does_not_double_the_position(self):
        h, d = self._approved()
        b = broker()
        submit_approved(b, d, h, 100.0, execution_available=True)
        quantity = b.get_position("XYZ")["quantity"]
        submit_approved(b, d, h, 100.0, execution_available=True)
        self.assertAlmostEqual(b.get_position("XYZ")["quantity"], quantity,
                               places=6)

    def test_a_proposal_cannot_be_built_from_a_rejection(self):
        h, _ = self._approved()
        rejected = evaluate(h, RiskContext(session_date="x",
                                           market_session="CLOSED"))
        with self.assertRaises(ExecutionRefused):
            build_proposal(rejected, 100.0)

    def test_the_notional_matches_the_approved_capital(self):
        _h, d = self._approved()
        proposal = build_proposal(d, 100.0)
        self.assertAlmostEqual(proposal.notional, d.capital_required,
                               places=6)

    def test_the_limit_price_bounds_how_bad_the_fill_may_be(self):
        _h, d = self._approved()
        proposal = build_proposal(d, 100.0)
        self.assertGreater(proposal.limit_price, 100.0)
        self.assertLess(proposal.limit_price, 101.0)


class TestCapabilities(unittest.TestCase):

    def test_paper_broker_declares_itself_paper(self):
        capabilities = broker().capabilities()
        self.assertTrue(capabilities["is_paper"])
        self.assertFalse(capabilities["shorting"])
        self.assertFalse(capabilities["margin"])
        self.assertFalse(capabilities["options"])

    def test_it_declares_the_costs_it_models(self):
        capabilities = broker().capabilities()
        self.assertTrue(capabilities["models_spread"])
        self.assertTrue(capabilities["models_slippage"])
        self.assertTrue(capabilities["models_partial_fills"])

    def test_market_orders_are_not_offered(self):
        """A market order accepts any price; the risk model rests on
        knowing the worst case."""
        self.assertNotIn("MARKET", broker().capabilities()["order_types"])

    def test_paper_broker_satisfies_the_adapter_interface(self):
        self.assertIsInstance(broker(), BrokerAdapter)


class TestDeterminism(unittest.TestCase):

    def test_the_same_seed_reproduces_the_same_fills(self):
        """An unseeded simulation cannot be compared with itself."""
        def run():
            b = broker(partial=0.5, seed=42)
            return [b.submit_order("XYZ", "BUY", 5.0)["filled_quantity"]
                    for _ in range(5)]
        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()


class _FakeExternalBroker:
    """An external venue, with the capabilities that matter here.

    `events` is shared with the ledger fake so that ORDERING can be
    asserted. Proving that both a write and a submit happened is not the
    property under test: the property is that the write happened FIRST,
    because the whole defect class lives in the window between them.
    """
    name = "fake_external"
    is_paper = True
    is_external_venue = True

    def __init__(self, events, lookup=None, submit_raises=None):
        self.events = events
        self.submits = []
        self._lookup = lookup
        self._submit_raises = submit_raises

    def submit_order(self, **kw):
        self.events.append("submit")
        self.submits.append(kw)
        if self._submit_raises:
            raise self._submit_raises
        return {"order_id": "brk_1", "client_order_id": kw["client_order_id"],
                "status": "FILLED", "raw_status": "filled",
                "filled_quantity": kw["quantity"], "remaining_quantity": 0.0,
                "average_fill_price": 100.05,
                "submitted_at": "2026-10-02T13:30:00+00:00"}

    def find_by_client_order_id(self, client_order_id):
        self.events.append("lookup")
        if self._lookup is None:
            return None
        if isinstance(self._lookup, Exception):
            raise self._lookup
        return self._lookup


class _RecordingLedger(InMemoryOrderLedger):
    def __init__(self, events, intent_raises=None, obs_raises=None,
                 get_raises=None):
        super().__init__()
        self.events = events
        self._intent_raises = intent_raises
        self._obs_raises = obs_raises
        self._get_raises = get_raises

    def get(self, client_order_id):
        if self._get_raises:
            raise self._get_raises
        return super().get(client_order_id)

    def record_intent(self, record):
        if self._intent_raises:
            raise self._intent_raises
        self.events.append("intent")
        return super().record_intent(record)

    def record_observation(self, client_order_id, order, observed_at):
        if self._obs_raises:
            raise self._obs_raises
        self.events.append("observation")
        return super().record_observation(client_order_id, order, observed_at)


class TestIntentBeforeSubmit(unittest.TestCase):
    """An external order is durably recorded before it is sent.

    The orphan on 2026-10-02 existed because nothing in this system had
    any record of asking for the order that filled. These tests are about
    the window that produced it.
    """

    def _approved(self):
        return approved_pair()

    def _submit(self, broker_obj, ledger, hd=None, **kw):
        # `hd` is passed when the test has already seeded a ledger row
        # for this decision. _approved() mints a new decision_id each
        # call, and since the client order id is derived from it, calling
        # it twice would produce a different id and the seeded row would
        # never be found - the test would pass for the wrong reason.
        h, d = hd if hd is not None else self._approved()
        return submit_approved(broker_obj, d, h, 100.0,
                               execution_available=True, ledger=ledger,
                               session_date="2026-10-02",
                               cohort="TEST_COHORT", **kw)

    # --- the ordering property ------------------------------------------

    def test_the_intent_is_recorded_before_the_order_is_sent(self):
        events = []
        b = _FakeExternalBroker(events)
        self._submit(b, _RecordingLedger(events))
        self.assertEqual(events, ["intent", "submit", "observation"])
        self.assertLess(events.index("intent"), events.index("submit"))

    def test_a_failed_intent_write_prevents_the_submission(self):
        events = []
        b = _FakeExternalBroker(events)
        ledger = _RecordingLedger(events,
                                  intent_raises=OrderLedgerError("table gone"))
        with self.assertRaises(ExecutionRefused) as caught:
            self._submit(b, ledger)
        self.assertIn("NOT submitted", str(caught.exception))
        # The property: nothing reached the venue.
        self.assertEqual(b.submits, [])
        self.assertNotIn("submit", events)

    def test_an_unreadable_ledger_prevents_the_submission(self):
        events = []
        b = _FakeExternalBroker(events)
        ledger = _RecordingLedger(events,
                                  get_raises=OrderLedgerError("read failed"))
        with self.assertRaises(ExecutionRefused) as caught:
            self._submit(b, ledger)
        self.assertIn("could not be read", str(caught.exception))
        self.assertEqual(b.submits, [])

    # --- the ledger is not optional for an external venue ---------------

    def test_an_external_broker_without_a_ledger_refuses(self):
        events = []
        b = _FakeExternalBroker(events)
        h, d = self._approved()
        with self.assertRaises(ExecutionRefused) as caught:
            submit_approved(b, d, h, 100.0, execution_available=True,
                            ledger=None, session_date="2026-10-02")
        self.assertIn("ledger", str(caught.exception))
        self.assertEqual(b.submits, [])

    def test_an_external_broker_without_a_session_date_refuses(self):
        events = []
        b = _FakeExternalBroker(events)
        h, d = self._approved()
        with self.assertRaises(ExecutionRefused):
            submit_approved(b, d, h, 100.0, execution_available=True,
                            ledger=_RecordingLedger(events), session_date=None)
        self.assertEqual(b.submits, [])

    def test_the_simulator_still_needs_no_ledger(self):
        """The in-process broker serialises its own orders, so requiring
        a ledger of it would be ceremony rather than safety."""
        h, d = self._approved()
        order = submit_approved(broker(), d, h, 100.0,
                                execution_available=True)
        self.assertIsNotNone(order.get("client_order_id"))

    # --- what the record says afterwards --------------------------------

    def test_the_record_carries_the_intent_and_then_the_observation(self):
        events = []
        b = _FakeExternalBroker(events)
        ledger = _RecordingLedger(events)
        order = self._submit(b, ledger)
        row = ledger.get(order["client_order_id"])
        self.assertEqual(row.session_date, "2026-10-02")
        self.assertEqual(row.cohort, "TEST_COHORT")
        self.assertEqual(row.requested_quantity, b.submits[0]["quantity"])
        self.assertIsNotNone(row.risk_decision_id)
        self.assertIsNotNone(row.intent_at)
        self.assertTrue(row.submission_outcome_known)
        self.assertEqual(row.status, "FILLED")
        self.assertEqual(row.broker_order_id, "brk_1")

    def test_an_intent_is_written_even_when_the_submission_raises(self):
        """The crash case. The order may or may not exist at the venue,
        and the record is what makes finding out possible at all."""
        events = []
        b = _FakeExternalBroker(events, submit_raises=RuntimeError("boom"))
        ledger = _RecordingLedger(events)
        with self.assertRaises(RuntimeError):
            self._submit(b, ledger)
        rows = ledger.for_session("2026-10-02")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].submission_outcome_known)
        # Not terminal, so it keeps reserving exposure until resolved.
        self.assertFalse(rows[0].is_terminal)

    def test_a_lost_observation_does_not_unsend_the_order(self):
        """The order exists. Raising here would report a failure for an
        order that was accepted, and the intent record already makes it
        recoverable by polling."""
        events = []
        b = _FakeExternalBroker(events)
        ledger = _RecordingLedger(events,
                                  obs_raises=OrderLedgerError("write failed"))
        order = self._submit(b, ledger)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(len(b.submits), 1)

    # --- lost-response recovery -----------------------------------------

    def test_an_order_the_venue_already_has_is_not_sent_again(self):
        events = []
        existing = {"order_id": "brk_prior", "status": "FILLED",
                    "raw_status": "filled", "filled_quantity": 1.0,
                    "average_fill_price": 99.0}
        b = _FakeExternalBroker(events, lookup=existing)
        ledger = _RecordingLedger(events)
        h, d = self._approved()
        cli = build_proposal(d, 100.0).client_order_id
        ledger.record_intent(ExternalOrderRecord(
            client_order_id=cli, session_date="2026-10-02",
            symbol=d.symbol, side="BUY", requested_notional=100.0))
        order = self._submit(b, ledger, hd=(h, d))
        self.assertEqual(order["order_id"], "brk_prior")
        self.assertEqual(b.submits, [])          # the whole point
        self.assertNotIn("submit", events)
        self.assertTrue(ledger.get(cli).submission_outcome_known)

    def test_a_confirmed_absence_resends_the_same_client_id(self):
        """A confirmed absence means the earlier attempt never reached
        the venue. Resending is correct, and it must reuse the same
        logical id so the venue's own idempotency still applies."""
        events = []
        b = _FakeExternalBroker(events, lookup=None)
        ledger = _RecordingLedger(events)
        h, d = self._approved()
        cli = build_proposal(d, 100.0).client_order_id
        ledger.record_intent(ExternalOrderRecord(
            client_order_id=cli, session_date="2026-10-02",
            symbol=d.symbol, side="BUY", requested_notional=100.0))
        order = self._submit(b, ledger, hd=(h, d))
        self.assertEqual(len(b.submits), 1)
        self.assertEqual(b.submits[0]["client_order_id"], cli)
        self.assertEqual(order["client_order_id"], cli)

    def test_an_unanswerable_lookup_refuses_rather_than_risking_a_double(self):
        events = []
        b = _FakeExternalBroker(events, lookup=RuntimeError("venue down"))
        ledger = _RecordingLedger(events)
        h, d = self._approved()
        cli = build_proposal(d, 100.0).client_order_id
        ledger.record_intent(ExternalOrderRecord(
            client_order_id=cli, session_date="2026-10-02",
            symbol=d.symbol, side="BUY", requested_notional=100.0))
        with self.assertRaises(ExecutionRefused) as caught:
            self._submit(b, ledger, hd=(h, d))
        self.assertIn("could not be queried", str(caught.exception))
        self.assertEqual(b.submits, [])

    def test_a_venue_with_no_lookup_refuses_to_resubmit_an_observed_order(self):
        events = []

        class _NoLookup(_FakeExternalBroker):
            find_by_client_order_id = None

        b = _NoLookup(events)
        ledger = _RecordingLedger(events)
        h, d = self._approved()
        cli = build_proposal(d, 100.0).client_order_id
        row = ExternalOrderRecord(
            client_order_id=cli, session_date="2026-10-02",
            symbol=d.symbol, side="BUY", requested_notional=100.0)
        row.submission_outcome_known = True
        row.status = "FILLED"
        ledger.record_intent(row)
        with self.assertRaises(ExecutionRefused) as caught:
            self._submit(b, ledger, hd=(h, d))
        self.assertIn("already been submitted", str(caught.exception))
        self.assertEqual(b.submits, [])

    # --- the marker itself ----------------------------------------------

    def test_the_alpaca_adapter_declares_itself_external(self):
        from agent.broker.alpaca_paper import AlpacaPaperBroker
        self.assertTrue(AlpacaPaperBroker.is_external_venue)

    def test_the_simulator_does_not_declare_itself_external(self):
        self.assertFalse(PaperBroker.is_external_venue)

    def test_a_new_adapter_defaults_to_not_external(self):
        """The default has to be the laxer path's opposite: a new
        adapter must opt IN to being trusted as in-process."""
        self.assertFalse(BrokerAdapter.is_external_venue)
