"""
The trade journal and performance analytics.

The failure this suite exists to prevent: looking at fifteen profitable
trades, concluding the strategy works, and scaling up. With two
concurrent positions and a small daily ceiling the sample stays
statistically inadequate for months, and the arithmetic of small samples
is not intuitive - a 100% win rate over twelve trades has a lower
confidence bound of 76%, which sounds impressive and means nothing about
the thirteenth trade.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.broker import (                                        # noqa: E402
    PaperBroker, PaperBrokerConfig, Quote,
)
from agent.journal import (                                       # noqa: E402
    MIN_SAMPLE_FOR_CLAIM, MIN_SAMPLE_FOR_DIRECTION,
    MIN_SAMPLE_FOR_ESTIMATE, MIN_SAMPLE_PER_GROUP, SCRATCH_THRESHOLD_R,
    Adequacy, InMemoryJournal, Metric, RecorderError, TradeAlreadyRecorded,
    TradeCosts, TradeOutcome, TradeRecord, adequacy_for, describe,
    expectancy_r, from_dict, group_by, max_drawdown, mean_interval,
    profit_factor, record_closed_position, stop_integrity, total_costs,
    total_net_pnl, wilson_interval, win_rate,
)
from agent.positions import (                                     # noqa: E402
    ExitPlan, PositionManager,
)

RISK_PER_SHARE = 2.0


def trade(r, i=0, symbol="XYZ", strategy="MOMENTUM",
          exit_reason="PROFIT_TARGET", quantity=1.0, is_paper=True,
          commission=0.0):
    """A trade engineered to produce a given R multiple."""
    entry = 100.0
    return TradeRecord(
        trade_id=f"t{i}", symbol=symbol, quantity=quantity,
        entry_price=entry, exit_price=entry + r * RISK_PER_SHARE,
        opened_at="2026-09-30T14:00:00+00:00",
        closed_at=f"2026-09-30T15:{i % 60:02d}:00+00:00",
        planned_stop=entry - RISK_PER_SHARE, strategy=strategy,
        exit_reason=exit_reason, session_date="2026-09-30",
        is_paper=is_paper, costs=TradeCosts(commission=commission))


def population(wins, losses, r_win=1.0, r_loss=-1.0):
    return ([trade(r_win, i=i) for i in range(wins)]
            + [trade(r_loss, i=i + wins) for i in range(losses)])


class TestRMultiples(unittest.TestCase):
    """The only fair way to compare trades of different sizes."""

    def test_r_is_pnl_over_planned_risk(self):
        t = trade(2.0)
        self.assertAlmostEqual(t.planned_risk, RISK_PER_SHARE, places=6)
        self.assertAlmostEqual(t.r_multiple, 2.0, places=6)

    def test_r_is_size_independent(self):
        """
        A $10 win on $5 of risk and a $100 win on $50 of risk are the
        same trade. Raw P&L says otherwise, which is why sizing changes
        must not look like performance changes.
        """
        small = trade(1.5, quantity=1.0)
        large = trade(1.5, quantity=10.0)
        self.assertNotAlmostEqual(small.net_pnl, large.net_pnl)
        self.assertAlmostEqual(small.r_multiple, large.r_multiple, places=6)

    def test_r_is_none_when_there_was_no_risk_to_measure(self):
        t = trade(1.0)
        t.planned_stop = t.entry_price
        self.assertIsNone(t.r_multiple)
        self.assertEqual(t.outcome, TradeOutcome.UNKNOWN)

    def test_commission_reduces_net_but_not_gross(self):
        t = trade(1.0, commission=0.5)
        self.assertAlmostEqual(t.gross_pnl, 2.0, places=6)
        self.assertAlmostEqual(t.net_pnl, 1.5, places=6)

    def test_costs_are_not_deducted_twice(self):
        """
        The broker's fills already contain spread and slippage, so those
        fields are a breakdown of gross_pnl rather than a further
        deduction. Subtracting them again would understate every result.
        """
        t = trade(1.0)
        t.costs = TradeCosts(entry_slippage=0.3, exit_slippage=0.3,
                             spread_paid=0.4)
        self.assertAlmostEqual(t.net_pnl, t.gross_pnl, places=6)


class TestOutcomeClassification(unittest.TestCase):

    def test_a_win_is_a_win(self):
        self.assertEqual(trade(1.0).outcome, TradeOutcome.WIN)

    def test_a_loss_is_a_loss(self):
        self.assertEqual(trade(-1.0).outcome, TradeOutcome.LOSS)

    def test_a_tiny_move_is_a_scratch_not_a_win(self):
        """
        Counting scratches as wins is a cheap way to inflate a win
        rate. A move smaller than a tenth of the risk taken is noise.
        """
        self.assertEqual(trade(SCRATCH_THRESHOLD_R / 2).outcome,
                         TradeOutcome.SCRATCH)
        self.assertEqual(trade(-SCRATCH_THRESHOLD_R / 2).outcome,
                         TradeOutcome.SCRATCH)

    def test_scratches_are_excluded_from_the_win_rate_denominator(self):
        """
        Including them would let a strategy that mostly goes nowhere
        report a flattering win rate by diluting its losses.
        """
        trades = ([trade(0.01, i=i) for i in range(10)]
                  + [trade(-1.0, i=i + 10) for i in range(10)])
        self.assertEqual(win_rate(trades).sample_size, 10)
        self.assertAlmostEqual(win_rate(trades).value, 0.0, places=6)

    def test_an_unmeasurable_trade_still_counts_toward_the_money(self):
        """
        "Cannot compute R" and "did not happen" are different things. A
        trade with no measurable risk still moved real cash, and a
        reported P&L that omits it would disagree with the account
        balance - a worse failure than an incomplete statistic.
        """
        scoreable = population(5, 5)
        unmeasurable = trade(1.0, i=99)
        unmeasurable.planned_stop = unmeasurable.entry_price
        self.assertEqual(unmeasurable.outcome, TradeOutcome.UNKNOWN)
        self.assertNotEqual(unmeasurable.net_pnl, 0.0)

        with_it = total_net_pnl(scoreable + [unmeasurable])
        without = total_net_pnl(scoreable)
        self.assertAlmostEqual(with_it - without, unmeasurable.net_pnl,
                               places=6)

    def test_the_money_total_reconciles_with_the_sum_of_every_trade(self):
        """The total must equal what the account actually did."""
        trades = population(5, 5)
        broken = trade(-2.0, i=99)
        broken.planned_stop = broken.entry_price
        everything = trades + [broken]
        self.assertAlmostEqual(total_net_pnl(everything),
                               sum(t.net_pnl for t in everything),
                               places=6)

    def test_drawdown_includes_unmeasurable_trades(self):
        """
        The drawdown experienced is an account fact. Omitting a trade
        because its R could not be computed would understate the fall
        that actually happened.
        """
        loser = trade(-5.0, i=1)
        loser.planned_stop = loser.entry_price     # UNKNOWN outcome
        self.assertEqual(loser.outcome, TradeOutcome.UNKNOWN)
        self.assertLess(max_drawdown([trade(1.0, i=0), loser]).value, 0.0)

    def test_unknown_outcomes_are_excluded_from_every_denominator(self):
        """Missing data is not a neutral result."""
        good = population(5, 5)
        broken = trade(1.0, i=99)
        broken.planned_stop = broken.entry_price
        self.assertEqual(broken.outcome, TradeOutcome.UNKNOWN)
        self.assertEqual(win_rate(good + [broken]).sample_size,
                         win_rate(good).sample_size)
        report = describe(good + [broken])
        self.assertEqual(report["trades_excluded_unknown"], 1)
        # The statistical sample must not silently absorb it.
        self.assertEqual(report["trades_counted"], len(good))

    def test_an_unmeasurable_trade_is_kept_out_of_the_profit_factor(self):
        """
        Its P&L is real, but a ratio built from trades whose risk is
        unknown is not measuring the strategy.
        """
        scoreable = population(5, 5)
        broken = trade(3.0, i=99)
        broken.planned_stop = broken.entry_price
        self.assertAlmostEqual(profit_factor(scoreable + [broken]).value,
                               profit_factor(scoreable).value, places=6)

    def test_an_unmeasurable_trade_is_kept_out_of_the_groups(self):
        broken = trade(1.0, i=99, strategy="MYSTERY")
        broken.planned_stop = broken.entry_price
        groups = group_by(population(2, 2) + [broken], "strategy")
        self.assertNotIn("MYSTERY", groups)


class TestWilsonInterval(unittest.TestCase):
    """Validated against published reference values."""

    def test_matches_published_values(self):
        for successes, n, low, high in [
            (60, 100, 0.5020, 0.6906),
            (12, 12, 0.7572, 1.0000),
            (5, 10, 0.2366, 0.7634),
            (0, 10, 0.0000, 0.2775),
        ]:
            with self.subTest(successes=successes, n=n):
                got_low, got_high = wilson_interval(successes, n)
                self.assertAlmostEqual(got_low, low, places=2)
                self.assertAlmostEqual(got_high, high, places=2)

    def test_the_interval_narrows_as_the_sample_grows(self):
        widths = []
        for n in (10, 50, 200, 1000):
            low, high = wilson_interval(n // 2, n)
            widths.append(high - low)
        self.assertEqual(widths, sorted(widths, reverse=True))

    def test_an_empty_sample_has_no_interval(self):
        self.assertEqual(wilson_interval(0, 0), (None, None))

    def test_the_interval_stays_within_zero_and_one(self):
        for successes, n in ((0, 3), (3, 3), (1, 2)):
            with self.subTest(successes=successes, n=n):
                low, high = wilson_interval(successes, n)
                self.assertGreaterEqual(low, 0.0)
                self.assertLessEqual(high, 1.0)


class TestSampleAdequacy(unittest.TestCase):
    """What a number is ALLOWED to be called."""

    def test_twelve_profitable_trades_demonstrate_nothing(self):
        """
        The seductive case. A 100% win rate over twelve trades has a
        lower bound of 76%, which sounds like a strategy and is not one.
        """
        report = describe([trade(1.5, i=i) for i in range(12)])
        self.assertEqual(report["verdict"], "NO_EDGE_DEMONSTRATED")
        self.assertFalse(report["metrics"]["win_rate"]["is_evidence"])
        self.assertFalse(report["metrics"]["expectancy_r"]["is_evidence"])

    def test_the_summary_warns_against_increasing_size(self):
        report = describe([trade(1.5, i=i) for i in range(12)])
        self.assertIn("increasing size", report["summary"])

    def test_the_thresholds_escalate_with_sample_size(self):
        self.assertEqual(adequacy_for(0), Adequacy.INSUFFICIENT)
        self.assertEqual(adequacy_for(MIN_SAMPLE_FOR_DIRECTION - 1),
                         Adequacy.INSUFFICIENT)
        self.assertEqual(adequacy_for(MIN_SAMPLE_FOR_DIRECTION),
                         Adequacy.DIRECTIONAL)
        self.assertEqual(adequacy_for(MIN_SAMPLE_FOR_ESTIMATE),
                         Adequacy.ESTIMATE)
        self.assertEqual(adequacy_for(MIN_SAMPLE_FOR_CLAIM),
                         Adequacy.DEMONSTRATED)

    def test_a_metric_with_an_interval_spanning_the_null_is_not_evidence(self):
        metric = Metric(name="x", value=0.6, sample_size=500,
                        adequacy=Adequacy.DEMONSTRATED,
                        ci_low=0.4, ci_high=0.8, null_value=0.5)
        self.assertFalse(metric.excludes_null)
        self.assertFalse(metric.is_evidence)

    def test_a_large_sample_with_a_clear_interval_is_evidence(self):
        """The falsifying control: something must be able to qualify."""
        metric = Metric(name="x", value=0.6, sample_size=500,
                        adequacy=Adequacy.DEMONSTRATED,
                        ci_low=0.55, ci_high=0.65, null_value=0.5)
        self.assertTrue(metric.excludes_null)
        self.assertTrue(metric.is_evidence)

    def test_a_clear_interval_on_a_small_sample_is_still_not_evidence(self):
        """Both conditions are required, not either."""
        metric = Metric(name="x", value=1.0, sample_size=5,
                        adequacy=Adequacy.INSUFFICIENT,
                        ci_low=0.8, ci_high=1.0, null_value=0.5)
        self.assertTrue(metric.excludes_null)
        self.assertFalse(metric.is_evidence)

    def test_excludes_null_is_derived_not_assignable(self):
        metric = Metric(name="x", value=0.6, sample_size=5,
                        adequacy=Adequacy.INSUFFICIENT)
        with self.assertRaises(AttributeError):
            metric.excludes_null = True

    def test_is_evidence_is_derived_not_assignable(self):
        metric = Metric(name="x", value=0.6, sample_size=5,
                        adequacy=Adequacy.INSUFFICIENT)
        with self.assertRaises(AttributeError):
            metric.is_evidence = True

    def test_a_large_winning_sample_can_reach_a_verdict(self):
        """
        The falsifying control for the whole module: the gate must be
        passable, or it is not a gate but a refusal to ever measure.
        """
        report = describe(population(80, 40, r_win=2.0, r_loss=-1.0))
        self.assertEqual(report["verdict"], "POSITIVE_EDGE_DEMONSTRATED")
        self.assertTrue(report["metrics"]["expectancy_r"]["is_evidence"])

    def test_a_large_losing_sample_reaches_a_negative_verdict(self):
        report = describe(population(20, 100, r_win=1.0, r_loss=-1.0))
        self.assertEqual(report["verdict"], "NEGATIVE_EDGE_DEMONSTRATED")

    def test_no_trades_is_its_own_verdict(self):
        self.assertEqual(describe([])["verdict"], "NO_TRADES")

    def test_the_numbers_are_always_reported_even_when_inadequate(self):
        """
        Withholding the figures would be its own dishonesty. What
        changes with sample size is the claim, not the disclosure.
        """
        report = describe([trade(1.0, i=i) for i in range(3)])
        self.assertIsNotNone(report["metrics"]["win_rate"]["value"])
        self.assertIsNotNone(report["total_net_pnl"])


class TestExpectancy(unittest.TestCase):

    def test_expectancy_is_the_mean_r(self):
        metric = expectancy_r(population(1, 1, r_win=3.0, r_loss=-1.0))
        self.assertAlmostEqual(metric.value, 1.0, places=6)

    def test_a_low_win_rate_can_still_be_profitable(self):
        """
        Win rate alone says nothing. A 40% win rate at 3R against 1R is
        a good strategy, and a report that leads with win rate will
        mislead about it.
        """
        trades = population(40, 60, r_win=3.0, r_loss=-1.0)
        self.assertAlmostEqual(win_rate(trades).value, 0.4, places=6)
        self.assertGreater(expectancy_r(trades).value, 0.0)

    def test_a_high_win_rate_can_still_lose_money(self):
        trades = population(80, 20, r_win=0.25, r_loss=-3.0)
        self.assertAlmostEqual(win_rate(trades).value, 0.8, places=6)
        self.assertLess(expectancy_r(trades).value, 0.0)

    def test_the_mean_interval_needs_two_points(self):
        mean, low, high = mean_interval([1.0])
        self.assertEqual(mean, 1.0)
        self.assertIsNone(low)
        self.assertIsNone(high)

    def test_an_empty_mean_is_none(self):
        self.assertEqual(mean_interval([]), (None, None, None))


class TestProfitFactorAndDrawdown(unittest.TestCase):

    def test_profit_factor_divides_wins_by_losses(self):
        trades = population(2, 1, r_win=1.0, r_loss=-1.0)
        self.assertAlmostEqual(profit_factor(trades).value, 2.0, places=6)

    def test_no_losses_yet_means_no_profit_factor(self):
        """
        An infinite profit factor is not a good sign, it is a sign of
        too small a sample, and reporting it as a number invites the
        wrong conclusion.
        """
        metric = profit_factor([trade(1.0, i=i) for i in range(5)])
        self.assertIsNone(metric.value)
        self.assertIn("sample size", metric.note)

    def test_drawdown_is_measured_along_the_equity_curve(self):
        trades = [trade(2.0, i=0), trade(-3.0, i=1), trade(1.0, i=2)]
        # Equity: +4, -2, ... peak 4, trough -2 -> drawdown -6
        self.assertAlmostEqual(max_drawdown(trades).value, -6.0, places=6)

    def test_drawdown_is_never_positive(self):
        self.assertLessEqual(
            max_drawdown([trade(1.0, i=i) for i in range(5)]).value, 0.0)

    def test_drawdown_uses_close_order_not_insertion_order(self):
        """The equity curve is the order it was actually experienced."""
        a = trade(2.0, i=1)
        b = trade(-3.0, i=2)
        self.assertAlmostEqual(max_drawdown([b, a]).value,
                               max_drawdown([a, b]).value, places=6)

    def test_drawdown_notes_that_the_worst_is_probably_ahead(self):
        self.assertIn("still to come", max_drawdown(population(5, 5)).note)


class TestStopIntegrity(unittest.TestCase):
    """The most important diagnostic in the module."""

    def test_a_loss_worse_than_one_r_is_a_breach(self):
        """
        If losses routinely exceed 1R the stops are not holding, every
        upstream risk calculation is wrong and position sizes are
        systematically too large.
        """
        self.assertTrue(trade(-1.5).exceeded_planned_risk)
        self.assertFalse(trade(-0.9).exceeded_planned_risk)
        self.assertFalse(trade(2.0).exceeded_planned_risk)

    def test_the_breach_rate_is_reported(self):
        result = stop_integrity([trade(-1.0, i=0), trade(-2.5, i=1),
                                 trade(-1.8, i=2), trade(2.0, i=3)])
        self.assertEqual(result["stop_breaches"], 2)
        self.assertAlmostEqual(result["breach_rate"], 0.5, places=6)
        self.assertAlmostEqual(result["worst_r"], -2.5, places=4)

    def test_breached_symbols_are_named(self):
        result = stop_integrity([trade(-2.0, i=0, symbol="AAA"),
                                 trade(-2.0, i=1, symbol="BBB"),
                                 trade(1.0, i=2, symbol="CCC")])
        self.assertEqual(result["breached_symbols"], ["AAA", "BBB"])

    def test_a_clean_history_reports_no_breaches(self):
        result = stop_integrity(population(5, 5, r_loss=-1.0))
        self.assertEqual(result["stop_breaches"], 0)
        self.assertAlmostEqual(result["breach_rate"], 0.0, places=6)

    def test_stop_integrity_appears_in_the_report(self):
        self.assertIn("stop_integrity", describe(population(2, 2)))


class TestGrouping(unittest.TestCase):

    def test_small_groups_are_marked_not_comparable(self):
        """
        Picking the best cell out of a set of three-trade groups is
        choosing noise and calling it insight.
        """
        trades = ([trade(2.0, i=i, strategy="MOMENTUM") for i in range(3)]
                  + [trade(-1.0, i=i + 3, strategy="BREAKOUT")
                     for i in range(MIN_SAMPLE_PER_GROUP + 5)])
        groups = group_by(trades, "strategy")
        self.assertFalse(groups["MOMENTUM"]["comparable"])
        self.assertTrue(groups["BREAKOUT"]["comparable"])

    def test_a_small_group_still_reports_its_numbers(self):
        groups = group_by([trade(2.0, i=0, strategy="MOMENTUM")], "strategy")
        self.assertIsNotNone(groups["MOMENTUM"]["expectancy_r"])
        self.assertIn("not comparable", groups["MOMENTUM"]["note"])

    def test_grouping_by_exit_reason_works(self):
        trades = [trade(1.0, i=0, exit_reason="PROFIT_TARGET"),
                  trade(-1.0, i=1, exit_reason="HARD_STOP")]
        groups = group_by(trades, "exit_reason")
        self.assertEqual(set(groups), {"PROFIT_TARGET", "HARD_STOP"})

    def test_a_missing_group_key_is_labelled_not_dropped(self):
        groups = group_by([trade(1.0, i=0, strategy="")], "strategy")
        self.assertIn("UNSPECIFIED", groups)

    def test_group_counts_sum_to_the_counted_total(self):
        trades = population(10, 10)
        groups = group_by(trades, "strategy")
        self.assertEqual(sum(g["count"] for g in groups.values()),
                         len(trades))


class TestCosts(unittest.TestCase):

    def test_costs_are_reported_against_gross(self):
        """
        The ratio reveals a strategy whose gross edge is real but
        entirely consumed by execution - a different problem from having
        no edge, needing a different response.
        """
        t = trade(1.0, i=0)
        t.costs = TradeCosts(entry_slippage=0.5, exit_slippage=0.5)
        result = total_costs([t])
        self.assertAlmostEqual(result["total_costs"], 1.0, places=6)
        self.assertAlmostEqual(result["gross_pnl"], 2.0, places=6)
        self.assertAlmostEqual(result["costs_as_pct_of_gross_wins"], 50.0,
                               places=2)

    def test_the_report_says_costs_are_already_in_the_fills(self):
        self.assertIn("not a further deduction",
                      total_costs(population(2, 2))["note"])

    def test_costs_total_sums_its_parts(self):
        costs = TradeCosts(entry_slippage=1.0, exit_slippage=2.0,
                           spread_paid=3.0, commission=4.0)
        self.assertAlmostEqual(costs.total, 10.0, places=6)


class TestJournalStore(unittest.TestCase):

    def test_a_trade_can_be_recorded_and_retrieved(self):
        journal = InMemoryJournal()
        t = trade(1.0, i=1)
        journal.record(t)
        self.assertEqual(journal.get(t.trade_id).trade_id, t.trade_id)

    def test_the_journal_is_append_only(self):
        """
        A performance history that can be revised is not a record of
        anything.
        """
        journal = InMemoryJournal()
        t = trade(1.0, i=1)
        journal.record(t)
        with self.assertRaises(TradeAlreadyRecorded):
            journal.record(t)

    def test_trades_list_in_close_order(self):
        journal = InMemoryJournal()
        for i in (5, 1, 3):
            journal.record(trade(1.0, i=i))
        closes = [t.closed_at for t in journal.list_trades()]
        self.assertEqual(closes, sorted(closes))

    def test_trades_can_be_filtered_by_session(self):
        journal = InMemoryJournal()
        a = trade(1.0, i=1)
        b = trade(1.0, i=2)
        b.session_date = "2026-10-01"
        journal.record(a)
        journal.record(b)
        self.assertEqual(len(journal.list_trades(session_date="2026-10-01")),
                         1)

    def test_a_round_trip_through_serialisation_preserves_the_record(self):
        t = trade(1.5, i=7)
        rebuilt = from_dict(t.as_dict())
        self.assertEqual(rebuilt.trade_id, t.trade_id)
        self.assertAlmostEqual(rebuilt.r_multiple, t.r_multiple, places=6)
        self.assertEqual(rebuilt.outcome, t.outcome)

    def test_derived_values_are_recomputed_not_trusted_from_storage(self):
        """
        A stale or tampered derived value in storage must not survive a
        round trip, or the journal could disagree with itself.
        """
        t = trade(1.0, i=8)
        data = t.as_dict()
        data["r_multiple"] = 99.0
        data["outcome"] = "WIN"
        rebuilt = from_dict(data)
        self.assertAlmostEqual(rebuilt.r_multiple, 1.0, places=6)


class TestRecorder(unittest.TestCase):
    """The seam where provenance is preserved or lost."""

    def _closed_trade(self, stop=97.0, exit_price=None, trail_to=None):
        broker = PaperBroker(PaperBrokerConfig(starting_cash=5000.0, seed=3,
                                               partial_fill_probability=0.0))
        broker.set_quote(Quote(symbol="XYZ", bid=99.9, ask=100.1, last=100.0))
        entry = broker.submit_order("XYZ", "BUY", 1.0)
        manager = PositionManager(broker=broker, execution_available=True)
        position = manager.open_from_order(
            entry, ExitPlan(stop_price=stop, target_price=110.0),
            hypothesis_id="h1", risk_decision_id="d1",
            config_version="pos-1")
        if trail_to is not None:
            position.tighten_stop(trail_to, reason="TRAILING_STOP")
        if exit_price is not None:
            broker.set_quote(Quote(symbol="XYZ", bid=exit_price - 0.1,
                                   ask=exit_price + 0.1, last=exit_price))
        exit_order = broker.close_position("XYZ")
        return position, entry, exit_order

    def test_a_closed_position_becomes_a_journal_entry(self):
        journal = InMemoryJournal()
        position, entry, exit_order = self._closed_trade(exit_price=105.0)
        record = record_closed_position(
            journal, position, exit_order, entry_order=entry,
            session_date="2026-09-30",
            config_versions={"positions": "pos-1", "risk": "risk-1"})
        self.assertEqual(record.symbol, "XYZ")
        self.assertGreater(record.net_pnl, 0.0)
        self.assertEqual(len(journal.list_trades()), 1)

    def test_provenance_survives_into_the_journal(self):
        """
        Once the position is gone, anything not copied here is
        unrecoverable.
        """
        journal = InMemoryJournal()
        position, entry, exit_order = self._closed_trade(exit_price=105.0)
        record = record_closed_position(
            journal, position, exit_order, entry_order=entry,
            config_versions={"positions": "pos-1"})
        self.assertEqual(record.hypothesis_id, "h1")
        self.assertEqual(record.risk_decision_id, "d1")
        self.assertEqual(record.position_id, position.position_id)
        self.assertEqual(record.entry_order_id, entry["order_id"])
        self.assertEqual(record.config_versions["positions"], "pos-1")

    def test_r_is_measured_against_the_stop_accepted_at_entry(self):
        """
        Using the trailed stop would rewrite history and make every
        trailing exit look like a 0R scratch, hiding the fact that the
        trade risked something when it was taken.
        """
        journal = InMemoryJournal()
        position, entry, exit_order = self._closed_trade(
            stop=97.0, trail_to=104.0, exit_price=105.0)
        record = record_closed_position(journal, position, exit_order,
                                        entry_order=entry)
        self.assertAlmostEqual(record.planned_stop, 97.0, places=4)
        self.assertGreater(record.planned_risk, 3.0)

    def test_an_exit_without_a_fill_price_cannot_be_journalled(self):
        """An unknown result must not be recorded as a known one."""
        journal = InMemoryJournal()
        position, entry, _ = self._closed_trade(exit_price=105.0)
        with self.assertRaises(RecorderError):
            record_closed_position(journal, position,
                                   {"order_id": "x", "fills": []},
                                   entry_order=entry)

    def test_no_position_raises(self):
        with self.assertRaises(RecorderError):
            record_closed_position(InMemoryJournal(), None,
                                   {"average_fill_price": 1.0})

    def test_the_excursion_extremes_are_carried_over(self):
        journal = InMemoryJournal()
        position, entry, exit_order = self._closed_trade(exit_price=105.0)
        position.high_water_price = 108.0
        position.low_water_price = 98.0
        record = record_closed_position(journal, position, exit_order,
                                        entry_order=entry)
        self.assertGreater(record.max_favourable_excursion_r, 0.0)
        self.assertLess(record.max_adverse_excursion_r, 0.0)

    def test_the_cost_breakdown_does_not_double_count_the_overlap(self):
        """
        Slippage and spread overlap - the fill price contains both - so
        the spread component is reduced by the slippage already counted.
        Summing them naively would report a cost larger than the trade
        actually paid.
        """
        from agent.journal import costs_from_orders
        entry_order = {
            "average_fill_price": 100.5,
            "fills": [{"price": 100.5, "quantity": 1.0,
                       "slippage_bps": 50.0}],       # ~0.50 of slippage
        }
        costs = costs_from_orders(entry_order, None, quantity=1.0,
                                  reference_entry=100.0)
        # The raw spread estimate is |100.5 - 100.0| * 1 = 0.50, which is
        # entirely accounted for by the 0.50 of slippage already counted.
        self.assertAlmostEqual(costs.entry_slippage, 0.50, places=2)
        self.assertAlmostEqual(costs.spread_paid, 0.0, places=2)
        self.assertAlmostEqual(costs.total, 0.50, places=2)

    def test_spread_beyond_the_slippage_is_still_counted(self):
        """The falsifying control: the overlap subtraction must not
        simply zero the spread in every case."""
        from agent.journal import costs_from_orders
        entry_order = {
            "average_fill_price": 102.0,
            "fills": [{"price": 102.0, "quantity": 1.0,
                       "slippage_bps": 10.0}],       # ~0.10 of slippage
        }
        costs = costs_from_orders(entry_order, None, quantity=1.0,
                                  reference_entry=100.0)
        self.assertGreater(costs.spread_paid, 1.5)

    def test_the_cost_breakdown_is_never_negative(self):
        from agent.journal import costs_from_orders
        entry_order = {
            "average_fill_price": 100.01,
            "fills": [{"price": 100.01, "quantity": 1.0,
                       "slippage_bps": 500.0}],      # slippage >> spread
        }
        costs = costs_from_orders(entry_order, None, quantity=1.0,
                                  reference_entry=100.0)
        self.assertGreaterEqual(costs.spread_paid, 0.0)
        self.assertGreaterEqual(costs.total, 0.0)

    def test_paper_trades_are_marked_paper(self):
        journal = InMemoryJournal()
        position, entry, exit_order = self._closed_trade(exit_price=105.0)
        record = record_closed_position(journal, position, exit_order,
                                        entry_order=entry)
        self.assertTrue(record.is_paper)
        self.assertTrue(describe([record])["paper_only"])

    def test_a_report_of_mixed_paper_and_live_is_not_paper_only(self):
        self.assertFalse(
            describe([trade(1.0, i=0), trade(1.0, i=1, is_paper=False)]
                     )["paper_only"])


class TestDeterminism(unittest.TestCase):

    def test_the_same_trades_give_the_same_report(self):
        trades = population(30, 20)
        first, second = describe(trades), describe(trades)
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(first["verdict"], second["verdict"])
        self.assertEqual(first["by_strategy"], second["by_strategy"])
        self.assertEqual(first["stop_integrity"], second["stop_integrity"])

    def test_trade_order_does_not_change_the_aggregates(self):
        """
        Reordering the input must not move a metric. Drawdown is the
        exception by design - it follows the equity curve - and it is
        sorted by close time so it is stable too.
        """
        trades = population(20, 15)
        forward, backward = describe(trades), describe(list(reversed(trades)))
        self.assertEqual(forward["metrics"], backward["metrics"])

    def test_the_report_records_its_configuration_version(self):
        self.assertTrue(describe(population(2, 2))["config_version"])


if __name__ == "__main__":
    unittest.main()
