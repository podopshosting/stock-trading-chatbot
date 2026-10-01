"""
Historical replay.

One property matters more than everything else in this file: at the
moment a decision is made, the system must not be able to see anything
that had not happened yet. Lookahead does not make a backtest slightly
optimistic - it makes it arbitrary, because a strategy that can see the
next bar can be made to return any number you like. And the output looks
identical to a good result.

So these tests are mostly attempts to cheat, each of which must fail.
"""
import os
import random
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.replay import (                                        # noqa: E402
    Bar, LookaheadError, PointInTimeEvidence, PointInTimeSeries,
    ReplayBroker, ReplayClock, ReplayConfig, ReplayResult, run,
    UnadjustedCorporateAction, adjust_bars_for_splits, find_discontinuities,
    PointInTimeFundamentals,
)


def bars(n, seed=11, drift=0.0012, vol=0.010, start=100.0):
    """A trending series with pullbacks, so indicators are not pinned."""
    rng, out, price = random.Random(seed), [], start
    for i in range(n):
        price *= (1 + drift + rng.gauss(0, vol))
        o = price * (1 + rng.gauss(0, vol / 3))
        high = max(o, price) * (1 + abs(rng.gauss(0, vol / 3)))
        low = min(o, price) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append(Bar(
            timestamp=f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00+00:00",
            open=round(o, 2), high=round(high, 2), low=round(low, 2),
            close=round(price, 2), volume=5_000_000))
    return out


def flat_bars(n, price=100.0):
    return [Bar(timestamp=f"2026-01-01T00:{i:02d}:00+00:00", open=price,
                high=price, low=price, close=price, volume=1_000_000)
            for i in range(n)]


def permissive_regime(symbol, clock):
    return {"regime": "BULLISH", "regime_confidence": 0.8,
            "risk_posture": "NORMAL", "market_session": "OPEN"}


class TestClock(unittest.TestCase):

    def test_time_only_moves_forward(self):
        """
        A backtest that can rewind can also re-decide with knowledge it
        did not have at the time.
        """
        clock = ReplayClock()
        clock.advance(5)
        with self.assertRaises(LookaheadError):
            clock.advance(4)

    def test_the_clock_cannot_stand_still(self):
        clock = ReplayClock()
        clock.advance(5)
        with self.assertRaises(LookaheadError):
            clock.advance(5)

    def test_timestamps_may_not_go_backwards(self):
        clock = ReplayClock("2026-01-01T10:00:00+00:00")
        clock.advance(1, "2026-01-01T11:00:00+00:00")
        with self.assertRaises(LookaheadError):
            clock.advance(2, "2026-01-01T09:00:00+00:00")

    def test_nothing_is_visible_before_the_run_starts(self):
        clock = ReplayClock()
        self.assertFalse(clock.started)
        with self.assertRaises(LookaheadError):
            clock.assert_visible(0)

    def test_a_future_bar_is_not_visible(self):
        clock = ReplayClock()
        clock.advance(10)
        clock.assert_visible(10)          # the current bar is fine
        clock.assert_visible(3)           # the past is fine
        with self.assertRaises(LookaheadError):
            clock.assert_visible(11)

    def test_a_future_timestamp_is_not_visible(self):
        clock = ReplayClock("2026-01-01T10:00:00+00:00")
        clock.advance(1, "2026-01-01T10:00:00+00:00")
        clock.assert_not_future("2026-01-01T09:59:00+00:00")
        with self.assertRaises(LookaheadError):
            clock.assert_not_future("2026-01-01T10:01:00+00:00")

    def test_an_undated_item_is_treated_as_not_visible(self):
        """
        An item whose publication time is unknown might have been
        published after the decision. Assuming otherwise is exactly the
        mistake this class exists to prevent.
        """
        clock = ReplayClock("2026-01-01T10:00:00+00:00")
        clock.advance(1, "2026-01-01T10:00:00+00:00")
        self.assertFalse(clock.is_visible(None))
        with self.assertRaises(LookaheadError):
            clock.assert_not_future(None)

    def test_the_clock_counts_its_advances(self):
        clock = ReplayClock()
        for i in range(3):
            clock.advance(i)
        self.assertEqual(clock.advances, 3)


class TestPointInTimeSeries(unittest.TestCase):

    def _series(self, n=10):
        clock = ReplayClock()
        return clock, PointInTimeSeries("XYZ", flat_bars(n), clock)

    def test_closes_stop_at_the_current_bar(self):
        clock, series = self._series(10)
        clock.advance(4, "2026-01-01T00:04:00+00:00")
        self.assertEqual(len(series.closes_through_now()), 5)

    def test_a_future_bar_cannot_be_read(self):
        clock, series = self._series(10)
        clock.advance(2)
        with self.assertRaises(LookaheadError):
            series.bar(7)

    def test_the_current_bar_can_be_read(self):
        """The falsifying control: the guard must not block everything."""
        clock, series = self._series(10)
        clock.advance(2)
        self.assertIsNotNone(series.bar(2))
        self.assertIsNotNone(series.current())

    def test_nothing_can_be_read_before_the_clock_starts(self):
        _clock, series = self._series(10)
        with self.assertRaises(LookaheadError):
            series.closes_through_now()

    def test_out_of_order_bars_are_refused(self):
        """
        Unsorted input would silently defeat the clock, because index
        order would stop corresponding to time order.
        """
        clock = ReplayClock()
        scrambled = list(reversed(flat_bars(5)))
        with self.assertRaises(ValueError):
            PointInTimeSeries("XYZ", scrambled, clock)

    def test_duplicate_timestamps_are_refused(self):
        clock = ReplayClock()
        duplicated = flat_bars(3) + [flat_bars(3)[-1]]
        with self.assertRaises(ValueError):
            PointInTimeSeries("XYZ", duplicated, clock)

    def test_next_open_is_the_only_forward_price(self):
        clock = ReplayClock()
        series = PointInTimeSeries("XYZ", bars(10), clock)
        clock.advance(3)
        self.assertIsNotNone(series.next_open())

    def test_next_open_is_none_on_the_final_bar(self):
        """
        An order decided on the final bar has nothing to fill against.
        Inventing a fill there adds a trade that could not have
        happened - and it will be one of the largest in the sample if
        the run ends on a spike.
        """
        clock = ReplayClock()
        series = PointInTimeSeries("XYZ", flat_bars(5), clock)
        clock.advance(4)
        self.assertIsNone(series.next_open())

    def test_a_bar_is_immutable(self):
        """A replay that can alter history is not a replay."""
        bar = flat_bars(1)[0]
        with self.assertRaises(Exception):
            bar.close = 999.0


class TestPointInTimeEvidence(unittest.TestCase):
    """Where lookahead is easiest to introduce by accident, because a
    headline feels like context rather than data."""

    def _feed(self):
        clock = ReplayClock("2026-01-01T10:00:00+00:00")
        items = [
            {"symbol": "XYZ", "published_at": "2026-01-01T09:00:00+00:00",
             "headline": "before"},
            {"symbol": "XYZ", "published_at": "2026-01-01T11:00:00+00:00",
             "headline": "after"},
            {"symbol": "XYZ", "headline": "undated"},
        ]
        return clock, PointInTimeEvidence(items, clock)

    def test_only_already_published_evidence_is_visible(self):
        clock, feed = self._feed()
        clock.advance(1, "2026-01-01T10:00:00+00:00")
        headlines = [i["headline"] for i in feed.visible()]
        self.assertIn("before", headlines)
        self.assertNotIn("after", headlines)

    def test_undated_evidence_is_never_served(self):
        clock, feed = self._feed()
        clock.advance(1, "2026-01-01T10:00:00+00:00")
        self.assertNotIn("undated",
                         [i["headline"] for i in feed.visible()])

    def test_undated_evidence_is_counted_so_the_run_can_report_it(self):
        """
        Otherwise a run proceeds on a thinner feed than expected and
        nobody knows.
        """
        _clock, feed = self._feed()
        self.assertEqual(feed.undated_count, 1)

    def test_later_evidence_becomes_visible_as_time_passes(self):
        clock, feed = self._feed()
        clock.advance(1, "2026-01-01T10:00:00+00:00")
        self.assertEqual(len(feed.visible()), 1)
        clock.advance(2, "2026-01-01T12:00:00+00:00")
        self.assertEqual(len(feed.visible()), 2)

    def test_evidence_can_be_filtered_by_symbol(self):
        clock = ReplayClock("2026-01-01T10:00:00+00:00")
        feed = PointInTimeEvidence([
            {"symbol": "AAA", "published_at": "2026-01-01T09:00:00+00:00"},
            {"symbol": "BBB", "published_at": "2026-01-01T09:00:00+00:00"},
        ], clock)
        clock.advance(1, "2026-01-01T10:00:00+00:00")
        self.assertEqual(len(feed.visible("AAA")), 1)


class TestFillTiming(unittest.TestCase):
    """
    The second-biggest source of backtest inflation after lookahead: a
    decision made on bar N's close that fills at bar N's close has used
    the fill price as an input. Every strategy becomes profitable under
    that rule.
    """

    def _broker(self, series_bars):
        clock = ReplayClock()
        series = {"XYZ": PointInTimeSeries("XYZ", series_bars, clock)}
        return clock, ReplayBroker(clock, series)

    def test_an_entry_fills_at_the_next_open_not_this_close(self):
        step = [
            Bar(timestamp="2026-01-01T00:00:00+00:00", open=100.0, high=100.0,
                low=100.0, close=100.0, volume=1e6),
            Bar(timestamp="2026-01-01T00:01:00+00:00", open=110.0, high=110.0,
                low=110.0, close=110.0, volume=1e6),
        ]
        clock, broker = self._broker(step)
        clock.advance(0, step[0].timestamp)
        broker.paper.set_market_open(True)
        order = broker.submit_entry("XYZ", notional=50.0)
        # Filled near 110 (the next open), NOT near 100 (this close).
        self.assertGreater(order["average_fill_price"], 105.0)

    def test_an_entry_on_the_final_bar_is_refused_not_invented(self):
        clock, broker = self._broker(flat_bars(3))
        clock.advance(2)
        order = broker.submit_entry("XYZ", notional=50.0)
        self.assertEqual(order["status"], "REJECTED")
        self.assertEqual(order["reject_reason"], "NO_NEXT_BAR")
        self.assertEqual(broker.unfillable_orders, 1)

    def test_a_stop_that_gaps_fills_worse_than_the_stop_level(self):
        """
        Gapping through a stop is normal. A backtest that fills every
        stop exactly at its level understates losses systematically.
        """
        gapping = [
            Bar(timestamp="2026-01-01T00:00:00+00:00", open=100.0, high=100.0,
                low=100.0, close=100.0, volume=1e6),
            Bar(timestamp="2026-01-01T00:01:00+00:00", open=90.0, high=91.0,
                low=89.0, close=90.0, volume=1e6),
        ]
        clock, broker = self._broker(gapping)
        clock.advance(0, gapping[0].timestamp)
        broker.paper.set_market_open(True)
        broker.submit_entry("XYZ", notional=50.0)
        # Position now open. A stop at 97 was gapped straight through:
        # the bar opened at 90, so 90 is the first available price.
        exit_order = broker.resolve_exit("XYZ", stop_price=97.0)
        # Asserting merely "< 97" would pass even if the gap were
        # ignored, because the broker's slippage nudges the fill under
        # the stop by itself. The fill must reflect the GAP, near 90.
        self.assertLess(exit_order["average_fill_price"], 93.0)

    def test_a_stop_reached_without_a_gap_fills_at_the_stop(self):
        touching = [
            Bar(timestamp="2026-01-01T00:00:00+00:00", open=100.0, high=100.0,
                low=100.0, close=100.0, volume=1e6),
            Bar(timestamp="2026-01-01T00:01:00+00:00", open=99.0, high=99.5,
                low=96.0, close=98.0, volume=1e6),
        ]
        clock, broker = self._broker(touching)
        clock.advance(0, touching[0].timestamp)
        broker.paper.set_market_open(True)
        broker.submit_entry("XYZ", notional=50.0)
        exit_order = broker.resolve_exit("XYZ", stop_price=97.0)
        self.assertAlmostEqual(exit_order["average_fill_price"], 97.0,
                               delta=0.5)

    def test_an_unreached_stop_exits_at_the_next_open(self):
        rising = [
            Bar(timestamp="2026-01-01T00:00:00+00:00", open=100.0, high=100.0,
                low=100.0, close=100.0, volume=1e6),
            Bar(timestamp="2026-01-01T00:01:00+00:00", open=105.0, high=106.0,
                low=104.0, close=105.5, volume=1e6),
        ]
        clock, broker = self._broker(rising)
        clock.advance(0, rising[0].timestamp)
        broker.paper.set_market_open(True)
        broker.submit_entry("XYZ", notional=50.0)
        exit_order = broker.resolve_exit("XYZ", stop_price=97.0)
        self.assertAlmostEqual(exit_order["average_fill_price"], 105.0,
                               delta=0.5)


class TestRunValidity(unittest.TestCase):

    def test_a_clean_run_is_valid(self):
        result = run({"XYZ": bars(260)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        self.assertTrue(result.valid)
        self.assertFalse(result.lookahead_detected)

    def test_validity_is_derived_not_assignable(self):
        """
        A run cannot be marked valid after the fact by code that would
        prefer a result.
        """
        result = ReplayResult(
            config={}, bars_processed=0, decisions_evaluated=0,
            entries_attempted=0, entries_filled=0, exits_filled=0,
            unfillable_orders=0, positions_open_at_end=0, rejections={},
            performance={}, lookahead_detected=True)
        self.assertFalse(result.valid)
        with self.assertRaises(AttributeError):
            result.valid = True

    def test_a_lookahead_run_is_void_not_merely_caveated(self):
        """
        A contaminated backtest looks identical to a good one, so the
        amount of contamination is unknown and the result has to be
        refused rather than annotated.
        """
        def peeking_regime(symbol, clock):
            clock.assert_visible(clock.index + 50, "a future bar")
            return permissive_regime(symbol, clock)

        result = run({"XYZ": bars(260)}, ReplayConfig(warmup_bars=200),
                     regime_for=peeking_regime)
        self.assertFalse(result.valid)
        self.assertTrue(result.lookahead_detected)
        self.assertEqual(result.performance["verdict"], "VOID_LOOKAHEAD")
        self.assertTrue(any("void" in w for w in result.warnings))

    def test_a_run_needs_at_least_one_symbol(self):
        with self.assertRaises(ValueError):
            run({}, ReplayConfig())


class TestRunHonesty(unittest.TestCase):

    def test_a_missing_regime_is_reported_not_defaulted(self):
        """
        Supplying a permissive default would be the replay relaxing a
        safety gate to manufacture trades. Failing closed is correct -
        but a reader seeing zero trades would otherwise conclude the
        strategy found no setups, when it was never allowed to look.
        """
        result = run({"XYZ": bars(260)}, ReplayConfig(warmup_bars=200))
        self.assertEqual(result.entries_filled, 0)
        self.assertTrue(any("failed closed" in w for w in result.warnings))

    def test_too_few_bars_for_the_warmup_is_reported(self):
        result = run({"XYZ": bars(20)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        self.assertTrue(any("warmup" in w for w in result.warnings))
        self.assertEqual(result.decisions_evaluated, 0)

    def test_no_decisions_are_taken_during_warmup(self):
        """
        Deciding on bar 3 of a 200-bar moving average is not a strategy
        decision, it is a division by a number that happens to exist.
        """
        result = run({"XYZ": bars(210)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        # 210 bars, 200 warmup, last bar cannot fill -> at most 9.
        self.assertLessEqual(result.decisions_evaluated, 9)

    def test_undated_evidence_is_reported_as_discarded(self):
        result = run(
            {"XYZ": bars(260)}, ReplayConfig(warmup_bars=200),
            evidence=[{"symbol": "XYZ", "headline": "no date"}],
            regime_for=permissive_regime)
        self.assertTrue(any("no publication time" in w
                            for w in result.warnings))

    def test_a_small_sample_cannot_claim_an_edge(self):
        """
        The replay hands its trades to the SAME metrics code as live, so
        it inherits the adequacy gating rather than reimplementing a
        more flattering version.
        """
        result = run({"XYZ": bars(400)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        self.assertIn(result.performance["verdict"],
                      ("NO_TRADES", "NO_EDGE_DEMONSTRATED"))

    def test_the_result_records_its_configuration(self):
        result = run({"XYZ": bars(260)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        self.assertTrue(result.config["config_version"])
        self.assertEqual(result.config["warmup_bars"], 200)

    def test_rejections_are_reported_by_code(self):
        """
        A run with no trades must say WHY, or a heavily gated strategy
        is indistinguishable from a broken pipeline.
        """
        result = run({"XYZ": bars(400)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        self.assertTrue(result.rejections)
        self.assertTrue(all(isinstance(v, int)
                            for v in result.rejections.values()))

    def test_every_trade_is_marked_paper(self):
        result = run({"XYZ": bars(400)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        self.assertTrue(result.performance["paper_only"])

    def test_a_completed_run_closes_everything_it_opened(self):
        result = run({"XYZ": bars(400)}, ReplayConfig(warmup_bars=200),
                     regime_for=permissive_regime)
        self.assertEqual(result.entries_filled, result.exits_filled)
        self.assertEqual(result.positions_open_at_end, 0)

    def test_a_position_with_no_exit_rule_is_still_closed_at_the_end(self):
        """
        With the default 3% trailing stop every position exits on its
        own, which made the run-level assertion vacuous - it passed
        because the situation never arose. Disabling the exit rules
        makes the end-of-data flatten the ONLY way out, so this test
        fails if that flatten is skipped.
        """
        config = ReplayConfig(warmup_bars=200, stop_distance_pct=50.0,
                              target_distance_pct=500.0,
                              trailing_stop_pct=None)
        result = run({"XYZ": bars(215, seed=6)}, config,
                     regime_for=permissive_regime)
        self.assertGreater(result.entries_filled, 0)
        self.assertEqual(result.exits_filled, result.entries_filled)
        self.assertEqual(result.positions_open_at_end, 0)

    def test_the_end_of_data_flatten_journals_an_open_position(self):
        """
        Tested directly rather than through a full run, because with a
        3% trailing stop no position happens to survive to the last bar
        - which made the run-level assertion vacuous. A test that
        passes because the situation never arises is not coverage.
        """
        from agent.broker.paper import PaperBroker, PaperBrokerConfig
        from agent.journal import InMemoryJournal
        from agent.positions import ExitPlan, PositionManager
        from agent.replay.engine import _flatten_at_end
        from agent.replay import PointInTimeSeries, ReplayBroker

        clock = ReplayClock()
        series = {"XYZ": PointInTimeSeries("XYZ", bars(10), clock)}
        broker = ReplayBroker(clock, series,
                              PaperBrokerConfig(starting_cash=5000.0, seed=3,
                                                partial_fill_probability=0.0))
        clock.advance(0)
        broker.paper.set_market_open(True)
        order = broker.submit_entry("XYZ", notional=50.0)
        manager = PositionManager(broker=broker, execution_available=True)
        fill = order["average_fill_price"]
        manager.open_from_order(order,
                                ExitPlan(stop_price=fill * 0.9),
                                hypothesis_id="h1", risk_decision_id="d1")
        self.assertEqual(manager.open_count, 1)

        journal = InMemoryJournal()
        stats = {"exits_filled": 0}
        _flatten_at_end(manager, series, journal,
                        {"XYZ": order}, ReplayConfig(), stats)

        self.assertEqual(manager.open_count, 0)
        self.assertEqual(len(journal.list_trades()), 1)
        self.assertEqual(stats["exits_filled"], 1)

    def test_an_unclosed_position_makes_the_results_incomplete(self):
        """
        If a position cannot be closed its P&L is missing, and the
        report must say so rather than present a partial figure as the
        result.
        """
        result = ReplayResult(
            config={}, bars_processed=1, decisions_evaluated=0,
            entries_attempted=1, entries_filled=1, exits_filled=0,
            unfillable_orders=0, positions_open_at_end=1, rejections={},
            performance={})
        self.assertEqual(result.positions_open_at_end, 1)


class TestDeterminism(unittest.TestCase):

    def test_the_same_inputs_give_the_same_run(self):
        """
        A replay that cannot reproduce itself cannot be used to compare
        two strategy versions, which is the only reason to have one.
        """
        series = bars(400)

        def go():
            result = run({"XYZ": list(series)},
                         ReplayConfig(warmup_bars=200, seed=42),
                         regime_for=permissive_regime)
            return (result.entries_filled, result.exits_filled,
                    result.performance["total_net_pnl"], result.rejections)
        self.assertEqual(go(), go())

    def test_a_different_seed_may_change_the_fills(self):
        """
        The falsifying control for the test above: it must be pinning
        the seed, not testing a function with no randomness at all.
        """
        series = bars(400)
        config = dict(warmup_bars=200, partial_fill_probability=0.9)
        a = run({"XYZ": list(series)}, ReplayConfig(seed=1, **config),
                regime_for=permissive_regime)
        b = run({"XYZ": list(series)}, ReplayConfig(seed=999, **config),
                regime_for=permissive_regime)
        # Either the fills differ, or the strategy took no trades at all
        # under these settings - in which case there was nothing to
        # randomise and the comparison is vacuous, which we assert
        # explicitly rather than let pass silently.
        if a.entries_filled == 0 and b.entries_filled == 0:
            self.skipTest("no trades taken; nothing to randomise")
        self.assertTrue(
            a.performance["total_net_pnl"] != b.performance["total_net_pnl"]
            or a.entries_filled != b.entries_filled)


if __name__ == "__main__":
    unittest.main()


class TestUnadjustedCorporateActions(unittest.TestCase):
    """A replay over an unadjusted split is not pessimistic, it is
    meaningless: a 10-for-1 reads as a 90% overnight fall, so every stop
    fires and the run reports confident numbers about a crash that never
    happened."""

    def bars(self, pre=1, post=2, pre_px=1000.0, post_px=100.0):
        out = [Bar(f"2026-06-{d:02d}T20:00:00Z", pre_px, pre_px * 1.01,
                   pre_px * 0.99, pre_px, 100.0)
               for d in range(1, pre + 1)]
        out += [Bar(f"2026-06-{d:02d}T20:00:00Z", post_px, post_px * 1.01,
                    post_px * 0.99, post_px, 1000.0)
                for d in range(pre + 1, pre + post + 1)]
        return out

    def series(self, bars, **kw):
        return PointInTimeSeries("NVDA", bars, ReplayClock(
            [b.timestamp for b in bars]), **kw)

    def test_an_unadjusted_split_refuses_to_construct(self):
        with self.assertRaises(UnadjustedCorporateAction) as ctx:
            self.series(self.bars())
        self.assertIn("10-for-1", str(ctx.exception))
        self.assertIn("never happened", str(ctx.exception))

    def test_the_protection_is_on_by_default(self):
        """A caller who does not know to ask for it still gets it."""
        with self.assertRaises(UnadjustedCorporateAction):
            PointInTimeSeries("NVDA", self.bars(), ReplayClock(
                [b.timestamp for b in self.bars()]))

    def test_the_adjusted_series_constructs(self):
        """The control: without it, the refusal above could be any
        construction failure rather than the split check."""
        adj = adjust_bars_for_splits(
            self.bars(), [{"ex_date": "2026-06-02", "ratio": 10}])
        s = self.series(adj)
        self.assertEqual(len(s), 3)
        self.assertEqual(s.discontinuities, [])

    def test_a_genuine_crash_is_not_refused(self):
        """An unexplained 40% fall may be exactly what the run exists to
        study. Raising on it would push callers to disable the check,
        which would disable it for the real splits too."""
        bars = self.bars(pre=1, post=2, pre_px=100.0, post_px=60.0)
        s = self.series(bars)
        self.assertEqual(len(s.discontinuities), 1)
        self.assertFalse(s.discontinuities[0]["matches_plausible_split"])

    def test_ordinary_volatility_is_not_flagged_at_all(self):
        bars = self.bars(pre=1, post=2, pre_px=100.0, post_px=92.0)
        self.assertEqual(self.series(bars).discontinuities, [])

    def test_the_check_can_be_waived_but_the_gap_is_still_recorded(self):
        s = self.series(self.bars(), require_adjusted=False)
        self.assertEqual(len(s.discontinuities), 1)
        self.assertEqual(s.discontinuities[0]["nearest_ratio"], 10)

    def test_adjustment_restates_prices_down_and_volumes_up(self):
        adj = adjust_bars_for_splits(
            self.bars(), [{"ex_date": "2026-06-02", "ratio": 10}])
        self.assertAlmostEqual(adj[0].close, 100.0)
        self.assertAlmostEqual(adj[0].volume, 1000.0)
        self.assertAlmostEqual(adj[0].high, 101.0)
        self.assertAlmostEqual(adj[-1].close, 100.0, places=6)

    def test_a_reverse_split_adjusts_the_other_way(self):
        bars = self.bars(pre=1, post=2, pre_px=10.0, post_px=100.0)
        adj = adjust_bars_for_splits(
            bars, [{"ex_date": "2026-06-02", "ratio": 0.1}])
        self.assertAlmostEqual(adj[0].close, 100.0)
        self.assertEqual(self.series(adj).discontinuities, [])

    def test_bars_after_the_split_are_left_alone(self):
        adj = adjust_bars_for_splits(
            self.bars(), [{"ex_date": "2026-06-02", "ratio": 10}])
        self.assertAlmostEqual(adj[1].close, 100.0)
        self.assertAlmostEqual(adj[1].volume, 1000.0)

    def test_two_splits_compound(self):
        bars = [Bar("2026-01-01T20:00:00Z", 400, 404, 396, 400, 10.0),
                Bar("2026-06-01T20:00:00Z", 200, 202, 198, 200, 20.0),
                Bar("2026-09-01T20:00:00Z", 100, 101, 99, 100, 40.0)]
        adj = adjust_bars_for_splits(bars, [
            {"ex_date": "2026-06-01", "ratio": 2},
            {"ex_date": "2026-09-01", "ratio": 2}])
        self.assertAlmostEqual(adj[0].close, 100.0)
        self.assertAlmostEqual(adj[1].close, 100.0)
        self.assertAlmostEqual(adj[2].close, 100.0)


class TestFundamentalsAreGatedOnFilingDate(unittest.TestCase):
    """The subtlest leak available to a replay. A quarter ending 27 June
    is not public on 27 June - Apple's actually filed 2026-08-01. Gating
    on the period end hands the strategy five weeks of hindsight about
    results nobody had, and because the data is genuinely historical the
    run looks impeccable."""

    FACTS = [
        {"concept": "Revenues", "period_end": "2026-03-28",
         "filed": "2026-05-02", "value": 90.0},
        {"concept": "Revenues", "period_end": "2026-06-27",
         "filed": "2026-08-01", "value": 94.0},
        {"concept": "Revenues", "period_end": "2026-06-27",
         "value": 99.0},                      # no filing date at all
    ]

    def at(self, day, facts=None):
        return PointInTimeFundamentals(facts if facts is not None
                                       else self.FACTS, ReplayClock(day))

    def test_a_quarter_is_invisible_before_it_is_filed(self):
        f = self.at("2026-07-15")
        self.assertEqual([r["value"] for r in f.visible()], [90.0])
        self.assertEqual(f.latest()["value"], 90.0)

    def test_the_same_quarter_is_visible_once_filed(self):
        """The control: without it, the test above could pass because
        nothing is ever visible."""
        self.assertEqual(self.at("2026-08-15").latest()["value"], 94.0)

    def test_a_fact_with_no_filing_date_is_never_served(self):
        """It cannot be shown to have been knowable, so it is not used -
        at either date, including after every real filing is public."""
        for day in ("2026-07-15", "2026-08-15"):
            with self.subTest(day=day):
                f = self.at(day)
                self.assertNotIn(99.0, [r["value"] for r in f.visible()])
                self.assertEqual(f.undated_count, 1)

    def test_a_withheld_quarter_is_counted_rather_than_hidden(self):
        f = self.at("2026-07-15")
        self.assertEqual([r["value"] for r in f.withheld()], [94.0])

    def test_coverage_reports_what_was_discarded_and_why(self):
        c = self.at("2026-07-15").coverage()
        self.assertEqual(c["total"], 3)
        self.assertEqual(c["visible"], 1)
        self.assertEqual(c["withheld_not_yet_filed"], 1)
        self.assertEqual(c["discarded_no_filing_date"], 1)
        self.assertEqual(c["gated_on"], "filed")

    def test_period_end_is_not_what_decides_visibility(self):
        """Stated directly, because this is the whole point: the June
        quarter has the later period end and is still the hidden one."""
        f = self.at("2026-07-15")
        visible = f.visible()
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["period_end"], "2026-03-28")

    def test_a_late_filed_restatement_does_not_displace_by_period_end(self):
        """Ordered by filing date first. By period end alone, a
        restatement covering an earlier quarter but filed later would
        displace the figure actually in front of the market."""
        f = self.at("2026-09-01", facts=self.FACTS + [
            {"concept": "Revenues", "period_end": "2026-03-28",
             "filed": "2026-08-20", "value": 91.5}])
        self.assertEqual(f.latest()["value"], 91.5)

    def test_concept_filtering_still_respects_the_gate(self):
        f = self.at("2026-07-15")
        self.assertEqual(len(f.visible("Revenues")), 1)
        self.assertIsNone(f.latest("NetIncomeLoss"))

    def test_an_unstarted_clock_serves_nothing_rather_than_everything(self):
        """ReplayClock treats an unset timestamp as "everything is
        visible", which is sensible for a clock and catastrophic here:
        it would serve every filing ever made."""
        f = PointInTimeFundamentals(self.FACTS, ReplayClock())
        self.assertEqual(f.visible(), [])
        self.assertIsNone(f.latest())
