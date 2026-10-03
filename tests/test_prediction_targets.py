"""Targets, and the lookahead boundary they must not cross.

The tests that matter here are the ones built on a GAP: a series where
bar N's close differs from bar N+1's open. On a continuous series the
two prices are equal, so a target computed from the wrong one produces
the right answer and every test passes while the system is broken. The
gap is what makes the boundary observable at all.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from agent.prediction import (FORBIDDEN_IMPORTS, PREDICTION_MODE)
from agent.prediction.targets import (
    AMBIGUOUS_SAME_BAR, DOWN, FLAT, NEITHER, STOP_FIRST, TARGET_FIRST, UP,
    direction, entry_price, excursions, forward_return, resolve_all,
    stop_before_target)


@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float = 1_000_000.0


def flat_series(n: int, price: float = 100.0):
    return [Bar(price, price, price, price) for _ in range(n)]


def gapped_series():
    """Bar 0 closes at 100; bar 1 OPENS at 110 - a 10% gap.

    Thereafter flat at 110. So:
      - measured from the fill (bar 1 open = 110): 0% return
      - measured from the decision close (100): +10% return
    Any target reporting +10% is reading a price the agent could not
    have traded at.
    """
    return [
        Bar(100.0, 100.0, 100.0, 100.0),
        Bar(110.0, 110.0, 110.0, 110.0),
        Bar(110.0, 110.0, 110.0, 110.0),
        Bar(110.0, 110.0, 110.0, 110.0),
    ]


class TestTheFillBoundary(unittest.TestCase):
    """The single property everything else depends on."""

    def test_entry_is_the_next_open_not_the_decision_close(self):
        bars = gapped_series()
        # The decision bar closed at 100. The agent fills at 110.
        self.assertEqual(entry_price(bars, 0), 110.0)
        self.assertNotEqual(entry_price(bars, 0), bars[0].close)

    def test_forward_return_measured_from_the_fill_sees_no_move(self):
        # If this returns ~10 instead of 0, the target captured the gap
        # the agent could never have traded - the whole failure mode.
        t = forward_return(gapped_series(), 0, horizon_minutes=1,
                           bar_interval_seconds=60.0)
        self.assertTrue(t.resolved)
        self.assertAlmostEqual(t.value, 0.0, places=6)

    def test_a_decision_on_the_final_bar_has_no_tradeable_price(self):
        bars = flat_series(3)
        self.assertIsNone(entry_price(bars, 2))
        t = forward_return(bars, 2, 1)
        self.assertFalse(t.resolved)
        self.assertEqual(t.reason, "NO_TRADEABLE_ENTRY_PRICE")

    def test_excursions_are_relative_to_the_fill_too(self):
        # Fill at 110; the high is also 110, so no favorable excursion.
        # Measured from 100 it would read +10%.
        mfe, _ = excursions(gapped_series(), 0, 1)
        self.assertAlmostEqual(mfe.value, 0.0, places=6)

    def test_stop_before_target_levels_come_off_the_fill(self):
        # Fill 110, stop 3% -> 106.7. A low of 107 must NOT be a stop.
        # Off a 100 entry the stop would be 97 and 107 looks safe too,
        # so the discriminating bar is one that straddles only one of
        # the two candidate stop levels.
        bars = [
            Bar(100.0, 100.0, 100.0, 100.0),
            Bar(110.0, 110.0, 110.0, 110.0),
            Bar(110.0, 110.0, 107.0, 108.0),   # below a 100-based
        ]                                      # target? no - above a
        t = stop_before_target(bars, 0)        # 110-based stop.
        self.assertEqual(t.value, NEITHER)


class TestUnresolvedIsNotZero(unittest.TestCase):

    def test_horizon_past_the_data_is_unresolved_with_a_reason(self):
        t = forward_return(flat_series(5), 0, horizon_minutes=60,
                           bar_interval_seconds=60.0)
        self.assertFalse(t.resolved)
        self.assertIsNone(t.value)
        self.assertEqual(t.reason, "HORIZON_EXCEEDS_AVAILABLE_DATA")

    def test_a_short_window_is_refused_rather_than_truncated(self):
        # 10 bars available, 60 asked for. Answering with what exists
        # would relabel a 60-minute target as a 10-minute one.
        self.assertFalse(forward_return(flat_series(11), 0, 60).resolved)

    def test_a_genuine_zero_return_stays_resolved(self):
        # The distinction unresolved-vs-zero only means something if
        # real zeros survive as values.
        t = forward_return(flat_series(5), 0, 1)
        self.assertTrue(t.resolved)
        self.assertEqual(t.value, 0.0)

    def test_an_unreadable_bar_inside_the_window_voids_the_extreme(self):
        bars = flat_series(4)
        bars[2] = Bar(100.0, None, 100.0, 100.0)   # type: ignore[arg-type]
        mfe, mae = excursions(bars, 0, 2, 60.0)
        self.assertFalse(mfe.resolved)
        self.assertFalse(mae.resolved)
        self.assertEqual(mfe.reason, "INCOMPLETE_BARS_IN_HORIZON")

    def test_resolve_all_returns_the_unresolved_ones_too(self):
        # Dropping them would make "could not answer" indistinguishable
        # from "never asked".
        out = resolve_all(flat_series(6), 0)
        self.assertTrue(any(not t.resolved for t in out))
        self.assertTrue(any(t.resolved for t in out))
        asked = {(t.name, t.horizon_minutes) for t in out}
        self.assertIn(("FORWARD_RETURN", 60), asked)


class TestDirection(unittest.TestCase):

    def test_flat_is_an_outcome_not_a_missing_value(self):
        t = direction(flat_series(5), 0, 1)
        self.assertTrue(t.resolved)
        self.assertEqual(t.value, FLAT)

    def test_a_move_inside_the_band_is_flat_not_up(self):
        bars = [Bar(100, 100, 100, 100), Bar(100, 100, 100, 100),
                Bar(100, 100.2, 100, 100.2)]
        self.assertEqual(direction(bars, 0, 2, 60.0).value, FLAT)

    def test_up_and_down_above_the_band(self):
        up = [Bar(100, 100, 100, 100), Bar(100, 105, 100, 105),
              Bar(105, 105, 105, 105)]
        self.assertEqual(direction(up, 0, 2, 60.0).value, UP)
        down = [Bar(100, 100, 100, 100), Bar(100, 100, 95, 95),
                Bar(95, 95, 95, 95)]
        self.assertEqual(direction(down, 0, 2, 60.0).value, DOWN)

    def test_direction_cannot_disagree_with_forward_return(self):
        # Derived from one computation, so the pair is consistent by
        # construction. Asserted because two independent computations
        # would be free to drift.
        bars = [Bar(100, 100, 100, 100), Bar(100, 104, 99, 104),
                Bar(104, 104, 104, 104)]
        fr = forward_return(bars, 0, 2, 60.0)
        d = direction(bars, 0, 2, 60.0)
        self.assertGreater(fr.value, 0)
        self.assertEqual(d.value, UP)

    def test_direction_is_unresolved_when_the_return_is(self):
        d = direction(flat_series(5), 0, 60, 60.0)
        self.assertFalse(d.resolved)
        self.assertEqual(d.reason, "HORIZON_EXCEEDS_AVAILABLE_DATA")


class TestStopBeforeTarget(unittest.TestCase):

    def test_stop_first_when_it_arrives_first_in_time(self):
        bars = [Bar(100, 100, 100, 100), Bar(100, 101, 100, 100),
                Bar(100, 100, 96.0, 96.5),     # stop (97) hit here
                Bar(96, 120.0, 96, 120.0)]     # target later - too late
        self.assertEqual(stop_before_target(bars, 0).value, STOP_FIRST)

    def test_extremes_alone_would_get_that_backwards(self):
        # Across the whole window max(high)=120 >= 106 AND
        # min(low)=96 <= 97, so a comparison on extremes could return
        # either. Order in time is what decides it.
        bars = [Bar(100, 100, 100, 100), Bar(100, 101, 100, 100),
                Bar(100, 100, 96.0, 96.5), Bar(96, 120.0, 96, 120.0)]
        self.assertEqual(max(b.high for b in bars[1:]), 120.0)
        self.assertLessEqual(min(b.low for b in bars[1:]), 97.0)
        self.assertEqual(stop_before_target(bars, 0).value, STOP_FIRST)

    def test_target_first(self):
        bars = [Bar(100, 100, 100, 100), Bar(100, 107.0, 100, 107.0),
                Bar(107, 107, 90.0, 90.0)]
        self.assertEqual(stop_before_target(bars, 0).value, TARGET_FIRST)

    def test_a_bar_spanning_both_is_ambiguous_not_a_guess(self):
        # Fill 100, stop 97, target 106. This bar covers 95 to 108.
        bars = [Bar(100, 100, 100, 100), Bar(100, 108.0, 95.0, 100.0)]
        self.assertEqual(stop_before_target(bars, 0).value,
                         AMBIGUOUS_SAME_BAR)

    def test_neither_when_price_stays_inside_both_levels(self):
        bars = [Bar(100, 100, 100, 100), Bar(100, 103, 98, 101),
                Bar(101, 104, 99, 102)]
        self.assertEqual(stop_before_target(bars, 0).value, NEITHER)

    def test_an_unreadable_bar_stops_the_walk_rather_than_skipping_it(self):
        # The missing bar might contain the stop, so a target found
        # after it is not provably first.
        bars = [Bar(100, 100, 100, 100),
                Bar(100, None, 100, 100),      # type: ignore[arg-type]
                Bar(100, 107.0, 100, 107.0)]
        t = stop_before_target(bars, 0)
        self.assertFalse(t.resolved)
        self.assertEqual(t.reason, "INCOMPLETE_BAR_BEFORE_RESOLUTION")

    def test_no_bars_after_entry_is_unresolved(self):
        t = stop_before_target([Bar(100, 100, 100, 100),
                                Bar(100, 100, 100, 100)], 0)
        self.assertEqual(t.value, NEITHER)   # one bar, nothing hit
        t2 = stop_before_target([Bar(100, 100, 100, 100)], 0)
        self.assertFalse(t2.resolved)


class TestCorruptInputIsNotAPrice(unittest.TestCase):

    def test_a_zero_price_is_refused_rather_than_dividing(self):
        bars = [Bar(100, 100, 100, 100), Bar(0.0, 0.0, 0.0, 0.0),
                Bar(100, 100, 100, 100)]
        self.assertIsNone(entry_price(bars, 0))

    def test_a_negative_price_is_refused(self):
        bars = [Bar(100, 100, 100, 100), Bar(-5.0, -5.0, -5.0, -5.0)]
        self.assertIsNone(entry_price(bars, 0))

    def test_a_non_numeric_price_is_refused_not_crashed(self):
        bars = [Bar(100, 100, 100, 100), Bar("abc", 1, 1, 1)]  # type: ignore
        self.assertIsNone(entry_price(bars, 0))

    def test_mapping_bars_work_the_same_as_objects(self):
        # The replay engine hands out objects; a dataset round-trip
        # hands out dicts. Both must resolve identically or a target
        # would depend on which path produced it.
        obj = forward_return(flat_series(5), 0, 1)
        dicts = [{"open": 100.0, "high": 100.0, "low": 100.0,
                  "close": 100.0} for _ in range(5)]
        self.assertEqual(forward_return(dicts, 0, 1).value, obj.value)


class TestResearchOnlyIsStructural(unittest.TestCase):

    def test_the_mode_is_research_only(self):
        self.assertEqual(PREDICTION_MODE, "RESEARCH_ONLY")

    def test_the_package_imports_nothing_that_decides_anything(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        pkg = root / "agent" / "prediction"
        offenders = []
        for path in pkg.rglob("*.py"):
            text = path.read_text()
            for forbidden in FORBIDDEN_IMPORTS:
                # Both import spellings. Checking only "import x" would
                # miss "from x import y", which is the common one.
                if (f"import {forbidden}" in text
                        or f"from {forbidden}" in text):
                    offenders.append(f"{path.name} -> {forbidden}")
        self.assertEqual(
            offenders, [],
            "a research module imported a decision path: " + str(offenders))

    def test_the_mirrored_replay_distances_match_the_replay_engine(self):
        # targets.py copies the replay engine's fixed distances rather
        # than importing it, because agent.replay imports the risk and
        # hypothesis packages and the structural guard above only reads
        # literal import lines - it would pass while the coupling was
        # real. So the copy is what gets tested for drift. Tests may
        # import anything; the production module may not.
        from agent.replay.engine import ReplayConfig
        from agent.prediction.targets import (REPLAY_STOP_DISTANCE_PCT,
                                              REPLAY_TARGET_DISTANCE_PCT)
        cfg = ReplayConfig()
        self.assertAlmostEqual(REPLAY_STOP_DISTANCE_PCT,
                               cfg.stop_distance_pct, places=6)
        self.assertAlmostEqual(REPLAY_TARGET_DISTANCE_PCT,
                               cfg.target_distance_pct, places=6)

    def test_the_default_stop_is_not_claimed_to_be_the_live_one(self):
        # The live stop is volatility-derived and ranges over 1% to 8%,
        # so the replay's flat 3% is one point in that range and not
        # the agent's rule. This test exists so the next person to read
        # STOP_DISTANCE_PCT cannot conclude the live agent risks 3%.
        from agent.hypothesis.engine import (MAX_STOP_DISTANCE_PCT,
                                             MIN_STOP_DISTANCE_PCT,
                                             suggest_stop_distance)
        from agent.prediction.targets import REPLAY_STOP_DISTANCE_PCT
        self.assertLess(MIN_STOP_DISTANCE_PCT, REPLAY_STOP_DISTANCE_PCT)
        self.assertGreater(MAX_STOP_DISTANCE_PCT, REPLAY_STOP_DISTANCE_PCT)
        # And the live function really does return something else for a
        # plausible volatility, so the gap is observable rather than
        # merely possible.
        quiet = suggest_stop_distance(0.5)
        loud = suggest_stop_distance(4.0)
        self.assertNotAlmostEqual(quiet, REPLAY_STOP_DISTANCE_PCT, places=6)
        self.assertNotAlmostEqual(loud, REPLAY_STOP_DISTANCE_PCT, places=6)

    def test_a_caller_can_supply_the_trades_own_stop(self):
        # Without this, every prediction about a live trade would be
        # scored against the simulator's stop.
        bars = [Bar(100, 100, 100, 100), Bar(100, 100, 100, 100),
                Bar(100, 100, 98.5, 99.0)]
        # 3% default: 97 not touched.
        self.assertEqual(stop_before_target(bars, 0).value, NEITHER)
        # A 1% stop, as a quiet symbol would get: 99 touched.
        tight = stop_before_target(bars, 0, stop_pct=1.0)
        self.assertEqual(tight.value, STOP_FIRST)


if __name__ == "__main__":
    unittest.main()
