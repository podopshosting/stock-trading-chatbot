"""
Named market conditions.

The first version of these scenarios produced 119 decisions and ZERO
entries, because the bar generator used a fixed alternating wobble that
the indicators read as directionless. Every scenario looked safe, and
none of them had tested anything: a harness in which nothing trades
cannot distinguish "the risk controls held" from "the agent never
acted".

So the first test here is the control, and several others exist only to
stop a scenario quietly becoming decorative.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.replay import ReplayConfig, run, scenarios          # noqa: E402
from agent.risk import RiskLimits                              # noqa: E402


def execute(name, **kw):
    scenario = scenarios.build(name)
    config = ReplayConfig(warmup_bars=scenarios.WARMUP,
                          risk_limits=RiskLimits(), starting_cash=100.0)
    for key, value in (scenario.config_overrides or {}).items():
        assert hasattr(config, key), f"unknown config {key}"
        setattr(config, key, value)
    if scenario.spread_pct is not None:
        kw.setdefault("spread_pct", scenario.spread_pct)
    return scenario, run(scenario.bars, config,
                         regime_for=scenarios.regime_for(scenario), **kw)


class TestTheHarnessActuallyTrades(unittest.TestCase):
    """Without this, every other scenario passes for the wrong reason."""

    def test_the_control_scenario_takes_a_position(self):
        _scenario, result = execute("grind_up")
        self.assertTrue(result.valid)
        self.assertGreater(
            result.entries_filled, 0,
            "the control produced no entry, so no scenario in this file "
            "is testing the risk controls - they are testing silence")

    def test_the_control_also_closes_what_it_opens(self):
        _scenario, result = execute("grind_up")
        self.assertGreater(result.exits_filled, 0)
        self.assertEqual(result.positions_open_at_end, 0)


class TestEveryScenarioIsWellFormed(unittest.TestCase):

    def test_every_scenario_builds(self):
        for name in scenarios.ALL:
            with self.subTest(name=name):
                scenario = scenarios.build(name)
                self.assertTrue(scenario.bars)
                self.assertTrue(scenario.question)
                self.assertTrue(scenario.expectation)

    def test_bars_are_strictly_increasing_in_time(self):
        """A fixture whose clock rewinds is refused by the engine, and
        it took a scenario that printed nothing at all to notice."""
        for name in scenarios.ALL:
            scenario = scenarios.build(name)
            for symbol, bars in scenario.bars.items():
                with self.subTest(name=name, symbol=symbol):
                    stamps = [b.timestamp for b in bars]
                    self.assertEqual(stamps, sorted(stamps))
                    self.assertEqual(len(stamps), len(set(stamps)))

    def test_every_bar_brackets_its_own_open_and_close(self):
        """A high below the open is not a market condition, it is a bad
        fixture, and it would make every downstream check meaningless."""
        for name in scenarios.ALL:
            scenario = scenarios.build(name)
            for symbol, bars in scenario.bars.items():
                for i, bar in enumerate(bars):
                    with self.subTest(name=name, symbol=symbol, bar=i):
                        self.assertGreaterEqual(bar.high,
                                                max(bar.open, bar.close))
                        self.assertLessEqual(bar.low,
                                             min(bar.open, bar.close))
                        self.assertGreater(bar.low, 0)

    def test_every_scenario_has_enough_bars_for_the_warmup(self):
        for name in scenarios.ALL:
            scenario = scenarios.build(name)
            for symbol, bars in scenario.bars.items():
                with self.subTest(name=name, symbol=symbol):
                    self.assertGreater(len(bars), scenarios.WARMUP)

    def test_scenarios_are_deterministic(self):
        for name in scenarios.ALL:
            with self.subTest(name=name):
                first = [b.as_dict() for b in
                         next(iter(scenarios.build(name).bars.values()))]
                second = [b.as_dict() for b in
                          next(iter(scenarios.build(name).bars.values()))]
                self.assertEqual(first, second)

    def test_an_unknown_scenario_is_refused(self):
        with self.assertRaises(KeyError):
            scenarios.build("not_a_scenario")

    def test_every_scenario_carries_the_disclaimer(self):
        for name in scenarios.ALL:
            with self.subTest(name=name):
                self.assertIn("SCENARIO_NOT_EVIDENCE",
                              scenarios.build(name).as_dict()["disclaimer"])

    def test_every_scenario_runs_without_lookahead(self):
        for name in scenarios.ALL:
            with self.subTest(name=name):
                _scenario, result = execute(name)
                self.assertTrue(result.valid, result.lookahead_detail)


class TestTheScenariosMeasureWhatTheyClaim(unittest.TestCase):
    """Each of these would pass trivially if the scenario did nothing,
    which is why the control above is a separate test."""

    def _stops(self, result):
        return (result.as_dict().get("performance") or {}).get(
            "stop_integrity") or {}

    def test_a_gap_through_the_stop_breaches_the_planned_risk(self):
        """A stop is an instruction to the market, not a promise from
        it. The number is the point."""
        _scenario, result = execute("gap_through_stop")
        stops = self._stops(result)
        self.assertGreater(stops.get("trades_assessed", 0), 0)
        self.assertGreater(stops.get("stop_breaches", 0), 0)
        self.assertLess(stops.get("worst_r", 0), -1.0,
                        "a 12% gap through a 3% stop must lose more than "
                        "the planned risk; if it does not, the harness "
                        "is not producing the gap")

    def test_a_smooth_decline_does_not_breach_it(self):
        """The counterpart. If BOTH scenarios breached, the finding
        would be about the fixture rather than about gaps."""
        _scenario, result = execute("slow_bleed")
        stops = self._stops(result)
        self.assertGreater(stops.get("trades_assessed", 0), 0)
        self.assertEqual(stops.get("stop_breaches"), 0)
        self.assertGreaterEqual(stops.get("worst_r", -99), -1.0)

    def test_a_wide_spread_refuses_every_entry(self):
        _scenario, result = execute("spread_blowout")
        self.assertEqual(result.entries_filled, 0)
        self.assertGreater(result.rejections.get("SPREAD_TOO_WIDE", 0), 0)

    def test_a_normal_spread_does_not_refuse_on_spread(self):
        """The control for the one above: proves SPREAD_TOO_WIDE comes
        from the scenario's spread and not from the gate always firing."""
        _scenario, result = execute("grind_up")
        self.assertEqual(result.rejections.get("SPREAD_TOO_WIDE", 0), 0)

    def test_the_spread_reaches_the_risk_context_at_all(self):
        """This is the specific defect: spread_pct was a literal in the
        engine, so a scenario could widen the broker's spread and the
        risk gate would still see five basis points."""
        scenario = scenarios.build("grind_up")
        config = ReplayConfig(warmup_bars=scenarios.WARMUP,
                              risk_limits=RiskLimits(), starting_cash=100.0)
        wide = run(scenario.bars, config,
                   regime_for=scenarios.regime_for(scenario),
                   spread_pct=5.0)
        self.assertGreater(wide.rejections.get("SPREAD_TOO_WIDE", 0), 0,
                           "widening the spread changed nothing, so the "
                           "spread gate is untestable")

    def test_a_halt_is_a_hole_in_the_timeline_not_flat_bars(self):
        """A halt is an ABSENCE of prints. Flat bars are data saying
        'unchanged', which is the opposite claim."""
        scenario = scenarios.build("trading_halt")
        bars = scenario.bars["XYZ"]
        gaps = [i for i in range(1, len(bars))
                if bars[i].timestamp != bars[i - 1].timestamp]
        self.assertTrue(gaps)
        minutes = {b.timestamp for b in bars}
        self.assertLess(len(minutes), scenarios.WARMUP + 100)

    def test_a_hostile_regime_suppresses_entries(self):
        _scenario, result = execute("bear_regime")
        self.assertEqual(result.entries_filled, 0)


class TestTheSpreadIsPaidAsWellAsChecked(unittest.TestCase):
    """The spread has two jobs and they are separate.

    The risk gate uses it to REFUSE an entry. The broker uses it to
    PRICE a fill. Wiring only the first means a scenario can declare a
    one-dollar spread, have the gate refuse on it, and - if the gate
    were ever relaxed - fill as though the market were tight. A
    mutation reverting the broker's spread to its old literal survived
    until this test existed.
    """

    def _broker(self, spread_pct):
        from agent.broker.paper import PaperBrokerConfig
        from agent.replay.broker import ReplayBroker
        from agent.replay.clock import ReplayClock
        from agent.replay.data import PointInTimeSeries
        scenario = scenarios.build("grind_up")
        clock = ReplayClock()
        series = {s: PointInTimeSeries(s, b, clock)
                  for s, b in scenario.bars.items()}
        return ReplayBroker(clock, series,
                            PaperBrokerConfig(starting_cash=100.0,
                                              slippage_bps=0.0, seed=1),
                            spread_pct=spread_pct)

    def _quoted_spread(self, spread_pct):
        """The spread the broker actually PUBLISHES, not the attribute
        it stores.

        The first version of this test asserted `spread_fraction` and a
        mutation reverting the quote line to its old literal survived:
        the attribute was set and never used. Observing the published
        quote is the only version of this test that can fail.
        """
        broker = self._broker(spread_pct)
        broker.clock.advance(0, broker.series["XYZ"].bars[0].timestamp
                             if hasattr(broker.series["XYZ"], "bars")
                             else None)
        broker.sync_quotes()
        quote = broker.paper._quotes["XYZ"]
        return quote.ask - quote.bid, quote.last

    def test_the_published_spread_follows_the_configured_value(self):
        tight, last_tight = self._quoted_spread(0.05)
        wide, last_wide = self._quoted_spread(5.0)
        self.assertAlmostEqual(last_tight, last_wide,
                               msg="same bar, so the same last price")
        self.assertGreater(
            wide, tight * 10,
            "a 100x wider configured spread produced no wider quote, so "
            "the broker is ignoring it and only the risk gate sees it")

    def test_the_default_publishes_what_it_used_to_hardcode(self):
        """So this change could not alter any existing replay result."""
        spread, last = self._quoted_spread(0.05)
        self.assertAlmostEqual(spread, max(0.01, last * 0.0005), places=6)

    def test_the_engine_passes_its_spread_to_the_broker(self):
        """Not just to the risk context. Asserted on the engine source
        because the alternative is a fill-price comparison that depends
        on the whole pipeline agreeing to trade."""
        import inspect
        from agent.replay import engine
        source = inspect.getsource(engine.run)
        self.assertIn("spread_pct=spread_pct", source)


class TestDeclarationsMatchBehaviour(unittest.TestCase):
    """A scenario states up front whether a trade should happen and
    whether a loss may legitimately exceed 1R.

    When a declaration disagrees with what the run actually does, one of
    the two is wrong and somebody should look. `chop` declared
    may_exceed_1r=False and then breached at -1.30R; the declaration was
    wrong, because a polled stop is evaluated at a bar close and filled
    at the next open, so it can never do better than the next print.
    Catching that disagreement is the point of declaring it at all.
    """

    def test_trade_expectations_hold(self):
        for name in scenarios.ALL:
            scenario, result = execute(name)
            with self.subTest(name=name, declared=scenario.trade):
                if scenario.trade == scenarios.TRADE_EXPECTED:
                    self.assertGreater(
                        result.entries_filled, 0,
                        f"{name} declares TRADE_EXPECTED and produced "
                        f"none, so it proves nothing about trade "
                        f"management")
                elif scenario.trade == scenarios.NO_TRADE_EXPECTED:
                    self.assertEqual(
                        result.entries_filled, 0,
                        f"{name} declares NO_TRADE_EXPECTED and traded")

    def test_a_breach_only_happens_where_it_is_declared_possible(self):
        for name in scenarios.ALL:
            scenario, result = execute(name)
            stops = (result.as_dict().get("performance") or {}).get(
                "stop_integrity") or {}
            breaches = stops.get("stop_breaches") or 0
            with self.subTest(name=name, may_exceed=scenario.may_exceed_1r):
                if not scenario.may_exceed_1r:
                    self.assertEqual(
                        breaches, 0,
                        f"{name} declares that a loss may NOT exceed 1R "
                        f"and recorded {breaches} breach(es) with "
                        f"worst_r={stops.get('worst_r')}. Either the "
                        f"declaration is wrong or the risk model is")

    def test_every_scenario_declares_its_failure_mode(self):
        """Losing money is not a failure mode. Behaving incorrectly is."""
        for name in scenarios.ALL:
            with self.subTest(name=name):
                self.assertTrue(scenarios.build(name).expected_failure,
                                f"{name} does not say what going wrong "
                                f"would look like")

    def test_at_least_one_scenario_expects_no_trade(self):
        """Not every scenario may be 'the system should win'."""
        declared = {scenarios.build(n).trade for n in scenarios.ALL}
        self.assertIn(scenarios.NO_TRADE_EXPECTED, declared)

    def test_at_least_one_scenario_expects_a_legitimate_breach(self):
        self.assertTrue(any(scenarios.build(n).may_exceed_1r
                            for n in scenarios.ALL))
