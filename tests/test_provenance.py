"""
Whether an externally discovered position is the agent's own.

The DRAM case on 2026-10-02 is the regression fixture: a position the
agent had provably created, classified as unexplained, and therefore
left unmanaged while the agent halted correctly and could not close what
it had bought.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.broker.provenance import (                               # noqa: E402
    EVIDENCE_LEDGER, EVIDENCE_NO_AGENT_ORDER, EVIDENCE_PREFIX_ONLY,
    EVIDENCE_RECONSTRUCTED_ID, EVIDENCE_UNREADABLE, ORIGIN_AGENT_CREATED,
    ORIGIN_PREEXISTING_EXTERNAL, ORIGIN_UNKNOWN, blocks_new_exposure,
    classify_position, client_order_id_for,
)

# Taken verbatim from the venue and the decision log on 2026-10-02.
DRAM_CID = "cli_1d1d1063b70a461b9e9e"
DRAM_DECISION = "risk_adef08c5015b"
DRAM_ORDER = {"id": "d7eaecdd-4c25-4273-8685-8085e60d48b4",
              "client_order_id": DRAM_CID, "symbol": "DRAM",
              "status": "filled", "filled_qty": "0.48247"}
DRAM_DECISION_ROW = {"symbol": "DRAM", "outcome": "ORDER_NOT_FILLED",
                     "risk": {"decision_id": DRAM_DECISION}}


class TestTheRealDramCase(unittest.TestCase):

    def test_the_client_id_is_reproduced_exactly(self):
        """Only something holding the agent's own risk decision id could
        produce this string."""
        self.assertEqual(client_order_id_for(DRAM_DECISION, "ENTRY"),
                         DRAM_CID)

    def test_dram_is_classified_as_the_agents_own(self):
        c = classify_position("DRAM", [DRAM_ORDER],
                              decisions=[DRAM_DECISION_ROW])
        self.assertEqual(c["origin"], ORIGIN_AGENT_CREATED)
        self.assertEqual(c["evidence"], EVIDENCE_RECONSTRUCTED_ID)

    def test_the_conclusion_is_proven_not_inferred(self):
        c = classify_position("DRAM", [DRAM_ORDER],
                              decisions=[DRAM_DECISION_ROW])
        self.assertTrue(c["proven"])
        self.assertTrue(c["adoptable"])

    def test_it_carries_the_evidence_forward(self):
        c = classify_position("DRAM", [DRAM_ORDER],
                              decisions=[DRAM_DECISION_ROW])
        self.assertEqual(c["client_order_id"], DRAM_CID)
        self.assertEqual(c["risk_decision_id"], DRAM_DECISION)
        self.assertIn("sha256", c["detail"])


class TestForeignAndUnknown(unittest.TestCase):

    def test_a_symbol_with_no_agent_order_is_preexisting(self):
        c = classify_position("MSFT", [DRAM_ORDER],
                              decisions=[DRAM_DECISION_ROW])
        self.assertEqual(c["origin"], ORIGIN_PREEXISTING_EXTERNAL)
        self.assertEqual(c["evidence"], EVIDENCE_NO_AGENT_ORDER)
        self.assertFalse(c["adoptable"])

    def test_a_preexisting_position_is_never_adoptable(self):
        c = classify_position("MSFT", [], decisions=[])
        self.assertFalse(c["adoptable"])

    def test_unreadable_orders_give_unknown_not_preexisting(self):
        """Not knowing who opened a position is not evidence that
        somebody else did."""
        c = classify_position("DRAM", None)
        self.assertEqual(c["origin"], ORIGIN_UNKNOWN)
        self.assertEqual(c["evidence"], EVIDENCE_UNREADABLE)
        self.assertFalse(c["adoptable"])

    def test_an_unattributable_order_is_unknown(self):
        foreign = {"id": "x", "client_order_id": "someone_else_42",
                   "symbol": "DRAM", "status": "filled"}
        c = classify_position("DRAM", [foreign], decisions=[])
        self.assertEqual(c["origin"], ORIGIN_UNKNOWN)

    def test_unknown_origin_blocks_new_exposure(self):
        c = classify_position("DRAM", None)
        self.assertTrue(blocks_new_exposure([c]))

    def test_a_preexisting_position_alone_does_not_block(self):
        """Somebody else's holding is left alone, not treated as a
        reason to stop - otherwise a funded account could never be used
        at all."""
        c = classify_position("MSFT", [], decisions=[])
        self.assertFalse(blocks_new_exposure([c]))

    def test_an_agent_position_does_not_block(self):
        c = classify_position("DRAM", [DRAM_ORDER],
                              decisions=[DRAM_DECISION_ROW])
        self.assertFalse(blocks_new_exposure([c]))


class TestEvidenceStrength(unittest.TestCase):

    def test_a_ledger_record_is_the_strongest_evidence(self):
        class Row:
            symbol = "DRAM"
            filled_quantity = 0.48247
            client_order_id = DRAM_CID
        c = classify_position("DRAM", [DRAM_ORDER], ledger_rows=[Row()])
        self.assertEqual(c["evidence"], EVIDENCE_LEDGER)
        self.assertTrue(c["proven"])

    def test_an_unfilled_ledger_row_is_not_evidence_of_a_position(self):
        """A recorded order that never filled cannot explain a holding."""
        class Row:
            symbol = "DRAM"
            filled_quantity = 0.0
            client_order_id = DRAM_CID
        c = classify_position("DRAM", [], ledger_rows=[Row()], decisions=[])
        self.assertNotEqual(c["evidence"], EVIDENCE_LEDGER)

    def test_a_prefix_match_is_reported_as_weaker(self):
        """Any client could choose the same prefix, so this is
        suggestive and labelled as such rather than counted as proof."""
        orphan = {"id": "y", "client_order_id": "cli_deadbeefdeadbeefdead",
                  "symbol": "DRAM", "status": "filled"}
        c = classify_position("DRAM", [orphan], decisions=[])
        self.assertEqual(c["origin"], ORIGIN_AGENT_CREATED)
        self.assertEqual(c["evidence"], EVIDENCE_PREFIX_ONLY)
        self.assertFalse(c["proven"])

    def test_reconstruction_beats_a_prefix_when_both_apply(self):
        """The control on ordering: a reconstructable id must not be
        downgraded to a prefix match."""
        c = classify_position("DRAM", [DRAM_ORDER],
                              decisions=[DRAM_DECISION_ROW])
        self.assertEqual(c["evidence"], EVIDENCE_RECONSTRUCTED_ID)

    def test_a_different_intent_does_not_collide(self):
        self.assertNotEqual(client_order_id_for(DRAM_DECISION, "ENTRY"),
                            client_order_id_for(DRAM_DECISION, "EXIT"))

    def test_a_different_decision_does_not_collide(self):
        self.assertNotEqual(client_order_id_for("risk_aaa", "ENTRY"),
                            client_order_id_for("risk_bbb", "ENTRY"))


class TestTheTwoDerivationsCannotDrift(unittest.TestCase):
    """The client order id is derived in TWO places, by design.

    `agent/broker/execution.py` builds it when proposing an order;
    `agent/broker/provenance.py` reconstructs it when deciding whose a
    discovered position is. Provenance deliberately cannot import the
    execution path, because the read API must not be able to reach it,
    so the same rule is written twice.

    Two implementations of one rule drift. If they ever disagreed the
    ledger would key orders under one id while provenance reconstructed
    another; adoption would quietly stop recognising the agent's own
    positions, and every other test would still pass, because each side
    is internally self-consistent. This is the only test that fails.

    Canonical fixed decision ids, not generated ones: a generated
    decision makes the comparison depend on hypothesis generation, and
    a fixture that drifts cannot pin anything.
    """

    # Includes the real DRAM decision, whose id was proven byte-for-byte
    # against the venue on 2026-10-02.
    CANONICAL = (
        "risk_adef08c5015b",
        "risk_0000000000",
        "risk_ffffffffffffffffffff",
        "d1",
        "risk_with:colon",
        "risk_with spaces",
        "risk_unicode_éè",
    )
    INTENTS = ("ENTRY", "EXIT", "REPLACE", "")

    class _Decision:
        """The minimum build_proposal needs, with a fixed decision id."""
        approved = True
        capital_required = 100.0
        symbol = "XYZ"
        hypothesis_id = "h1"

        def __init__(self, decision_id):
            self.decision_id = decision_id

    def test_both_implementations_agree_byte_for_byte(self):
        from agent.broker.execution import build_proposal
        for decision_id in self.CANONICAL:
            for intent in self.INTENTS:
                with self.subTest(decision=decision_id, intent=intent):
                    proposed = build_proposal(
                        self._Decision(decision_id), 100.0,
                        intent=intent).client_order_id
                    reconstructed = client_order_id_for(decision_id, intent)
                    self.assertEqual(
                        proposed, reconstructed,
                        "execution.py and provenance.py derive different "
                        "client order ids; provenance can no longer prove "
                        "a position is the agent's own")

    def test_repeated_calls_are_stable_on_both_sides(self):
        """Determinism is the property the whole scheme rests on: a
        retry must produce the same id or the venue cannot dedupe it."""
        from agent.broker.execution import build_proposal
        for decision_id in self.CANONICAL[:3]:
            first = build_proposal(self._Decision(decision_id), 100.0,
                                   intent="ENTRY").client_order_id
            for _ in range(5):
                self.assertEqual(
                    build_proposal(self._Decision(decision_id), 100.0,
                                   intent="ENTRY").client_order_id, first)
                self.assertEqual(
                    client_order_id_for(decision_id, "ENTRY"), first)

    def test_the_reference_price_does_not_enter_the_id(self):
        """A retry at a different price is the SAME logical order. If
        price entered the id, a retry after a tick would place a second
        order and the venue would have no way to tell."""
        from agent.broker.execution import build_proposal
        a = build_proposal(self._Decision("risk_px"), 100.0).client_order_id
        b = build_proposal(self._Decision("risk_px"), 987.65).client_order_id
        self.assertEqual(a, b)
        self.assertEqual(a, client_order_id_for("risk_px", "ENTRY"))

    def test_the_shared_shape_holds_on_both_sides(self):
        from agent.broker.execution import build_proposal
        from agent.broker.provenance import CLIENT_ID_PREFIX, DIGEST_LENGTH
        proposed = build_proposal(self._Decision("risk_shape"),
                                  100.0).client_order_id
        for cli in (proposed, client_order_id_for("risk_shape", "ENTRY")):
            self.assertTrue(cli.startswith(CLIENT_ID_PREFIX))
            self.assertEqual(len(cli), len(CLIENT_ID_PREFIX) + DIGEST_LENGTH)
            self.assertLessEqual(len(cli), 128)   # Alpaca's limit

    def test_different_intents_never_collide(self):
        ids = {client_order_id_for("risk_same", i) for i in self.INTENTS}
        self.assertEqual(len(ids), len(self.INTENTS))
