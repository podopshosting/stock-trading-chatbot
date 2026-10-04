"""Adoption must be CALLED, and calling it twice must be a no-op.

agent/positions/adoption.py existed for some time with ZERO call sites.
The code was correct and never ran, so a position the agent itself
opened could sit at the venue unmanaged indefinitely - which is the
DRAM situation: reconciliation saw a broker position the agent did not
hold, raised RECONCILIATION_MISMATCH and EMERGENCY_STOP, and halted
permanently because nothing ever closed the gap.

A module that is never invoked passes all of its own unit tests.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CYCLE = os.path.join(REPO, "lambda-micro", "agent-cycle", "handler.py")


def cycle_source() -> str:
    with open(CYCLE) as fh:
        return fh.read()


class TestAdoptionIsWiredIntoTheCycle(unittest.TestCase):

    def test_the_cycle_imports_adoption(self):
        self.assertIn("from agent.positions.adoption import "
                      "adopt_external_positions", cycle_source())

    def test_the_cycle_CALLS_adoption(self):
        """The property that was missing.

        Not "the import exists" - an unused import passes that. The
        call site is what makes the module run.
        """
        src = cycle_source()
        self.assertIn("adopt_external_positions(", src)
        # Once for the import, once for the call.
        self.assertGreaterEqual(src.count("adopt_external_positions"), 2)

    def test_adoption_happens_AFTER_the_store_is_loaded(self):
        """Order is what makes it idempotent.

        Adoption recognises an already-managed symbol only if the
        manager was populated from the store first. Verified against
        the real paper account: with the load, a second pass reports
        already_managed and the position id is unchanged; without it,
        every pass adopts again with a new id.
        """
        src = cycle_source()
        load_at = src.index("manager._positions[position.symbol] = position")
        adopt_at = src.index("adopt_external_positions(\n")
        self.assertLess(load_at, adopt_at,
                        "adoption runs before the store is loaded, so it "
                        "cannot recognise an already-managed position")

    def test_adoption_failure_is_not_fatal(self):
        """A failed adoption must not stop the exits.

        Exits are the one thing that must keep working while halted, and
        an exception here would take them down with it.
        """
        src = cycle_source()
        start = src.index("adoption_result = None")
        end = src.index("orchestrator = MarketDayOrchestrator", start)
        block = src[start:end]
        self.assertIn("except Exception", block)
        self.assertIn("adoption_failed", block)
        # And it must not re-raise.
        self.assertNotIn("raise", block)

    def test_the_ledger_is_shared_with_the_orchestrator(self):
        """Adoption must read the evidence the exit is written into.

        Two separate ledgers could disagree about what the agent has
        already sent, which is how a duplicate order gets created.
        """
        src = cycle_source()
        self.assertIn("external_order_ledger = (DynamoDBOrderLedger", src)
        self.assertIn("order_ledger=external_order_ledger", src)
        self.assertIn("ledger=external_order_ledger", src)

    def test_both_new_events_are_registered(self):
        # An unregistered event name fails a separate guard; named here
        # so a reader of THIS file knows the events exist.
        from agent.observability import log_event              # noqa: F401
        with open(os.path.join(REPO, "agent",
                               "observability.py")) as fh:
            src = fh.read()
        self.assertIn('"positions_adopted"', src)
        self.assertIn('"adoption_failed"', src)


class TestOnlyProvenPositionsAreAdopted(unittest.TestCase):
    """The gate on adoption is PROVEN provenance, not adoptability."""

    def test_adoption_gates_on_proven_not_adoptable(self):
        with open(os.path.join(REPO, "agent", "positions",
                               "adoption.py")) as fh:
            src = fh.read()
        # `adoptable` is true for prefix-only evidence, which is NOT
        # enough: a client id carrying the agent's prefix that no stored
        # decision reproduces is weaker than a reconstruction.
        self.assertIn('"proven"', src)

    def test_prefix_only_evidence_does_not_adopt(self):
        from agent.broker import provenance as P
        # A client id with the right prefix that no decision reproduces.
        orders = [{"client_order_id": "cli_1d1d1063b70a461b9e9e",
                   "symbol": "DRAM", "side": "buy", "status": "filled"}]
        result = P.classify_position("DRAM", orders, [], [])
        self.assertEqual(result["origin"], P.ORIGIN_AGENT_CREATED)
        self.assertEqual(result["evidence"], P.EVIDENCE_PREFIX_ONLY)
        self.assertTrue(result["adoptable"])
        # Adoptable but NOT proven - and adoption requires proven.
        self.assertFalse(result["proven"])

    def test_a_reconstructed_id_IS_proven(self):
        """The falsifying control for the test above.

        If `proven` were always False, the test above would pass while
        nothing could ever be adopted.
        """
        from agent.broker import provenance as P
        # The field names classify_position actually reads: either
        # row["risk"]["decision_id"] or row["risk_decision_id"]. My
        # first attempt used "decision_id" at the top level, which it
        # does not read - so the control failed and said so, which is
        # what a control is for.
        decisions = [{"risk_decision_id": "risk_adef08c5015b"}]
        orders = [{"client_order_id": "cli_1d1d1063b70a461b9e9e",
                   "symbol": "DRAM", "side": "buy", "status": "filled"}]
        result = P.classify_position("DRAM", orders, [], decisions)
        self.assertEqual(result["evidence"], P.EVIDENCE_RECONSTRUCTED_ID)
        self.assertTrue(result["proven"])

    def test_unreadable_orders_are_unknown_not_preexisting(self):
        # Not knowing who opened a position is not evidence that
        # somebody else did.
        from agent.broker import provenance as P
        result = P.classify_position("DRAM", None, [], [])
        self.assertEqual(result["origin"], P.ORIGIN_UNKNOWN)
        self.assertFalse(result["proven"])


if __name__ == "__main__":
    unittest.main()
