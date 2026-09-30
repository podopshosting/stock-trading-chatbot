"""
Position management and the exit engine.

The asymmetry these tests exist to protect: for a NEW entry the safe
direction is to do nothing; for an OPEN position the safe direction is
to get out. A system that treats "I have no data" as "hold" is holding
unbounded risk in exchange for nothing.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    PaperBroker, PaperBrokerConfig, Quote,
)
from agent.positions import (                                     # noqa: E402
    EXIT_PRIORITY, MAX_QUOTE_AGE_SECONDS, PROTECTIVE_REASONS,
    TRAILING_ACTIVATION_PCT, ExitContext, ExitPlan, ExitReason,
    ManagedPosition, PositionManager, PositionManagerError, PositionState,
    StopMechanism, StopWidened, apply_trailing_stop, evaluate,
    stop_gap_disclosure, trailing_stop_price, update_high_water,
)


def position(stop=97.0, entry=100.0, quantity=1.0, **plan_kwargs):
    return ManagedPosition(
        position_id="pos_test", symbol="XYZ", quantity=quantity,
        entry_price=entry, high_water_price=entry, current_price=entry,
        plan=ExitPlan(stop_price=stop, **plan_kwargs))


def broker(cash=5000.0, partial=0.0):
    # Partial fills are off by default so tests about position
    # management are not silently testing the fill model as well.
    b = PaperBroker(PaperBrokerConfig(starting_cash=cash, seed=3,
                                      partial_fill_probability=partial))
    b.set_quote(Quote(symbol="XYZ", bid=99.9, ask=100.1, last=100.0))
    return b


class TestStopsMayOnlyTighten(unittest.TestCase):
    """The invariant this module exists to enforce."""

    def test_a_stop_may_not_be_widened(self):
        """
        Moving a stop away from price to avoid taking a loss converts a
        bounded loss into an unbounded one. It is the single most
        destructive discretionary act in retail trading, so the code
        must make it impossible rather than merely discourage it.
        """
        p = position(stop=97.0)
        with self.assertRaises(StopWidened):
            p.tighten_stop(95.0)
        self.assertEqual(p.plan.stop_price, 97.0)

    def test_a_stop_may_not_be_widened_by_a_hair(self):
        p = position(stop=97.0)
        with self.assertRaises(StopWidened):
            p.tighten_stop(96.9999)

    def test_a_stop_may_not_be_set_to_none(self):
        """Removing a stop is the widest possible widening."""
        with self.assertRaises(StopWidened):
            position().tighten_stop(None)

    def test_a_stop_may_be_tightened(self):
        p = position(stop=97.0)
        self.assertTrue(p.tighten_stop(98.5, reason="trail"))
        self.assertEqual(p.plan.stop_price, 98.5)

    def test_an_unchanged_stop_reports_no_move(self):
        p = position(stop=97.0)
        self.assertFalse(p.tighten_stop(97.0))
        self.assertEqual(len(p.stop_history), 0)

    def test_every_stop_move_is_recorded(self):
        """Provenance: the record must show how the stop got there."""
        p = position(stop=97.0)
        p.tighten_stop(98.0, reason="TRAILING_STOP")
        p.tighten_stop(99.0, reason="manual")
        self.assertEqual(len(p.stop_history), 2)
        self.assertEqual(p.stop_history[0]["from"], 97.0)
        self.assertEqual(p.stop_history[0]["to"], 98.0)
        self.assertEqual(p.stop_history[1]["reason"], "manual")


class TestTrailingStop(unittest.TestCase):

    def test_the_high_water_mark_never_retreats(self):
        p = position()
        update_high_water(p, 110.0)
        update_high_water(p, 105.0)
        self.assertEqual(p.high_water_price, 110.0)

    def test_a_missing_price_does_not_disturb_the_high_water_mark(self):
        p = position()
        update_high_water(p, 110.0)
        update_high_water(p, None)
        self.assertEqual(p.high_water_price, 110.0)

    def test_the_trail_is_inactive_until_the_trade_has_earned_room(self):
        """
        Trailing from the first tick converts ordinary noise into an
        exit and guarantees the strategy never holds a winner.
        """
        p = position(entry=100.0, trailing_stop_pct=3.0)
        update_high_water(p, 100.2)          # +0.2%, under activation
        self.assertIsNone(trailing_stop_price(p))

    def test_the_trail_activates_once_the_trade_is_up(self):
        p = position(entry=100.0, trailing_stop_pct=3.0)
        update_high_water(p, 105.0)
        self.assertAlmostEqual(trailing_stop_price(p), 105.0 * 0.97, places=4)

    def test_activation_uses_the_documented_threshold(self):
        p = position(entry=100.0, trailing_stop_pct=3.0)
        update_high_water(p, 100.0 * (1 + TRAILING_ACTIVATION_PCT / 100.0))
        self.assertIsNotNone(trailing_stop_price(p))

    def test_no_trail_configured_means_no_trailing_stop(self):
        p = position(trailing_stop_pct=None)
        update_high_water(p, 150.0)
        self.assertIsNone(trailing_stop_price(p))

    def test_applying_the_trail_ratchets_the_hard_stop_up(self):
        p = position(entry=100.0, stop=97.0, trailing_stop_pct=3.0)
        update_high_water(p, 110.0)
        self.assertTrue(apply_trailing_stop(p))
        self.assertAlmostEqual(p.plan.stop_price, 106.7, places=4)

    def test_the_trail_never_loosens_an_already_tighter_stop(self):
        """The tighter of the two always wins."""
        p = position(entry=100.0, stop=108.0, trailing_stop_pct=3.0)
        update_high_water(p, 110.0)          # trail would be 106.7
        self.assertFalse(apply_trailing_stop(p))
        self.assertEqual(p.plan.stop_price, 108.0)

    def test_the_trail_holds_as_price_falls_back(self):
        p = position(entry=100.0, stop=97.0, trailing_stop_pct=3.0)
        for price in (103.0, 106.0, 104.0, 101.0):
            update_high_water(p, price)
            apply_trailing_stop(p)
        self.assertEqual(p.high_water_price, 106.0)
        self.assertAlmostEqual(p.plan.stop_price, 102.82, places=4)


class TestFailingOutwardNotInward(unittest.TestCase):
    """Missing data must force an exit, never a hold."""

    def test_no_price_forces_an_exit(self):
        """
        An open position whose price cannot be established is holding
        unbounded risk. Holding through that is the failure mode; out is
        the safe direction.
        """
        intent = evaluate(position(), ExitContext(price=None))
        self.assertIsNotNone(intent)
        self.assertEqual(intent.primary_reason, ExitReason.HARD_STOP)
        self.assertTrue(intent.protective)

    def test_a_stale_quote_forces_an_exit(self):
        intent = evaluate(position(), ExitContext(
            price=100.0, quote_age_seconds=MAX_QUOTE_AGE_SECONDS + 1))
        self.assertIsNotNone(intent)
        self.assertEqual(intent.primary_reason, ExitReason.HARD_STOP)

    def test_a_fresh_quote_at_a_safe_price_does_not_force_an_exit(self):
        """The falsifying control for the two tests above."""
        self.assertIsNone(evaluate(position(), ExitContext(
            price=100.0, quote_age_seconds=5.0)))

    def test_an_unreadable_halt_state_assumes_halted(self):
        intent = evaluate(position(), ExitContext(
            price=100.0, halt_state_readable=False))
        self.assertEqual(intent.primary_reason, ExitReason.GLOBAL_HALT)

    def test_a_global_halt_exits(self):
        intent = evaluate(position(), ExitContext(price=100.0,
                                                  global_halt=True))
        self.assertEqual(intent.primary_reason, ExitReason.GLOBAL_HALT)

    def test_broker_divergence_exits(self):
        intent = evaluate(position(), ExitContext(price=100.0,
                                                  broker_divergence=True))
        self.assertEqual(intent.primary_reason, ExitReason.BROKER_DIVERGENCE)

    def test_an_unknown_position_state_exits(self):
        p = position()
        p.state = PositionState.UNKNOWN
        intent = evaluate(p, ExitContext(price=100.0))
        self.assertEqual(intent.primary_reason, ExitReason.BROKER_DIVERGENCE)


class TestExitRules(unittest.TestCase):

    def test_price_at_the_stop_exits(self):
        intent = evaluate(position(stop=97.0), ExitContext(price=97.0))
        self.assertEqual(intent.primary_reason, ExitReason.HARD_STOP)

    def test_price_below_the_stop_exits(self):
        intent = evaluate(position(stop=97.0), ExitContext(price=95.0))
        self.assertEqual(intent.primary_reason, ExitReason.HARD_STOP)

    def test_price_above_the_stop_holds(self):
        self.assertIsNone(evaluate(position(stop=97.0),
                                   ExitContext(price=99.0)))

    def test_the_profit_target_exits(self):
        intent = evaluate(position(target_price=110.0),
                          ExitContext(price=110.5))
        self.assertEqual(intent.primary_reason, ExitReason.PROFIT_TARGET)

    def test_the_time_stop_exits(self):
        intent = evaluate(position(max_hold_minutes=60),
                          ExitContext(price=100.0, minutes_held=61))
        self.assertEqual(intent.primary_reason, ExitReason.TIME_STOP)

    def test_an_invalidated_thesis_exits(self):
        intent = evaluate(position(), ExitContext(
            price=100.0, thesis_invalidated=True,
            thesis_detail="the catalyst was retracted"))
        self.assertEqual(intent.primary_reason, ExitReason.THESIS_INVALIDATED)
        self.assertIn("retracted", intent.detail)

    def test_the_daily_loss_limit_exits(self):
        intent = evaluate(position(), ExitContext(price=100.0,
                                                  daily_loss_breached=True))
        self.assertEqual(intent.primary_reason, ExitReason.DAILY_LOSS_LIMIT)

    def test_the_close_flattens(self):
        """
        This system's risk model assumes no overnight gap risk, so it
        must not carry any. A stop does nothing between the close and
        the open.
        """
        intent = evaluate(position(flatten_before_close_minutes=10),
                          ExitContext(price=100.0, minutes_to_close=5))
        self.assertEqual(intent.primary_reason, ExitReason.END_OF_DAY)

    def test_well_before_the_close_does_not_flatten(self):
        self.assertIsNone(evaluate(
            position(flatten_before_close_minutes=10),
            ExitContext(price=100.0, minutes_to_close=120)))

    def test_a_closed_position_is_not_re_evaluated(self):
        p = position()
        p.state = PositionState.CLOSED
        self.assertIsNone(evaluate(p, ExitContext(price=1.0)))


class TestReasonsStaySeparate(unittest.TestCase):
    """No hidden combined score: every reason is kept."""

    def test_all_firing_reasons_are_recorded(self):
        intent = evaluate(
            position(stop=97.0, max_hold_minutes=60),
            ExitContext(price=95.0, minutes_held=200, minutes_to_close=5,
                        thesis_invalidated=True))
        self.assertIn(ExitReason.HARD_STOP, intent.all_reasons)
        self.assertIn(ExitReason.TIME_STOP, intent.all_reasons)
        self.assertIn(ExitReason.END_OF_DAY, intent.all_reasons)
        self.assertIn(ExitReason.THESIS_INVALIDATED, intent.all_reasons)

    def test_the_most_protective_reason_becomes_primary(self):
        intent = evaluate(
            position(stop=97.0, max_hold_minutes=60),
            ExitContext(price=95.0, minutes_held=200, global_halt=True))
        self.assertEqual(intent.primary_reason, ExitReason.GLOBAL_HALT)

    def test_an_invalidated_thesis_outranks_a_profit_target(self):
        """
        The one case where the order reasons are DETECTED differs from
        the order they are RANKED: the profit target is checked before
        the thesis, but ranks below it. It matters, because recording
        this exit as PROFIT_TARGET would credit the strategy with a win
        its own logic did not earn - price merely happened to touch the
        target while the reason for the trade was collapsing.
        """
        intent = evaluate(
            position(target_price=110.0),
            ExitContext(price=111.0, thesis_invalidated=True,
                        thesis_detail="the catalyst was retracted"))
        self.assertIn(ExitReason.PROFIT_TARGET, intent.all_reasons)
        self.assertEqual(intent.primary_reason,
                         ExitReason.THESIS_INVALIDATED)
        self.assertTrue(intent.protective)

    def test_the_primary_reason_is_chosen_by_rank_not_detection_order(self):
        """
        Generalises the case above: whatever order reasons happen to be
        appended in, the primary must be the one ranked highest.
        """
        intent = evaluate(
            position(target_price=110.0, max_hold_minutes=10),
            ExitContext(price=111.0, minutes_held=999,
                        thesis_invalidated=True))
        best = min(intent.all_reasons, key=EXIT_PRIORITY.index)
        self.assertEqual(intent.primary_reason, best)

    def test_reasons_are_returned_in_priority_order(self):
        intent = evaluate(
            position(stop=97.0, max_hold_minutes=60),
            ExitContext(price=95.0, minutes_held=200, minutes_to_close=5))
        indices = [EXIT_PRIORITY.index(r) for r in intent.all_reasons]
        self.assertEqual(indices, sorted(indices))

    def test_reasons_are_not_duplicated(self):
        intent = evaluate(position(stop=97.0), ExitContext(price=95.0))
        self.assertEqual(len(intent.all_reasons), len(set(intent.all_reasons)))

    def test_every_exit_reason_appears_in_the_priority_order(self):
        """
        Otherwise _primary would fall through to an arbitrary reason and
        the choice would stop being deterministic.
        """
        for reason in ExitReason:
            with self.subTest(reason=reason):
                self.assertIn(reason, EXIT_PRIORITY)

    def test_the_priority_order_has_no_duplicates(self):
        self.assertEqual(len(EXIT_PRIORITY), len(set(EXIT_PRIORITY)))

    def test_risk_driven_reasons_are_marked_protective(self):
        """
        Protective exits must not be gated by the checks that govern new
        exposure. A system that can open a position but not close one is
        far more dangerous than one that can do neither.
        """
        for reason in (ExitReason.GLOBAL_HALT, ExitReason.HARD_STOP,
                       ExitReason.DAILY_LOSS_LIMIT,
                       ExitReason.BROKER_DIVERGENCE,
                       ExitReason.TRAILING_STOP, ExitReason.END_OF_DAY):
            with self.subTest(reason=reason):
                self.assertIn(reason, PROTECTIVE_REASONS)

    def test_a_profit_target_is_not_protective(self):
        """The falsifying control: not everything is protective."""
        self.assertNotIn(ExitReason.PROFIT_TARGET, PROTECTIVE_REASONS)


class TestRiskArithmetic(unittest.TestCase):

    def test_risk_per_share_is_entry_minus_stop(self):
        self.assertAlmostEqual(
            position(entry=100.0, stop=97.0).risk_per_share, 3.0, places=6)

    def test_open_risk_shrinks_as_price_rises(self):
        p = position(entry=100.0, stop=97.0, quantity=10.0)
        p.current_price = 105.0
        self.assertAlmostEqual(p.open_risk, 80.0, places=4)

    def test_open_risk_is_negative_once_the_stop_is_above_entry(self):
        """A stop above entry means the worst case is a profit."""
        p = position(entry=100.0, stop=103.0, quantity=1.0)
        p.current_price = 105.0
        self.assertAlmostEqual(p.open_risk, 2.0, places=4)
        p.current_price = 103.0
        self.assertAlmostEqual(p.open_risk, 0.0, places=4)

    def test_open_risk_is_none_without_a_price(self):
        p = position()
        p.current_price = None
        self.assertIsNone(p.open_risk)

    def test_total_open_risk_is_none_if_any_position_is_unpriceable(self):
        """
        A total that silently omits an unpriceable position understates
        risk, and an understated risk total is worse than none.
        """
        pm = PositionManager(broker=broker(), execution_available=True)
        b = broker()
        pm._positions["XYZ"] = position()
        pm._positions["AAA"] = ManagedPosition(
            position_id="p2", symbol="AAA", quantity=1.0, entry_price=50.0,
            plan=ExitPlan(stop_price=48.0))
        pm._positions["AAA"].current_price = None
        self.assertIsNone(pm.total_open_risk())

    def test_total_open_risk_sums_when_all_are_priceable(self):
        pm = PositionManager()
        p = position(entry=100.0, stop=97.0, quantity=1.0)
        p.current_price = 100.0
        pm._positions["XYZ"] = p
        self.assertAlmostEqual(pm.total_open_risk(), 3.0, places=4)


class TestOpeningPositions(unittest.TestCase):

    def test_a_filled_order_becomes_a_managed_position(self):
        b = broker()
        order = b.submit_order("XYZ", "BUY", 2.0)
        pm = PositionManager(broker=b, execution_available=True)
        p = pm.open_from_order(order, ExitPlan(stop_price=97.0),
                               hypothesis_id="h1", risk_decision_id="d1")
        self.assertAlmostEqual(p.quantity, 2.0, places=6)
        self.assertEqual(p.hypothesis_id, "h1")
        self.assertEqual(p.entry_order_id, order["order_id"])

    def test_an_unfilled_order_cannot_be_managed(self):
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0, order_type="LIMIT",
                               limit_price=50.0)
        pm = PositionManager(broker=b)
        with self.assertRaises(PositionManagerError):
            pm.open_from_order(order, ExitPlan(stop_price=45.0))

    def test_a_stop_at_or_above_the_entry_is_refused(self):
        """That is not a stop, it is an immediate exit."""
        b = broker()
        order = b.submit_order("XYZ", "BUY", 1.0)
        pm = PositionManager(broker=b)
        with self.assertRaises(PositionManagerError):
            pm.open_from_order(order, ExitPlan(stop_price=200.0))

    def test_a_partially_filled_entry_manages_only_what_was_filled(self):
        b = broker(partial=1.0)
        order = b.submit_order("XYZ", "BUY", 4.0)
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        pm = PositionManager(broker=b, execution_available=True)
        p = pm.open_from_order(order, ExitPlan(stop_price=97.0))
        self.assertAlmostEqual(p.quantity, order["filled_quantity"],
                               places=6)

    def test_a_partially_filled_entry_has_its_remainder_cancelled(self):
        """
        A remainder left working would fill later, diverge from the
        agent's quantity and halt everything over a routine partial
        fill - and it could never be managed, because a second position
        in the same name is refused.
        """
        b = broker(partial=1.0)
        order = b.submit_order("XYZ", "BUY", 4.0)
        self.assertGreater(order["remaining_quantity"], 0)
        pm = PositionManager(broker=b, execution_available=True)
        pm.open_from_order(order, ExitPlan(stop_price=97.0))
        self.assertEqual(b.get_order(order["order_id"])["status"],
                         "CANCELLED")

    def test_a_partial_entry_reconciles_after_the_remainder_is_cancelled(self):
        """The point of cancelling: the two views agree afterwards."""
        b = broker(partial=1.0)
        pm = PositionManager(broker=b, execution_available=True)
        pm.open_from_order(b.submit_order("XYZ", "BUY", 4.0),
                           ExitPlan(stop_price=97.0))
        self.assertTrue(pm.reconcile().matched)
        self.assertFalse(pm.halted)

    def test_an_uncancellable_remainder_fails_closed(self):
        class Stubborn:
            def __init__(self, inner):
                self._inner = inner
            def __getattr__(self, name):
                return getattr(self._inner, name)
            def cancel_order(self, order_id):
                raise RuntimeError("cancel rejected")
        b = broker(partial=1.0)
        order = b.submit_order("XYZ", "BUY", 4.0)
        pm = PositionManager(broker=Stubborn(b), execution_available=True)
        with self.assertRaises(PositionManagerError) as caught:
            pm.open_from_order(order, ExitPlan(stop_price=97.0))
        self.assertIn("remainder", str(caught.exception))

    def test_a_second_position_in_the_same_name_is_refused(self):
        """
        Averaging down is forbidden by the risk limits, and a second
        entry would also break the one-plan-per-position model the exit
        engine assumes.
        """
        b = broker()
        pm = PositionManager(broker=b)
        pm.open_from_order(b.submit_order("XYZ", "BUY", 1.0),
                           ExitPlan(stop_price=97.0))
        with self.assertRaises(PositionManagerError):
            pm.open_from_order(b.submit_order("XYZ", "BUY", 1.0),
                               ExitPlan(stop_price=97.0))


class TestReconciliation(unittest.TestCase):
    """The broker is authoritative; disagreement is unsafe."""

    def test_matching_views_reconcile(self):
        b = broker()
        pm = PositionManager(broker=b, execution_available=True)
        pm.open_from_order(b.submit_order("XYZ", "BUY", 1.0),
                           ExitPlan(stop_price=97.0))
        result = pm.reconcile()
        self.assertTrue(result.matched)
        self.assertTrue(result.safe_to_trade)
        self.assertFalse(pm.halted)

    def test_a_position_the_broker_does_not_have_halts(self):
        b = broker()
        pm = PositionManager(broker=b, execution_available=True)
        pm.open_from_order(b.submit_order("XYZ", "BUY", 1.0),
                           ExitPlan(stop_price=97.0))
        b._positions.clear()
        result = pm.reconcile()
        self.assertFalse(result.matched)
        self.assertEqual(result.agent_only, ["XYZ"])
        self.assertTrue(pm.halted)

    def test_a_position_the_agent_does_not_have_halts(self):
        b = broker()
        b.submit_order("XYZ", "BUY", 1.0)
        pm = PositionManager(broker=b, execution_available=True)
        result = pm.reconcile()
        self.assertFalse(result.matched)
        self.assertEqual(result.broker_only, ["XYZ"])
        self.assertTrue(pm.halted)

    def test_a_quantity_mismatch_halts(self):
        b = broker()
        pm = PositionManager(broker=b, execution_available=True)
        pm.open_from_order(b.submit_order("XYZ", "BUY", 2.0),
                           ExitPlan(stop_price=97.0))
        pm._positions["XYZ"].quantity = 1.0
        result = pm.reconcile()
        self.assertFalse(result.matched)
        self.assertEqual(len(result.quantity_mismatches), 1)
        self.assertTrue(pm.halted)

    def test_no_broker_is_an_unknown_state_and_halts(self):
        """Uncertain brokerage state is a halt condition, not a default."""
        pm = PositionManager(broker=None)
        result = pm.reconcile()
        self.assertFalse(result.matched)
        self.assertTrue(pm.halted)

    def test_an_unreadable_broker_halts(self):
        class Broken:
            def get_positions(self):
                raise RuntimeError("connection lost")
        pm = PositionManager(broker=Broken())
        result = pm.reconcile()
        self.assertFalse(result.matched)
        self.assertTrue(pm.halted)
        self.assertIn("connection lost", result.detail)

    def test_safe_to_trade_is_derived_not_assigned(self):
        """There is no field to set to make an unsafe state look safe."""
        from agent.positions.models import ReconciliationResult
        result = ReconciliationResult(matched=False)
        self.assertFalse(result.safe_to_trade)
        with self.assertRaises(AttributeError):
            result.safe_to_trade = True

    def test_a_halted_manager_marks_positions_for_divergence_exit(self):
        b = broker()
        pm = PositionManager(broker=b, execution_available=True)
        pm.open_from_order(b.submit_order("XYZ", "BUY", 1.0),
                           ExitPlan(stop_price=97.0))
        b._positions.clear()
        pm.reconcile()
        intents = pm.evaluate_exits({"XYZ": ExitContext(price=100.0)})
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].primary_reason,
                         ExitReason.BROKER_DIVERGENCE)


class TestExitSubmission(unittest.TestCase):

    def _manager(self, execution_available=True):
        b = broker()
        pm = PositionManager(broker=b, execution_available=execution_available)
        pm.open_from_order(b.submit_order("XYZ", "BUY", 1.0),
                           ExitPlan(stop_price=97.0))
        return b, pm

    def test_an_exit_closes_the_position(self):
        b, pm = self._manager()
        intent = evaluate(pm.get("XYZ"), ExitContext(price=96.0))
        order = pm.submit_exit(intent)
        self.assertEqual(order["status"], "FILLED")
        self.assertIsNone(pm.get("XYZ"))
        self.assertEqual(len(pm.closed_positions()), 1)

    def test_the_exit_reasons_survive_on_the_closed_position(self):
        b, pm = self._manager()
        intent = evaluate(pm.get("XYZ"), ExitContext(price=96.0))
        pm.submit_exit(intent)
        self.assertIn(ExitReason.HARD_STOP,
                      pm.closed_positions()[0].exit_reasons)

    def test_execution_unavailable_refuses_and_says_risk_is_live(self):
        """
        An open position that cannot be closed is a distinct and more
        serious condition than an entry that cannot be opened. The
        message must say so rather than read as a routine refusal.
        """
        b, pm = self._manager(execution_available=False)
        intent = evaluate(pm.get("XYZ"), ExitContext(price=96.0))
        with self.assertRaises(PositionManagerError) as caught:
            pm.submit_exit(intent)
        self.assertIn("remains at risk", str(caught.exception))
        self.assertIsNotNone(pm.get("XYZ"))

    def test_an_exit_for_an_unknown_symbol_raises(self):
        _b, pm = self._manager()
        intent = evaluate(pm.get("XYZ"), ExitContext(price=96.0))
        intent.symbol = "NOPE"
        with self.assertRaises(PositionManagerError):
            pm.submit_exit(intent)

    def test_evaluating_exits_marks_the_position_exiting(self):
        _b, pm = self._manager()
        pm.evaluate_exits({"XYZ": ExitContext(price=96.0)})
        self.assertEqual(pm.get("XYZ").state, PositionState.EXITING)

    def test_a_position_with_no_context_is_still_evaluated(self):
        """
        Skipping it would let a position with no data quietly escape its
        stop - the position most in need of evaluation ignored precisely
        because something went wrong.
        """
        _b, pm = self._manager()
        intents = pm.evaluate_exits({})
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].primary_reason, ExitReason.HARD_STOP)

    def test_flatten_all_builds_an_intent_per_position(self):
        _b, pm = self._manager()
        intents = pm.flatten_all(reason=ExitReason.END_OF_DAY)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].primary_reason, ExitReason.END_OF_DAY)
        self.assertTrue(intents[0].protective)

    def test_flatten_all_does_not_consult_the_exit_rules(self):
        """A flatten is an instruction, not a judgement."""
        _b, pm = self._manager()
        pm.get("XYZ").current_price = 500.0      # deeply profitable
        self.assertEqual(len(pm.flatten_all()), 1)


class TestStopGapDisclosure(unittest.TestCase):
    """Say plainly what a stop does and does not guarantee."""

    def test_a_polled_stop_does_not_guarantee_a_trigger(self):
        """
        If the agent is not running, a polled stop does not exist.
        Reporting it as though it filled at the stop price is the most
        common way a paper record overstates a strategy.
        """
        disclosure = stop_gap_disclosure(position(
            stop_mechanism=StopMechanism.ENGINE_POLLED,
            evaluation_interval_seconds=60))
        self.assertFalse(disclosure["guaranteed_trigger"])
        self.assertFalse(disclosure["guaranteed_fill_price"])
        self.assertEqual(disclosure["evaluation_interval_seconds"], 60)

    def test_a_resting_stop_triggers_but_does_not_guarantee_a_price(self):
        disclosure = stop_gap_disclosure(position(
            stop_mechanism=StopMechanism.BROKER_RESTING))
        self.assertTrue(disclosure["guaranteed_trigger"])
        self.assertFalse(disclosure["guaranteed_fill_price"])

    def test_no_mechanism_claims_a_guaranteed_fill_price(self):
        for mechanism in StopMechanism:
            with self.subTest(mechanism=mechanism):
                self.assertFalse(stop_gap_disclosure(
                    position(stop_mechanism=mechanism)
                )["guaranteed_fill_price"])


class TestProvenance(unittest.TestCase):

    def test_a_position_carries_its_reasoning_forward(self):
        b = broker()
        pm = PositionManager(broker=b, execution_available=True)
        p = pm.open_from_order(b.submit_order("XYZ", "BUY", 1.0),
                               ExitPlan(stop_price=97.0),
                               hypothesis_id="h1", risk_decision_id="d1",
                               config_version="cfg-1")
        d = p.as_dict()
        for key in ("hypothesis_id", "risk_decision_id", "entry_order_id",
                    "config_version", "position_id"):
            with self.subTest(key=key):
                self.assertIsNotNone(d[key])

    def test_an_exit_intent_records_its_configuration_version(self):
        intent = evaluate(position(stop=97.0), ExitContext(price=96.0))
        self.assertTrue(intent.config_version)

    def test_the_snapshot_reports_the_halt_state(self):
        pm = PositionManager(broker=None)
        pm.reconcile()
        snapshot = pm.snapshot()
        self.assertTrue(snapshot["halted"])
        self.assertTrue(snapshot["halt_detail"])


class TestDeterminism(unittest.TestCase):

    def test_the_same_inputs_give_the_same_decision(self):
        def run():
            intent = evaluate(
                position(stop=97.0, max_hold_minutes=60),
                ExitContext(price=95.0, minutes_held=200, minutes_to_close=5))
            return (intent.primary_reason,
                    tuple(intent.all_reasons), intent.detail)
        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
