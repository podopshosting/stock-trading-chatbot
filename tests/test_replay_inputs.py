"""
Scenario variables must reach the modules that act on them.

Six values in the replay engine were constants dressed as inputs, and
each one silently disabled a risk control:

    spread_pct          literal  -> SPREAD_TOO_WIDE unreachable
    quote_age_seconds   0.0      -> STALE_MARKET_DATA unreachable
    minutes_to_close    120.0    -> TOO_LATE_IN_SESSION unreachable
    positions_opened    0        -> MAX_NEW_POSITIONS_REACHED unreachable
    realized_pnl_today  0.0      -> DAILY_RISK_LOCK unreachable
    dollar_volume       floored  -> INSUFFICIENT_LIQUIDITY unreachable

Every test here observes a DOWNSTREAM REFUSAL rather than asserting a
value was stored. An attribute that is set and never read is the shape
the earlier spread fix took, and a mutation reverting its use survived.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.replay import ReplayConfig, run                       # noqa: E402
from agent.replay.engine import _bar_gap_seconds                 # noqa: E402
from agent.replay.scenarios import WARMUP, _calm, regime_for     # noqa: E402
from agent.replay.scenarios import build                         # noqa: E402
from agent.risk import RiskLimits                                # noqa: E402


def go(bars=None, spread_pct=None, **config_kw):
    """spread_pct is a run() argument, not a config field - it is the
    market's state, not the strategy's setting."""
    scenario = build("grind_up")
    config = ReplayConfig(warmup_bars=WARMUP, risk_limits=RiskLimits(),
                          starting_cash=100.0, **config_kw)
    extra = {} if spread_pct is None else {"spread_pct": spread_pct}
    return run(bars or scenario.bars, config,
               regime_for=regime_for(scenario), **extra)


class TestTheRefusalIsReachable(unittest.TestCase):
    """Each control must be observable as a refusal, not merely wired."""

    def test_the_baseline_refuses_on_none_of_them(self):
        """The control. If the baseline already tripped these, the tests
        below would pass without the scenario variable doing anything."""
        result = go()
        for code in ("SPREAD_TOO_WIDE", "STALE_MARKET_DATA",
                     "TOO_LATE_IN_SESSION", "INSUFFICIENT_LIQUIDITY",
                     "DAILY_RISK_LOCK"):
            with self.subTest(code=code):
                self.assertEqual(result.rejections.get(code, 0), 0)
        self.assertGreater(result.entries_filled, 0,
                           "the baseline must trade, or none of this is "
                           "measuring a refusal")

    def test_a_wide_spread_refuses(self):
        self.assertGreater(
            go(spread_pct=5.0).rejections.get("SPREAD_TOO_WIDE", 0), 0)

    def test_a_near_close_session_refuses(self):
        self.assertGreater(
            go(minutes_to_close=1.0).rejections.get(
                "TOO_LATE_IN_SESSION", 0), 0)

    def test_thin_volume_refuses(self):
        from agent.replay.data import Bar
        scenario = build("grind_up")
        thin = {s: [Bar(timestamp=b.timestamp, open=b.open, high=b.high,
                        low=b.low, close=b.close, volume=100.0)
                    for b in bars]
                for s, bars in scenario.bars.items()}
        self.assertGreater(
            go(bars=thin).rejections.get("INSUFFICIENT_LIQUIDITY", 0), 0)

    def test_a_timeline_hole_refuses_as_stale(self):
        result = go(bars=build("trading_halt").bars)
        self.assertGreater(result.rejections.get("STALE_MARKET_DATA", 0), 0)


class TestStalenessIsTheExcessGapNotTheRawGap(unittest.TestCase):
    """At a bar's close its own price is current.

    Returning the raw gap made every DAILY replay refuse everything -
    86,400 seconds against a 120-second limit - which turned a year of
    real AAPL bars into zero trades and read as strategy selectivity.
    """

    def test_a_minute_bar_arriving_on_time_is_fresh(self):
        self.assertEqual(
            _bar_gap_seconds("2026-01-02T14:00:00+00:00",
                             "2026-01-02T14:01:00+00:00", 60.0), 0.0)

    def test_a_daily_bar_arriving_on_time_is_fresh(self):
        self.assertEqual(
            _bar_gap_seconds("2026-01-02T14:00:00+00:00",
                             "2026-01-03T14:00:00+00:00", 86400.0), 0.0)

    def test_a_hole_is_the_missing_time_only(self):
        """A 45-minute hole in a one-minute series is 44 minutes of
        absence, not 45."""
        self.assertEqual(
            _bar_gap_seconds("2026-01-02T14:00:00+00:00",
                             "2026-01-02T14:45:00+00:00", 60.0),
            45 * 60 - 60)

    def test_an_unparseable_timestamp_falls_back_to_the_interval(self):
        """An unknown gap must not read as a zero one."""
        for previous in (None, "", "not-a-time"):
            with self.subTest(previous=previous):
                self.assertEqual(
                    _bar_gap_seconds(previous, "2026-01-02T14:00:00+00:00",
                                     60.0), 60.0)

    def test_a_backwards_gap_falls_back_rather_than_going_negative(self):
        self.assertEqual(
            _bar_gap_seconds("2026-01-02T14:05:00+00:00",
                             "2026-01-02T14:00:00+00:00", 60.0), 60.0)


class TestDailyBudgetsResetPerCalendarDay(unittest.TestCase):
    """The counters used to accumulate across the whole run while the
    risk context derived session_date from each bar - so the governor
    saw the day change and the budget never did."""

    def _multi_day(self, days=4, per_day=40):
        bars = []
        price = 100.0
        for d in range(days):
            day = f"2026-01-{2 + d:02d}"
            series = _calm(per_day, start=price, day=day)
            bars.extend(series)
            price = series[-1].close
        warm = _calm(WARMUP, day="2026-01-01")
        return {"XYZ": warm + bars}

    def test_more_than_one_session_produces_more_than_one_day_of_trades(self):
        """With a single accumulating budget a multi-day run gets the
        same two trades as a single session."""
        single = go()
        multi = run({"XYZ": self._multi_day()["XYZ"]},
                    ReplayConfig(warmup_bars=WARMUP,
                                 risk_limits=RiskLimits(),
                                 starting_cash=100.0),
                    regime_for=regime_for(build("grind_up")))
        self.assertGreater(
            multi.entries_filled, single.entries_filled,
            "four sessions produced no more trades than one, so the "
            "daily budget is not resetting")

    def test_a_single_session_is_still_bounded_by_one_day_of_capital(self):
        """The control for the test above: the reset must not become an
        unlimited budget."""
        result = go()
        self.assertLessEqual(result.entries_filled, 3)
        self.assertGreater(result.rejections.get("INSUFFICIENT_CAPITAL", 0), 0)


class TestTheConfigIsRecordedWithTheResult(unittest.TestCase):

    def test_the_new_knobs_appear_in_the_recorded_config(self):
        """A run that cannot say what spread or session it assumed is not
        reproducible."""
        config = go(spread_pct=1.5, minutes_to_close=30.0,
                    bar_interval_seconds=300.0).as_dict()["config"]
        self.assertEqual(config["minutes_to_close"], 30.0)
        self.assertEqual(config["bar_interval_seconds"], 300.0)


class TestPositionsOpenedTodayReachesTheGovernor(unittest.TestCase):
    """The per-day new-position cap is wired but STRUCTURALLY
    unreachable through a full replay, and that is worth recording
    rather than hiding.

    With daily_capital_limit $50 and max_position_pct_of_daily 0.60, a
    position is at most $30, so roughly two entries exhaust the day's
    capital. INSUFFICIENT_CAPITAL therefore binds before the
    three-per-day cap ever can: the cap is dominated by the ceiling
    above it.

    So this exercises the handoff directly. A mutation setting
    positions_opened_today back to a literal 0 survived a full-replay
    suite precisely because no replay can reach the cap.
    """

    def _decide_with(self, opened):
        from agent.replay.clock import ReplayClock
        from agent.replay.data import PointInTimeEvidence, PointInTimeSeries
        from agent.replay.engine import _decide
        from agent.positions import PositionManager
        scenario = build("grind_up")
        clock = ReplayClock()
        bars = scenario.bars["XYZ"]
        series = PointInTimeSeries("XYZ", bars, clock)
        clock.advance(WARMUP + 5, bars[WARMUP + 5].timestamp)
        config = ReplayConfig(warmup_bars=WARMUP, risk_limits=RiskLimits(),
                              starting_cash=100.0)
        decision, _hypothesis = _decide(
            "XYZ", series, clock, config,
            PointInTimeEvidence([], clock), None,
            regime_for(scenario),
            PositionManager(broker=None, execution_available=False),
            0.0, 0.05, 0.0, opened, 0.0)
        return decision

    def test_the_cap_refuses_once_the_count_is_reached(self):
        decision = self._decide_with(RiskLimits().max_new_positions_per_day)
        self.assertIsNotNone(decision)
        self.assertIn("MAX_NEW_POSITIONS_REACHED",
                      [str(c) for c in decision.reason_codes])

    def test_a_zero_count_does_not_trip_the_cap(self):
        """The control: the refusal above must come from the count, not
        from something that refuses every decision in this fixture."""
        decision = self._decide_with(0)
        self.assertIsNotNone(decision)
        self.assertNotIn("MAX_NEW_POSITIONS_REACHED",
                         [str(c) for c in decision.reason_codes])

    def test_a_full_replay_cannot_reach_the_cap(self):
        """Recorded as a FINDING, not a defect.

        If this ever starts failing, the capital ceiling has been
        raised and the cap has become live - which is worth noticing
        deliberately rather than discovering in a backtest.
        """
        result = go()
        self.assertEqual(
            result.rejections.get("MAX_NEW_POSITIONS_REACHED", 0), 0,
            "the per-day cap is now reachable in replay; the capital "
            "ceiling used to bind first")
        self.assertLessEqual(result.entries_filled,
                             RiskLimits().max_new_positions_per_day)
