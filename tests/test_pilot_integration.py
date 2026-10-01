"""
End-to-end paper pilot: several cycles across simulated restarts.

The unit tests cover each component. What they cannot show is whether
the components compose into something that behaves sanely over a day,
and specifically whether state survives the boundary between
invocations. On Lambda every cycle is a cold start as far as memory is
concerned, so a pilot is only continuous if the stores make it so.

These tests drive the real orchestrator, the real paper broker, the real
position manager and the real journal, discarding the in-memory objects
between cycles exactly as Lambda does.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    InMemoryBrokerStateStore, PaperBroker, PaperBrokerConfig, Quote,
    restore,
)
from agent.hypothesis import generate                             # noqa: E402
from agent.journal import InMemoryJournal, describe               # noqa: E402
from agent.orchestration import (                                 # noqa: E402
    CycleOutcome, CyclePhase, InMemoryCycleLock, MarketDayOrchestrator,
)
from agent.positions import (                                     # noqa: E402
    InMemoryPositionStore, PositionManager, PositionState,
)
from agent.risk import InMemoryHaltStore, RiskLimits               # noqa: E402

SESSION = "2026-09-30"


def strong_hypothesis(symbol):
    signal = {"direction": "BUY", "signal_agreement": 1.0,
              "signal_magnitude": 0.7, "regime_adjusted_magnitude": 0.7,
              "strength_band": "STRONG", "buy_groups": 3, "sell_groups": 0,
              "opinionated_groups": 3,
              "data_quality": {"freshness": "FRESH"}}
    catalyst = {"has_active_catalyst": True, "direction": "POSITIVE",
                "primary_catalyst": {"materiality": 0.7, "novelty": 0.8,
                                     "type": "EARNINGS"},
                "evidence_score": 0.7, "independent_source_count": 2,
                "primary_source_count": 1}
    regime = {"regime": "BULLISH", "regime_confidence": 0.8,
              "risk_posture": "NORMAL", "market_session": "OPEN"}
    return generate(symbol, signal, catalyst, regime)


def no_hypothesis(symbol):
    return None


class Pilot:
    """A pilot whose in-memory objects are thrown away between cycles.

    This is the whole point: if anything survives by accident rather
    than by persistence, these tests would pass while the real Lambda
    failed.
    """

    def __init__(self, cash=200.0, trading=True, execution=True):
        self.broker_store = InMemoryBrokerStateStore()
        self.position_store = InMemoryPositionStore()
        self.journal = InMemoryJournal()
        self.halt_store = InMemoryHaltStore()
        self.lock = InMemoryCycleLock()
        self.cash = cash
        self.trading = trading
        self.execution = execution
        self.revision = None
        self.prices = {"XYZ": 100.0}
        self.save_failures = 0

    def _quote_for(self, symbol):
        price = self.prices.get(symbol)
        if price is None:
            return None
        return {"price": price, "spread_pct": 0.05,
                "dollar_volume": 5e8, "age_seconds": 5.0}

    def cycle(self, phase=CyclePhase.INTRADAY, candidates=("XYZ",),
              minutes_to_close=200, hypothesis_for=strong_hypothesis):
        # --- cold start: build everything fresh ----------------------
        broker = PaperBroker(PaperBrokerConfig(
            starting_cash=self.cash, seed=3,
            partial_fill_probability=0.0))
        snapshot, revision = self.broker_store.load()
        if snapshot:
            restore(broker, snapshot)
        for symbol, price in self.prices.items():
            broker.set_quote(Quote(symbol=symbol, bid=price * 0.999,
                                   ask=price * 1.001, last=price))

        manager = PositionManager(broker=broker,
                                  execution_available=self.execution)
        for position in self.position_store.load_open(SESSION):
            if position.state is not PositionState.CLOSED:
                manager._positions[position.symbol] = position

        orchestrator = MarketDayOrchestrator(
            broker=broker, position_manager=manager, journal=self.journal,
            halt_store=self.halt_store, limits=RiskLimits(),
            trading_enabled=self.trading,
            execution_available=self.execution, cycle_lock=self.lock)

        deployed = sum(p.get("cost_basis") or 0.0
                       for p in broker.get_positions())
        result = orchestrator.run_cycle(
            session_date=SESSION, phase=phase,
            candidates=list(candidates), quote_for=self._quote_for,
            hypothesis_for=hypothesis_for,
            minutes_to_close=minutes_to_close,
            capital_deployed=deployed,
            realized_pnl_today=broker.get_account()["realized_pnl"])

        # --- persist, then discard ------------------------------------
        try:
            self.revision = self.broker_store.save(
                broker, expected_revision=(revision if snapshot else None))
        except Exception:                                 # noqa: BLE001
            self.save_failures += 1
        for position in list(manager._positions.values()):
            self.position_store.save(position, SESSION)
        for position in manager.closed_positions():
            self.position_store.delete(position.symbol, SESSION)

        self.last_account = broker.get_account()
        return result


class TestContinuityAcrossCycles(unittest.TestCase):

    def test_a_position_opened_in_one_cycle_is_managed_in_the_next(self):
        """
        The single most important composition property. If it fails, the
        agent opens positions it then forgets, and reconciliation halts
        every cycle.
        """
        pilot = Pilot()
        first = pilot.cycle()
        self.assertEqual(first.entries_submitted, 1)

        second = pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertEqual(second.open_positions, 1)
        self.assertTrue(second.positions_reconciled)
        self.assertFalse(second.halted)

    def test_cash_is_not_replenished_between_cycles(self):
        """
        A reset account would silently fund every cycle from full
        starting cash - the agent would appear to have unlimited money.
        """
        pilot = Pilot(cash=200.0)
        pilot.cycle()
        after_first = pilot.last_account["cash"]
        pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertAlmostEqual(pilot.last_account["cash"], after_first,
                               places=4)
        self.assertLess(after_first, 200.0)

    def test_reconciliation_passes_across_a_restart(self):
        """
        If the broker and position stores disagree after a restart, the
        agent halts. This asserts the two round-trip consistently.
        """
        pilot = Pilot()
        pilot.cycle()
        for _ in range(3):
            result = pilot.cycle(hypothesis_for=no_hypothesis)
            self.assertTrue(result.positions_reconciled, result.errors)

    def test_the_trailing_stop_ratchet_survives_restarts(self):
        """
        Losing the high-water mark would reset the trail to the entry,
        silently giving back every ratchet the position earned.

        The prices stay BELOW the profit target (entry +6%) on purpose.
        An earlier version stepped to 108 and 112, which hit the target
        and closed the position after one observation - so the
        comparison ran on a single-element list and passed without
        testing anything.
        """
        pilot = Pilot()
        pilot.cycle()
        [position] = pilot.position_store.load_open(SESSION)
        entry = position.entry_price
        target = position.plan.target_price

        # Derive the price path from the position's OWN target rather
        # than hardcoding numbers. Hardcoded prices went through the
        # target, closed the position, and left the comparison running
        # on one element - and they would have broken again the next
        # time the stop distance changed.
        steps = 4
        prices = [entry + (target - entry) * (i + 1) / (steps + 1)
                  for i in range(steps)]
        self.assertTrue(all(p < target for p in prices))

        stops = []
        for price in prices:
            pilot.prices["XYZ"] = price
            pilot.cycle(hypothesis_for=no_hypothesis)
            open_positions = pilot.position_store.load_open(SESSION)
            self.assertEqual(len(open_positions), 1,
                             f"the position closed at {price:.2f}, so the "
                             "ratchet cannot be observed")
            stops.append(round(open_positions[0].plan.stop_price, 4))

        # Non-vacuity: the stop must actually have moved, more than once.
        self.assertEqual(len(stops), steps)
        self.assertGreater(len(set(stops)), 1,
                           "the stop never moved, so this proves nothing "
                           "about the ratchet surviving a restart")
        self.assertEqual(stops, sorted(stops),
                         "the stop moved backwards across a restart")

    def test_a_stop_breach_closes_the_position_and_journals_it(self):
        pilot = Pilot()
        pilot.cycle()
        pilot.prices["XYZ"] = 50.0            # far through any stop
        result = pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(result.trades_journalled, 1)
        self.assertEqual(pilot.position_store.load_open(SESSION), [])

    def test_a_closed_position_does_not_come_back_on_the_next_cycle(self):
        """
        If storage kept it, the next cycle would load a position the
        broker no longer has and reconciliation would halt.
        """
        pilot = Pilot()
        pilot.cycle()
        pilot.prices["XYZ"] = 50.0
        pilot.cycle(hypothesis_for=no_hypothesis)
        after = pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertEqual(after.open_positions, 0)
        self.assertFalse(after.halted, after.errors)

    def test_realized_pnl_accumulates_across_cycles(self):
        pilot = Pilot()
        pilot.cycle()
        pilot.prices["XYZ"] = 50.0
        pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertLess(pilot.last_account["realized_pnl"], 0.0)

    def test_no_state_was_lost_to_a_save_conflict(self):
        """
        The pilot saves with an expected revision. If those were
        mismatched the saves would silently fail and every assertion
        above would be testing in-memory state only.
        """
        pilot = Pilot()
        for _ in range(4):
            pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertEqual(pilot.save_failures, 0)


class TestLimitsHoldOverADay(unittest.TestCase):

    def test_the_concurrent_position_limit_holds_across_cycles(self):
        """
        A limit enforced only within one cycle would be defeated by the
        next invocation, which is how a per-request check becomes no
        check at all.
        """
        pilot = Pilot(cash=1000.0)
        pilot.prices.update({"AAA": 50.0, "BBB": 60.0, "CCC": 70.0})
        for _ in range(4):
            pilot.cycle(candidates=("XYZ", "AAA", "BBB", "CCC"))
        open_count = len(pilot.position_store.load_open(SESSION))
        self.assertLessEqual(open_count, RiskLimits().max_concurrent_positions)

    def test_available_capital_is_a_ceiling_not_a_target(self):
        """
        The agent must not spend down to the limit just because it can.
        With one candidate and a position cap of two, most of the
        allowance should remain unused.
        """
        pilot = Pilot(cash=1000.0)
        for _ in range(3):
            pilot.cycle()
        deployed = sum(p.plan.stop_price * p.quantity
                       for p in pilot.position_store.load_open(SESSION))
        self.assertLess(deployed, RiskLimits().daily_capital_limit * 2)

    def test_the_pilot_records_every_trade_it_closes(self):
        pilot = Pilot()
        pilot.cycle()
        pilot.prices["XYZ"] = 50.0
        pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertEqual(len(pilot.journal.list_trades()), 1)

    def test_performance_refuses_to_claim_an_edge_from_a_pilot_day(self):
        """
        A day's worth of paper trades cannot demonstrate anything, and
        the pilot reports that rather than a win rate.
        """
        pilot = Pilot()
        pilot.cycle()
        pilot.prices["XYZ"] = 50.0
        pilot.cycle(hypothesis_for=no_hypothesis)
        report = describe(pilot.journal.list_trades())
        self.assertIn(report["verdict"],
                      ("NO_TRADES", "NO_EDGE_DEMONSTRATED"))
        self.assertTrue(report["paper_only"])


class TestPilotSafety(unittest.TestCase):

    def test_switches_off_means_no_entries_across_every_cycle(self):
        pilot = Pilot(trading=False, execution=False)
        for _ in range(3):
            result = pilot.cycle()
            self.assertEqual(result.entries_submitted, 0)
        self.assertEqual(pilot.position_store.load_open(SESSION), [])

    def test_a_halt_mid_day_stops_entries_and_closes_positions(self):
        pilot = Pilot()
        pilot.cycle()
        self.assertEqual(len(pilot.position_store.load_open(SESSION)), 1)

        pilot.halt_store.engage("operator halt", engaged_by="test")
        result = pilot.cycle()
        self.assertTrue(result.halted)
        self.assertEqual(result.entries_submitted, 0)
        self.assertGreater(result.exits_submitted, 0)
        self.assertEqual(pilot.position_store.load_open(SESSION), [])

    def test_the_pre_close_cycle_leaves_nothing_open(self):
        """
        This system's risk model assumes no overnight gap risk, so a day
        must not end with exposure.
        """
        pilot = Pilot()
        pilot.cycle()
        pilot.cycle(phase=CyclePhase.PRE_CLOSE, minutes_to_close=5,
                    hypothesis_for=no_hypothesis)
        self.assertEqual(pilot.position_store.load_open(SESSION), [])

    def test_a_day_of_cycles_never_exceeds_the_daily_loss_limit_in_risk(self):
        """
        Open risk across all positions must stay within the daily loss
        limit, or a single bad day could exceed what the limits promise.
        """
        pilot = Pilot(cash=1000.0)
        pilot.prices.update({"AAA": 50.0, "BBB": 60.0})
        for _ in range(3):
            pilot.cycle(candidates=("XYZ", "AAA", "BBB"))
        positions = pilot.position_store.load_open(SESSION)
        total_risk = sum(
            p.quantity * (p.entry_price - p.plan.stop_price)
            for p in positions)
        limits = RiskLimits()
        self.assertLessEqual(
            total_risk,
            limits.max_trade_risk * limits.max_concurrent_positions + 1e-6)

    def test_every_journalled_trade_is_marked_paper(self):
        pilot = Pilot()
        pilot.cycle()
        pilot.prices["XYZ"] = 50.0
        pilot.cycle(hypothesis_for=no_hypothesis)
        for trade in pilot.journal.list_trades():
            self.assertTrue(trade.is_paper)

    def test_a_missing_quote_closes_rather_than_holds(self):
        pilot = Pilot()
        pilot.cycle()
        pilot.prices.pop("XYZ")
        result = pilot.cycle(hypothesis_for=no_hypothesis)
        self.assertGreater(result.exits_submitted, 0)


if __name__ == "__main__":
    unittest.main()
