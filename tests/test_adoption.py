"""
Taking back a position the agent created and did not record.

The real case drives these tests. On 2026-10-02 the agent bought
0.48247 DRAM at 62.204 under client order id cli_1d1d1063b70a461b9e9e,
the fill arrived asynchronously, and the position existed only at the
venue. The agent halted correctly and then could not close what it had
bought.

The property that matters most is the one that is easy to get wrong in
the permissive direction: a position that cannot be PROVEN to be the
agent's own is never adopted and never closed.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import PaperBroker, PaperBrokerConfig, Quote  # noqa: E402
from agent.broker.order_ledger import (                          # noqa: E402
    ExternalOrderRecord, InMemoryOrderLedger, OrderLedgerError,
)
from agent.broker.provenance import client_order_id_for           # noqa: E402
from agent.positions import PositionManager                       # noqa: E402
from agent.positions.adoption import (                            # noqa: E402
    INTEGRITY_COMPLETE, INTEGRITY_PARTIAL, INTEGRITY_UNKNOWN,
    PLAN_ORIGINAL, PLAN_RECONSTRUCTED, adopt_external_positions,
    build_adoption_plan,
)

SESSION = "2026-10-02"

# The real numbers.
DRAM_DECISION = "risk_adef08c5015b"
DRAM_CLI = "cli_1d1d1063b70a461b9e9e"
DRAM_QTY = 0.48247
DRAM_ENTRY = 62.204


def dram_position():
    return {"symbol": "DRAM", "quantity": DRAM_QTY,
            "average_entry_price": DRAM_ENTRY, "market_value": 29.65,
            "unrealized_pnl": -0.364}


class Venue:
    """Only the read surface adoption is allowed to use."""

    def __init__(self, positions=None, orders=None,
                 positions_raise=None, orders_raise=None):
        self._positions = positions
        self._orders = orders if orders is not None else []
        self._positions_raise = positions_raise
        self._orders_raise = orders_raise

    def get_positions(self):
        if self._positions_raise:
            raise self._positions_raise
        return self._positions

    def get_orders(self, status=None):
        if self._orders_raise:
            raise self._orders_raise
        return self._orders


class Decisions:
    def __init__(self, rows=None, raises=None):
        self._rows = rows or []
        self._raises = raises

    def for_session(self, session_date, symbol=None):
        if self._raises:
            raise self._raises
        return self._rows


def manager():
    broker = PaperBroker(PaperBrokerConfig(starting_cash=1000.0, seed=3))
    broker.set_quote(Quote(symbol="DRAM", bid=61.0, ask=61.2, last=61.1))
    return PositionManager(broker=broker, execution_available=True)


def decision_row(decision_id=DRAM_DECISION, stop_pct=None, symbol="DRAM"):
    return {"symbol": symbol,
            "risk": {"decision_id": decision_id, "approved": True,
                     "stop_distance_pct": stop_pct}}


class TestTheRealDramPosition(unittest.TestCase):
    """End to end on the actual incident."""

    def _adopt(self, **kw):
        mgr = kw.pop("manager", None) or manager()
        venue = Venue(positions=[dram_position()],
                      orders=[{"symbol": "DRAM", "id": "d7eaecdd",
                               "client_order_id": DRAM_CLI,
                               "status": "filled"}])
        out = adopt_external_positions(
            venue, mgr, session_date=SESSION,
            decisions=Decisions([decision_row()]), **kw)
        return mgr, out

    def test_the_venue_id_is_reproduced_from_the_stored_decision(self):
        """The evidence, restated as a test: only something holding
        risk_adef08c5015b could produce this string."""
        self.assertEqual(client_order_id_for(DRAM_DECISION, "ENTRY"),
                         DRAM_CLI)

    def test_dram_is_adopted_and_becomes_managed(self):
        mgr, out = self._adopt()
        self.assertEqual(out["adopted"], 1)
        self.assertEqual(out["adopted_symbols"], ["DRAM"])
        self.assertEqual(out["unknown_origin"], [])
        self.assertEqual(out["preexisting"], [])
        self.assertEqual(out["integrity"], INTEGRITY_COMPLETE)
        self.assertFalse(out["blocks_new_exposure"])
        held = {p.symbol: p for p in mgr.open_positions()}
        self.assertIn("DRAM", held)

    def test_the_adopted_quantity_equals_the_brokers(self):
        mgr, _out = self._adopt()
        position = next(p for p in mgr.open_positions() if p.symbol == "DRAM")
        self.assertEqual(position.quantity, DRAM_QTY)

    def test_the_evidence_is_the_reconstruction_not_a_prefix(self):
        _mgr, out = self._adopt()
        self.assertEqual(out["classifications"][0]["evidence"],
                         "RECONSTRUCTED_CLIENT_ID")
        self.assertTrue(out["classifications"][0]["proven"])

    def test_adoption_is_idempotent(self):
        """Repeated discovery must not create a second managed record,
        a second exit intent or a second journal event."""
        mgr, first = self._adopt()
        self.assertEqual(first["adopted"], 1)
        _mgr2, second = self._adopt(manager=mgr)
        self.assertEqual(second["adopted"], 0)
        self.assertEqual(second["already_managed"], 1)
        self.assertEqual(
            len([p for p in mgr.open_positions() if p.symbol == "DRAM"]), 1)

    def test_the_reconstructed_stop_bounds_the_loss_to_the_risk_limit(self):
        mgr, _out = self._adopt(max_trade_risk=2.0)
        position = next(p for p in mgr.open_positions() if p.symbol == "DRAM")
        loss = (DRAM_ENTRY - position.plan.stop_price) * DRAM_QTY
        self.assertAlmostEqual(loss, 2.0, places=3)
        self.assertLess(position.plan.stop_price, DRAM_ENTRY)

    def test_the_ledger_alone_also_proves_it(self):
        """Either route to proof is enough; the ledger is the stronger
        one because the agent wrote it itself."""
        mgr = manager()
        ledger = InMemoryOrderLedger()
        row = ExternalOrderRecord(
            client_order_id=DRAM_CLI, session_date=SESSION, symbol="DRAM",
            side="BUY", requested_quantity=DRAM_QTY,
            requested_notional=30.02, risk_decision_id=DRAM_DECISION)
        row.filled_quantity = DRAM_QTY
        row.average_fill_price = DRAM_ENTRY
        ledger.record_intent(row)
        out = adopt_external_positions(
            Venue(positions=[dram_position()], orders=[]), mgr,
            session_date=SESSION, ledger=ledger)
        self.assertEqual(out["adopted"], 1)
        self.assertEqual(out["classifications"][0]["evidence"],
                         "LEDGER_RECORD")


class TestWhatMustNeverBeAdopted(unittest.TestCase):

    def test_a_prefix_match_alone_is_not_adopted(self):
        """`adoptable` is True here and `proven` is False. A prefix is a
        naming convention; any client could choose the same one."""
        mgr = manager()
        out = adopt_external_positions(
            Venue(positions=[dram_position()],
                  orders=[{"symbol": "DRAM", "id": "x",
                           "client_order_id": "cli_somethingelse00000",
                           "status": "filled"}]),
            mgr, session_date=SESSION, decisions=Decisions([]))
        self.assertEqual(out["adopted"], 0)
        self.assertEqual(out["unknown_origin"], ["DRAM"])
        self.assertTrue(out["blocks_new_exposure"])
        self.assertEqual(out["classifications"][0]["evidence"],
                         "CLIENT_ID_PREFIX_ONLY")
        self.assertTrue(out["classifications"][0]["adoptable"],
                        "the weaker flag is set, which is exactly why "
                        "adoption keys on `proven` instead")
        self.assertFalse(out["classifications"][0]["proven"])
        self.assertEqual(list(mgr.open_positions()), [])

    def test_a_preexisting_position_is_neither_adopted_nor_closed(self):
        mgr = manager()
        venue = Venue(positions=[{"symbol": "FOREIGN", "quantity": 5.0,
                                  "average_entry_price": 10.0}],
                      orders=[])
        out = adopt_external_positions(venue, mgr, session_date=SESSION,
                                       decisions=Decisions([]))
        self.assertEqual(out["adopted"], 0)
        self.assertEqual(out["preexisting"], ["FOREIGN"])
        self.assertFalse(out["blocks_new_exposure"],
                         "somebody else's position is left alone; it does "
                         "not by itself stop the agent trading")
        self.assertEqual(list(mgr.open_positions()), [])

    def test_an_unreadable_order_history_is_unknown_not_preexisting(self):
        """Not knowing who opened a position is not evidence that
        somebody else did."""
        mgr = manager()
        out = adopt_external_positions(
            Venue(positions=[dram_position()],
                  orders_raise=RuntimeError("history unreadable")),
            mgr, session_date=SESSION)
        self.assertEqual(out["adopted"], 0)
        self.assertEqual(out["unknown_origin"], ["DRAM"])
        self.assertEqual(out["preexisting"], [])
        self.assertTrue(out["blocks_new_exposure"])
        self.assertEqual(out["integrity"], INTEGRITY_PARTIAL)

    def test_unreadable_broker_positions_block_new_exposure(self):
        out = adopt_external_positions(
            Venue(positions_raise=RuntimeError("down")), manager(),
            session_date=SESSION)
        self.assertEqual(out["integrity"], INTEGRITY_UNKNOWN)
        self.assertTrue(out["blocks_new_exposure"])
        self.assertEqual(out["discovered"], 0)

    def test_a_none_position_list_is_unknown_not_empty(self):
        out = adopt_external_positions(Venue(positions=None), manager(),
                                       session_date=SESSION)
        self.assertEqual(out["integrity"], INTEGRITY_UNKNOWN)
        self.assertTrue(out["blocks_new_exposure"])

    def test_a_position_with_no_viable_stop_is_refused_not_mismanaged(self):
        """If no positive stop bounds the loss to the configured limit,
        adopting under a wider stop would quietly exceed that limit."""
        mgr = manager()
        venue = Venue(positions=[{"symbol": "DRAM", "quantity": 0.001,
                                  "average_entry_price": 62.204}],
                      orders=[{"symbol": "DRAM", "id": "d",
                               "client_order_id": DRAM_CLI,
                               "status": "filled"}])
        out = adopt_external_positions(
            venue, mgr, session_date=SESSION,
            decisions=Decisions([decision_row()]), max_trade_risk=2.0)
        self.assertEqual(out["adopted"], 0)
        self.assertEqual(len(out["refused"]), 1)
        self.assertTrue(out["blocks_new_exposure"])
        self.assertEqual(list(mgr.open_positions()), [])


class TestTheStopPlanIsLabelled(unittest.TestCase):

    def test_a_recovered_stop_distance_is_used_and_labelled(self):
        built = build_adoption_plan(100.0, 1.0, 2.0, stop_distance_pct=3.0)
        self.assertEqual(built["stop_origin"], PLAN_ORIGINAL)
        self.assertAlmostEqual(built["plan"].stop_price, 97.0)

    def test_a_reconstructed_stop_is_labelled_differently(self):
        built = build_adoption_plan(100.0, 1.0, 2.0)
        self.assertEqual(built["stop_origin"], PLAN_RECONSTRUCTED)
        self.assertAlmostEqual(built["plan"].stop_price, 98.0)

    def test_the_stored_decision_is_preferred_over_the_limit(self):
        mgr = manager()
        venue = Venue(positions=[dram_position()],
                      orders=[{"symbol": "DRAM", "id": "d",
                               "client_order_id": DRAM_CLI,
                               "status": "filled"}])
        adopt_external_positions(
            venue, mgr, session_date=SESSION,
            decisions=Decisions([decision_row(stop_pct=1.5)]),
            max_trade_risk=2.0)
        position = next(p for p in mgr.open_positions() if p.symbol == "DRAM")
        self.assertAlmostEqual(position.plan.stop_price,
                               round(DRAM_ENTRY * (1 - 1.5 / 100.0), 4))

    def test_a_zero_or_negative_stored_distance_is_not_used(self):
        for bad in (0, -3.0, "nonsense", None):
            with self.subTest(stored=bad):
                mgr = manager()
                venue = Venue(positions=[dram_position()],
                              orders=[{"symbol": "DRAM", "id": "d",
                                       "client_order_id": DRAM_CLI,
                                       "status": "filled"}])
                out = adopt_external_positions(
                    venue, mgr, session_date=SESSION,
                    decisions=Decisions([decision_row(stop_pct=bad)]),
                    max_trade_risk=2.0)
                self.assertEqual(out["adopted"], 1)
                position = next(p for p in mgr.open_positions())
                # Falls back to the limit-derived stop, not a zero stop.
                self.assertLess(position.plan.stop_price, DRAM_ENTRY)
                self.assertGreater(position.plan.stop_price, 0.0)

    def test_an_unreadable_ledger_does_not_stop_a_decision_proof(self):
        class Broken(InMemoryOrderLedger):
            def for_session(self, session_date):
                raise OrderLedgerError("ledger unreadable")

        mgr = manager()
        venue = Venue(positions=[dram_position()],
                      orders=[{"symbol": "DRAM", "id": "d",
                               "client_order_id": DRAM_CLI,
                               "status": "filled"}])
        out = adopt_external_positions(
            venue, mgr, session_date=SESSION, ledger=Broken(),
            decisions=Decisions([decision_row()]))
        self.assertEqual(out["adopted"], 1)
        self.assertEqual(out["integrity"], INTEGRITY_PARTIAL,
                         "the proof held, but a source was unreadable and "
                         "that is reported")


class TestEachLayerOfTheProofGateIndependently(unittest.TestCase):
    """Two checks guard adoption, and each is pinned on its own.

    `adopt_external_positions` refuses on `not verdict["proven"]` and
    then again on the evidence not being one of the two proving kinds.
    That redundancy is deliberate - `proven` is defined in another
    module and could change meaning - but it also means a mutation to
    either check alone is masked by the other, so neither would be
    observable as load-bearing.

    These tests feed a crafted verdict in which the two disagree. Such a
    verdict cannot come out of the real classifier; it is precisely the
    inconsistency each layer exists to survive.
    """

    def _adopt_with_verdict(self, verdict):
        import agent.positions.adoption as adoption_module
        original = adoption_module.classify_position
        adoption_module.classify_position = lambda *a, **k: verdict
        try:
            mgr = manager()
            out = adopt_external_positions(
                Venue(positions=[dram_position()], orders=[]), mgr,
                session_date=SESSION)
            return mgr, out
        finally:
            adoption_module.classify_position = original

    def test_proven_false_refuses_even_with_proving_evidence(self):
        """Pins the `not proven` check alone."""
        mgr, out = self._adopt_with_verdict({
            "symbol": "DRAM", "origin": "AGENT_CREATED",
            "evidence": "RECONSTRUCTED_CLIENT_ID", "detail": "crafted",
            "adoptable": True, "proven": False})
        self.assertEqual(out["adopted"], 0)
        self.assertEqual(out["unknown_origin"], ["DRAM"])
        self.assertTrue(out["blocks_new_exposure"])
        self.assertEqual(list(mgr.open_positions()), [])

    def test_weak_evidence_refuses_even_when_proven_says_true(self):
        """Pins the evidence check alone."""
        mgr, out = self._adopt_with_verdict({
            "symbol": "DRAM", "origin": "AGENT_CREATED",
            "evidence": "CLIENT_ID_PREFIX_ONLY", "detail": "crafted",
            "adoptable": True, "proven": True})
        self.assertEqual(out["adopted"], 0)
        self.assertEqual(out["unknown_origin"], ["DRAM"])
        self.assertTrue(out["blocks_new_exposure"])
        self.assertEqual(list(mgr.open_positions()), [])

    def test_both_agreeing_does_adopt(self):
        """The control: with the checks satisfied the position IS
        adopted, so the two tests above are not passing merely because
        nothing is ever adopted on this path."""
        mgr, out = self._adopt_with_verdict({
            "symbol": "DRAM", "origin": "AGENT_CREATED",
            "evidence": "LEDGER_RECORD", "detail": "crafted",
            "adoptable": True, "proven": True,
            "client_order_id": DRAM_CLI})
        self.assertEqual(out["adopted"], 1)
        self.assertIn("DRAM", [p.symbol for p in mgr.open_positions()])

    def test_an_unknown_origin_with_proven_true_still_refuses(self):
        """Origin is checked too, not only the flags."""
        mgr, out = self._adopt_with_verdict({
            "symbol": "DRAM", "origin": "UNKNOWN_ORIGIN",
            "evidence": "LEDGER_RECORD", "detail": "crafted",
            "adoptable": True, "proven": True})
        self.assertEqual(out["adopted"], 0)
        self.assertTrue(out["blocks_new_exposure"])
        self.assertEqual(list(mgr.open_positions()), [])
