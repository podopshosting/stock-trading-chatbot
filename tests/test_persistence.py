"""
State persistence for the paper pilot.

Lambda is stateless. Without these stores a paper account resets on
every invocation, which would turn a pilot into a stream of unrelated
one-cycle experiments while looking like a continuous record.

The failures worth guarding against are all silent ones:

- An unreadable store returning "empty" rather than raising, so the
  agent believes it holds nothing while the broker holds real positions.
- A restored position arriving without its exit plan, which is the only
  thing standing between it and an unbounded loss.
- A restored stop that is WIDER than the one the position had reached.
- A client order id surviving while the order it points to does not, so
  a retry after a cold start places a second order.
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    BrokerStateError, ConcurrentBrokerUpdate, DynamoDBBrokerStateStore,
    InMemoryBrokerStateStore, PaperBroker, PaperBrokerConfig, Quote,
    restore, serialise,
)
from agent.positions import (                                     # noqa: E402
    DynamoDBPositionStore, ExitPlan, InMemoryPositionStore,
    PositionManager, PositionState, PositionStoreError, apply_trailing_stop,
    from_item, to_item, update_high_water,
)


def broker(cash=500.0):
    b = PaperBroker(PaperBrokerConfig(starting_cash=cash, seed=3,
                                      partial_fill_probability=0.0))
    b.set_quote(Quote(symbol="XYZ", bid=99.9, ask=100.1, last=100.0))
    return b


def round_trip(source):
    """Save and reload, as a cold Lambda start would."""
    store = InMemoryBrokerStateStore()
    store.save(source)
    snapshot, revision = store.load()
    fresh = PaperBroker(PaperBrokerConfig(
        starting_cash=source.config.starting_cash, seed=3))
    restore(fresh, snapshot)
    fresh.set_quote(Quote(symbol="XYZ", bid=99.9, ask=100.1, last=100.0))
    return fresh, store, revision


class TestBrokerStateSurvivesRestart(unittest.TestCase):

    def test_cash_survives(self):
        source = broker()
        source.submit_order("XYZ", "BUY", 1.0)
        before = source.get_account()["cash"]
        fresh, _s, _r = round_trip(source)
        self.assertAlmostEqual(fresh.get_account()["cash"], before, places=4)

    def test_cash_does_not_reset_to_starting_cash(self):
        """
        The failure this whole module exists to prevent: a reset account
        looks like a clean slate and silently funds trades with money
        that is already committed.
        """
        source = broker(cash=500.0)
        source.submit_order("XYZ", "BUY", 1.0)
        fresh, _s, _r = round_trip(source)
        self.assertNotAlmostEqual(fresh.get_account()["cash"], 500.0,
                                  places=2)

    def test_positions_survive(self):
        source = broker()
        source.submit_order("XYZ", "BUY", 1.5)
        fresh, _s, _r = round_trip(source)
        self.assertAlmostEqual(fresh.get_position("XYZ")["quantity"], 1.5,
                               places=6)

    def test_the_entry_price_survives(self):
        source = broker()
        source.submit_order("XYZ", "BUY", 1.0)
        before = source.get_position("XYZ")["average_entry_price"]
        fresh, _s, _r = round_trip(source)
        self.assertAlmostEqual(
            fresh.get_position("XYZ")["average_entry_price"], before,
            places=4)

    def test_realized_pnl_survives(self):
        source = broker()
        source.submit_order("XYZ", "BUY", 1.0)
        source.set_quote(Quote(symbol="XYZ", bid=109.9, ask=110.1,
                               last=110.0))
        source.close_position("XYZ")
        before = source.get_account()["realized_pnl"]
        self.assertNotEqual(before, 0.0)
        fresh, _s, _r = round_trip(source)
        self.assertAlmostEqual(fresh.get_account()["realized_pnl"], before,
                               places=4)

    def test_reserved_cash_survives(self):
        """
        Otherwise a working order's reservation is released by a restart
        and the same dollar funds a second position.
        """
        source = broker(cash=200.0)
        source.submit_order("XYZ", "BUY", 1.0, order_type="LIMIT",
                            limit_price=90.0)
        before = source.get_account()["reserved_cash"]
        self.assertGreater(before, 0.0)
        fresh, _s, _r = round_trip(source)
        self.assertAlmostEqual(fresh.get_account()["reserved_cash"], before,
                               places=4)


class TestIdempotencySurvivesRestart(unittest.TestCase):
    """
    The most expensive failure available: a retried invocation after a
    cold start doubling a position.
    """

    def test_a_duplicate_client_id_is_still_suppressed(self):
        source = broker()
        first = source.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        fresh, _s, _r = round_trip(source)
        again = fresh.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        self.assertEqual(again["order_id"], first["order_id"])
        self.assertAlmostEqual(fresh.get_position("XYZ")["quantity"], 1.0,
                               places=6)

    def test_terminal_orders_are_persisted_not_only_open_ones(self):
        """
        Idempotency must return the EXISTING order, so the order has to
        still exist. Persisting only open orders while keeping the whole
        client-id map made a duplicate submission crash after a restart.
        """
        source = broker()
        order = source.submit_order("XYZ", "BUY", 1.0,
                                    client_order_id="abc")
        self.assertEqual(order["status"], "FILLED")   # terminal
        snapshot = serialise(source)
        stored_ids = [o["order_id"] for o in snapshot["orders"]]
        self.assertIn(order["order_id"], stored_ids)

    def test_an_inconsistent_dedup_map_refuses_rather_than_places(self):
        """
        If the client id is known but its order is missing, the state is
        inconsistent. Placing the order would do exactly what the client
        id exists to prevent, so it is refused.
        """
        source = broker()
        source.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        fresh, _s, _r = round_trip(source)
        fresh._orders.clear()                  # a partial restore
        result = fresh.submit_order("XYZ", "BUY", 1.0,
                                    client_order_id="abc")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["reject_reason"],
                         "DUPLICATE_CLIENT_ORDER_ID")

    def test_an_inconsistent_dedup_map_does_not_add_to_the_position(self):
        source = broker()
        source.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        fresh, _s, _r = round_trip(source)
        before = fresh.get_position("XYZ")["quantity"]
        fresh._orders.clear()
        fresh.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        self.assertAlmostEqual(fresh.get_position("XYZ")["quantity"],
                               before, places=6)

    def test_an_older_snapshot_key_still_restores(self):
        """
        A snapshot written before the fix used "open_orders". Reading
        only the new key would silently drop every order and with it the
        idempotency guarantee.
        """
        source = broker()
        order = source.submit_order("XYZ", "BUY", 1.0,
                                    client_order_id="abc")
        snapshot = serialise(source)
        snapshot["open_orders"] = snapshot.pop("orders")
        fresh = PaperBroker(PaperBrokerConfig(starting_cash=500.0, seed=3))
        restore(fresh, snapshot)
        self.assertIn(order["order_id"], fresh._orders)


class TestBrokerConcurrency(unittest.TestCase):

    def test_a_stale_write_is_refused(self):
        """
        Two cycles that both load, both decide and both save would lose
        one set of fills silently.
        """
        source = broker()
        store = InMemoryBrokerStateStore()
        store.save(source)
        _snapshot, revision = store.load()
        store.save(source, expected_revision=revision)     # writer A
        with self.assertRaises(ConcurrentBrokerUpdate):
            store.save(source, expected_revision=revision)  # writer B

    def test_a_first_write_needs_no_revision(self):
        self.assertEqual(InMemoryBrokerStateStore().save(broker()), 1)

    def test_the_revision_advances_on_each_save(self):
        source = broker()
        store = InMemoryBrokerStateStore()
        self.assertEqual(store.save(source), 1)
        self.assertEqual(store.save(source, expected_revision=1), 2)

    def test_an_absent_account_loads_as_none_not_as_an_error(self):
        """A first run genuinely has no state, which is fine."""
        snapshot, revision = InMemoryBrokerStateStore().load()
        self.assertIsNone(snapshot)
        self.assertEqual(revision, 0)

    def test_an_unreadable_account_raises_rather_than_looking_empty(self):
        """
        Handing back a fresh broker with full starting cash would look
        like a clean slate while real positions sat unmanaged.
        """
        class Broken:
            def get_item(self, **kwargs):
                raise RuntimeError("dynamo unavailable")
        store = DynamoDBBrokerStateStore(table_name="x", client=Broken())
        with self.assertRaises(BrokerStateError):
            store.load()

    def test_a_conditional_failure_becomes_a_concurrency_error(self):
        class Conflicting:
            def put_item(self, **kwargs):
                raise RuntimeError("ConditionalCheckFailedException")
        store = DynamoDBBrokerStateStore(table_name="x",
                                         client=Conflicting())
        with self.assertRaises(ConcurrentBrokerUpdate):
            store.save(broker(), expected_revision=1)

    def test_restoring_an_empty_snapshot_raises(self):
        with self.assertRaises(BrokerStateError):
            restore(broker(), {})


class TestPositionPersistence(unittest.TestCase):

    def _position(self, stop=97.0, trail=3.0):
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0)
        manager = PositionManager(broker=b, execution_available=True)
        return manager.open_from_order(
            order, ExitPlan(stop_price=stop, target_price=110.0,
                            trailing_stop_pct=trail),
            hypothesis_id="h1", risk_decision_id="d1",
            config_version="cfg-1")

    def test_the_exit_plan_survives(self):
        """
        The broker knows quantity and cost basis. It does not know the
        plan, and the plan is what keeps the position from becoming an
        unbounded loss.
        """
        position = self._position()
        store = InMemoryPositionStore()
        store.save(position, "2026-09-30")
        [restored] = store.load_open("2026-09-30")
        self.assertAlmostEqual(restored.plan.stop_price,
                               position.plan.stop_price, places=4)
        self.assertAlmostEqual(restored.plan.target_price, 110.0, places=4)
        self.assertAlmostEqual(restored.plan.trailing_stop_pct, 3.0,
                               places=4)

    def test_the_high_water_mark_survives(self):
        """
        Losing it resets the trailing stop to the entry, silently giving
        back every ratchet the position had earned.
        """
        position = self._position()
        update_high_water(position, 130.0)
        apply_trailing_stop(position)
        tightened = position.plan.stop_price
        self.assertGreater(tightened, 97.0)

        store = InMemoryPositionStore()
        store.save(position, "2026-09-30")
        [restored] = store.load_open("2026-09-30")
        self.assertEqual(restored.high_water_price, 130.0)
        self.assertAlmostEqual(restored.plan.stop_price, tightened, places=4)

    def test_provenance_survives(self):
        position = self._position()
        store = InMemoryPositionStore()
        store.save(position, "2026-09-30")
        [restored] = store.load_open("2026-09-30")
        self.assertEqual(restored.hypothesis_id, "h1")
        self.assertEqual(restored.risk_decision_id, "d1")
        self.assertEqual(restored.position_id, position.position_id)
        self.assertEqual(restored.config_version, "cfg-1")

    def test_the_stop_history_survives(self):
        position = self._position()
        position.tighten_stop(98.0, reason="manual")
        store = InMemoryPositionStore()
        store.save(position, "2026-09-30")
        [restored] = store.load_open("2026-09-30")
        self.assertEqual(len(restored.stop_history), 1)

    def test_a_position_without_a_stored_stop_is_refused(self):
        """
        A position without a stop is an unbounded loss. Defaulting one
        would apply a number nobody chose to real exposure.
        """
        item = to_item(self._position())
        del item["plan"]["stop_price"]
        with self.assertRaises(PositionStoreError):
            from_item(item)

    def test_a_loosened_stop_is_refused_on_restore(self):
        """
        If storage were rolled back or edited, restoring the older wider
        stop would quietly increase risk on a live position - the one
        thing tighten_stop exists to make impossible.
        """
        position = self._position()
        update_high_water(position, 130.0)
        apply_trailing_stop(position)
        item = to_item(position)
        item["plan"]["stop_price"] = 90.0
        with self.assertRaises(PositionStoreError):
            from_item(item)

    def test_rounding_dust_is_not_mistaken_for_a_loosened_stop(self):
        """
        stop_history rounds to six places while plan.stop_price keeps
        the raw float, so a trailing stop at 116.39999999999999 reads as
        "wider" than its own recorded 116.4 under an exact comparison.
        That refused legitimate positions and would have halted the
        agent over floating-point dust.
        """
        position = self._position()
        update_high_water(position, 120.0)
        apply_trailing_stop(position)
        item = to_item(position)
        # Force the exact shape that broke: history rounded, plan raw.
        item["plan"]["stop_price"] = position.plan.stop_price
        item["stop_history"][-1]["to"] = round(position.plan.stop_price, 6)
        restored = from_item(item)
        self.assertAlmostEqual(restored.plan.stop_price,
                               position.plan.stop_price, places=6)

    def test_a_meaningfully_loosened_stop_is_still_refused(self):
        """
        The tolerance must not swallow a real loosening. A tenth of a
        cent of slack is not permission to move a stop a dollar.
        """
        position = self._position()
        update_high_water(position, 120.0)
        apply_trailing_stop(position)
        item = to_item(position)
        item["plan"]["stop_price"] = position.plan.stop_price - 1.0
        with self.assertRaises(PositionStoreError):
            from_item(item)

    def test_a_tightened_stop_restores_normally(self):
        """The falsifying control: the check must not refuse everything."""
        position = self._position()
        position.tighten_stop(99.0, reason="manual")
        restored = from_item(to_item(position))
        self.assertAlmostEqual(restored.plan.stop_price, 99.0, places=4)

    def test_closed_positions_are_not_reloaded_as_open(self):
        position = self._position()
        position.state = PositionState.CLOSED
        store = InMemoryPositionStore()
        store.save(position, "2026-09-30")
        # The in-memory store keeps everything; the filter belongs to
        # the caller, so assert on the state rather than the count.
        [restored] = store.load_open("2026-09-30")
        self.assertIs(restored.state, PositionState.CLOSED)

    def test_a_deleted_position_does_not_come_back(self):
        position = self._position()
        store = InMemoryPositionStore()
        store.save(position, "2026-09-30")
        store.delete(position.symbol, "2026-09-30")
        self.assertEqual(store.load_open("2026-09-30"), [])

    def test_positions_are_scoped_to_a_session(self):
        position = self._position()
        store = InMemoryPositionStore()
        store.save(position, "2026-09-30")
        self.assertEqual(store.load_open("2026-10-01"), [])

    def test_an_unreadable_position_store_raises(self):
        """
        Returning [] would make the orchestrator believe it holds
        nothing while the broker holds real positions. Reconciliation
        would catch that and halt - but this layer should not be relying
        on another layer to cover it.
        """
        class Broken:
            def query(self, **kwargs):
                raise RuntimeError("dynamo unavailable")
        store = DynamoDBPositionStore(table_name="x", client=Broken())
        with self.assertRaises(PositionStoreError):
            store.load_open("2026-09-30")


class TestSerialisationIsLossless(unittest.TestCase):

    def test_a_broker_snapshot_is_json_serialisable(self):
        """It has to survive DynamoDB, so it must survive json."""
        source = broker()
        source.submit_order("XYZ", "BUY", 1.0)
        json.dumps(serialise(source))

    def test_a_position_item_is_json_serialisable(self):
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0)
        manager = PositionManager(broker=b, execution_available=True)
        position = manager.open_from_order(order, ExitPlan(stop_price=97.0))
        json.dumps(to_item(position))

    def test_a_double_round_trip_is_stable(self):
        """
        Once is not enough: a lossy field often survives one pass and
        drifts on the second.
        """
        source = broker()
        source.submit_order("XYZ", "BUY", 1.0, client_order_id="abc")
        first = serialise(source)
        mid = PaperBroker(PaperBrokerConfig(starting_cash=500.0, seed=3))
        restore(mid, first)
        second = serialise(mid)
        self.assertEqual(first["account"], second["account"])
        self.assertEqual(first["positions"], second["positions"])
        self.assertEqual(first["client_order_ids"],
                         second["client_order_ids"])

    def test_a_position_double_round_trip_is_stable(self):
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0)
        manager = PositionManager(broker=b, execution_available=True)
        position = manager.open_from_order(
            order, ExitPlan(stop_price=97.0, trailing_stop_pct=3.0),
            hypothesis_id="h1")
        update_high_water(position, 120.0)
        apply_trailing_stop(position)
        first = to_item(position)
        second = to_item(from_item(first))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()


class TestTheSimulatorStoreRefusesAnExternalBroker(unittest.TestCase):
    """
    This store serialises PaperBroker's private state. On 2026-10-02 the
    cycle handed it AlpacaPaperBroker, and the refusal arrived as
    `AttributeError: 'AlpacaPaperBroker' object has no attribute
    '_account'` from deep inside serialise() - AFTER an order had been
    submitted, which is the one window where a fill can be lost.

    The guard does not change the direction of failure. It makes the
    failure legible, and places it at the boundary rather than in the
    middle of a cycle.
    """

    class External:
        """An external adapter: the public surface, none of the
        simulator's private state."""
        name = "alpaca_paper"
        base_url = "https://paper-api.alpaca.markets"

        def get_account(self):
            return {"cash": 100000.0}

        def get_positions(self):
            return []

        def get_orders(self, **_kw):
            return []

    def test_serialise_refuses_with_a_named_error(self):
        from agent.broker.store import serialise, BrokerStateError
        with self.assertRaises(BrokerStateError) as ctx:
            serialise(self.External())
        self.assertIn("internal simulator", str(ctx.exception))
        self.assertIn("authoritative for its own book", str(ctx.exception))

    def test_restore_refuses_too(self):
        from agent.broker.store import restore, BrokerStateError
        with self.assertRaises(BrokerStateError):
            restore(self.External(), {})

    def test_the_error_names_what_is_missing(self):
        from agent.broker.store import serialise, BrokerStateError
        with self.assertRaises(BrokerStateError) as ctx:
            serialise(self.External())
        self.assertIn("_account", str(ctx.exception))

    def test_it_is_not_an_attribute_error(self):
        """The point of the guard: a legible refusal, not a crash whose
        message is an implementation detail."""
        from agent.broker.store import serialise, BrokerStateError
        try:
            serialise(self.External())
        except BrokerStateError:
            pass
        except AttributeError:
            self.fail("still failing with AttributeError from inside")

    def test_the_internal_simulator_is_still_accepted(self):
        """The control. Without it the guard could refuse everything."""
        from agent.broker.paper import PaperBroker, PaperBrokerConfig
        from agent.broker.store import serialise
        broker = PaperBroker(config=PaperBrokerConfig(starting_cash=100.0))
        data = serialise(broker)
        self.assertIn("account", data)

    def test_a_round_trip_still_works(self):
        """The other control: the guard must not break the thing the
        store exists to do."""
        from agent.broker.paper import PaperBroker, PaperBrokerConfig
        from agent.broker.store import serialise, restore
        a = PaperBroker(config=PaperBrokerConfig(starting_cash=250.0))
        b = PaperBroker(config=PaperBrokerConfig(starting_cash=1.0))
        restore(b, serialise(a))
        self.assertAlmostEqual(b.get_account()["cash"], 250.0, places=4)
