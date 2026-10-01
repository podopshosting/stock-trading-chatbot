"""
Shadow comparisons: what the simulator expected against what the venue did.

The failure this guards against is an empty report reading as agreement.
"No differences recorded" and "no comparisons exist" are different
claims, and only one of them is evidence that the simulator is
calibrated.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.broker.shadow import ShadowRecord                      # noqa: E402
from agent.autonomy.shadow_store import (                           # noqa: E402
    DynamoDBShadowStore, InMemoryShadowStore,
)


def record(client_order_id="ord-1", symbol="MSFT", **kw):
    base = dict(client_order_id=client_order_id, symbol=symbol,
                requested_quantity=0.05, requested_notional=25.0,
                intended_limit=500.0, reference_price=499.5)
    base.update(kw)
    return ShadowRecord(**base)


class TestInMemoryStore(unittest.TestCase):

    def test_a_record_round_trips(self):
        store = InMemoryShadowStore()
        store.put("2026-10-02", record())
        [row] = store.for_session("2026-10-02")
        self.assertEqual(row["symbol"], "MSFT")
        self.assertEqual(row["client_order_id"], "ord-1")

    def test_sessions_are_kept_apart(self):
        store = InMemoryShadowStore()
        store.put("2026-10-02", record())
        self.assertEqual(store.for_session("2026-10-03"), [])

    def test_an_empty_session_returns_nothing_rather_than_a_default(self):
        self.assertEqual(InMemoryShadowStore().for_session("2026-10-02"), [])

    def test_every_comparison_field_survives(self):
        store = InMemoryShadowStore()
        store.put("2026-10-02", record(
            internal_fill_price=500.1, internal_fill_quantity=0.05,
            broker_fill_price=500.4, broker_fill_quantity=0.05,
            broker_status="FILLED", broker_order_id="abc",
            submitted_to_broker_count=1))
        [row] = store.for_session("2026-10-02")
        for field in ("internal_fill_price", "broker_fill_price",
                      "broker_status", "submitted_to_broker_count"):
            with self.subTest(field=field):
                self.assertIn(field, row)
        self.assertEqual(row["submitted_to_broker_count"], 1)


class TestDynamoKeying(unittest.TestCase):
    """One order must not be able to produce two comparison rows."""

    class Fake:
        def __init__(self):
            self.items = {}

        def put_item(self, TableName, Item):
            self.items[(Item["PK"]["S"], Item["SK"]["S"])] = Item

        def query(self, **kw):
            prefix = kw["ExpressionAttributeValues"][":p"]["S"]
            return {"Items": [v for (pk, _sk), v in self.items.items()
                              if pk == prefix]}

    def test_the_client_order_id_is_the_key(self):
        fake = self.Fake()
        store = DynamoDBShadowStore(table_name="t", client=fake)
        store.put("2026-10-02", record())
        store.put("2026-10-02", record())          # a retry of one order
        self.assertEqual(len(store.for_session("2026-10-02")), 1)

    def test_two_different_orders_are_two_rows(self):
        fake = self.Fake()
        store = DynamoDBShadowStore(table_name="t", client=fake)
        store.put("2026-10-02", record("ord-1"))
        store.put("2026-10-02", record("ord-2"))
        self.assertEqual(len(store.for_session("2026-10-02")), 2)

    def test_the_session_is_part_of_the_partition_key(self):
        fake = self.Fake()
        store = DynamoDBShadowStore(table_name="t", client=fake)
        store.put("2026-10-02", record())
        store.put("2026-10-03", record())
        self.assertEqual(len(store.for_session("2026-10-02")), 1)
        self.assertEqual(len(store.for_session("2026-10-03")), 1)

    def test_it_defaults_to_the_journal_table_not_the_state_table(self):
        """The state table is keyed on session_date alone; a PK/SK write
        to it fails with a ValidationException."""
        store = DynamoDBShadowStore(client=self.Fake())
        self.assertNotIn("state", store.table_name)
        self.assertIn("journal", store.table_name)


if __name__ == "__main__":
    unittest.main()
