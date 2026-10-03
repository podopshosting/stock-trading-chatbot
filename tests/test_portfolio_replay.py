"""
Several symbols, one envelope.

Single-symbol replay cannot prove the capital constraints, because a
constraint shared between competing candidates is only visible when they
compete. The engine already shared the envelope - three symbols produce
two entries, not six - but WHICH two depended on the order the caller
happened to build the bars dict in, so the same dataset supplied
differently was a different experiment.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.replay import ReplayConfig, run, scenarios                # noqa: E402
from agent.risk import RiskLimits                                    # noqa: E402


def go(symbols, order="ALPHABETICAL", bars=None, **kw):
    scenario = scenarios.build("grind_up")
    series = bars or {s: scenario.bars["XYZ"] for s in symbols}
    config = ReplayConfig(warmup_bars=scenarios.WARMUP,
                          risk_limits=RiskLimits(), starting_cash=100.0,
                          candidate_order=order, **kw)
    return run(series, config, regime_for=scenarios.regime_for(scenario))


class TestTheEnvelopeIsShared(unittest.TestCase):

    def test_three_symbols_do_not_get_three_budgets(self):
        one = go(["XYZ"])
        three = go(["AAA", "BBB", "CCC"])
        self.assertGreater(one.entries_filled, 0, "the control must trade")
        self.assertLessEqual(
            three.entries_filled, one.entries_filled + 1,
            "three symbols produced roughly three times the entries, so "
            "each one is being given its own capital")

    def test_the_concurrent_position_cap_holds_across_symbols(self):
        result = go(["AAA", "BBB", "CCC", "DDD", "EEE"])
        self.assertLessEqual(result.entries_filled,
                             RiskLimits().max_new_positions_per_day)

    def test_a_refused_candidate_names_the_envelope(self):
        result = go(["AAA", "BBB", "CCC"])
        codes = set()
        for bar in result.allocations:
            for candidate in bar["candidates"]:
                codes.update(candidate["reasons"])
        self.assertTrue(
            {"MAX_POSITIONS_REACHED", "INSUFFICIENT_CAPITAL",
             "DAILY_CAPITAL_EXCEEDED"} & codes,
            f"no candidate was refused for want of room; got {codes}")


class TestCandidateOrderIsDeterministic(unittest.TestCase):
    """The same dataset must be the same experiment."""

    def test_the_dict_order_does_not_decide_who_trades(self):
        forward = go(["AAA", "BBB", "CCC"])
        reverse = go(["CCC", "BBB", "AAA"])
        self.assertEqual(
            sorted((forward.as_dict()["performance"] or {}).get(
                "by_symbol", {})),
            sorted((reverse.as_dict()["performance"] or {}).get(
                "by_symbol", {})),
            "reversing the bars dict changed which symbols traded")

    def test_as_supplied_is_available_and_is_order_dependent(self):
        """Kept deliberately, for a caller testing the ordering effect
        itself. This test is what makes the DEFAULT's value visible."""
        forward = go(["AAA", "BBB", "CCC"], order="AS_SUPPLIED")
        reverse = go(["CCC", "BBB", "AAA"], order="AS_SUPPLIED")
        self.assertNotEqual(
            sorted((forward.as_dict()["performance"] or {}).get(
                "by_symbol", {})),
            sorted((reverse.as_dict()["performance"] or {}).get(
                "by_symbol", {})),
            "AS_SUPPLIED was expected to depend on the supplied order; "
            "if it no longer does, the ALPHABETICAL default is not "
            "fixing anything")

    def test_an_unknown_order_is_refused(self):
        """A replay whose candidate order is undefined is not
        reproducible, so it must not run."""
        with self.assertRaises(ValueError):
            go(["AAA", "BBB"], order="BY_VIBES")

    def test_the_order_policy_is_recorded_with_the_result(self):
        self.assertEqual(go(["AAA"]).as_dict()["config"]["candidate_order"],
                         "ALPHABETICAL")

    def test_repeating_a_run_gives_the_same_allocations(self):
        first = go(["AAA", "BBB", "CCC"])
        second = go(["AAA", "BBB", "CCC"])
        self.assertEqual(first.allocations, second.allocations)


class TestWhyAAndNotB(unittest.TestCase):
    """Aggregate refusal counts cannot answer it."""

    def test_a_contested_bar_records_every_candidate(self):
        result = go(["AAA", "BBB", "CCC"])
        self.assertTrue(result.allocations,
                        "no bar was recorded as contested, so the "
                        "portfolio decision is unexplainable")
        bar = result.allocations[0]
        self.assertEqual(sorted(c["symbol"] for c in bar["candidates"]),
                         ["AAA", "BBB", "CCC"])

    def test_the_record_shows_capital_consumed_in_order(self):
        """The winner took capital the loser then did not have."""
        bar = go(["AAA", "BBB", "CCC"]).allocations[0]
        entered = [c for c in bar["candidates"]
                   if c["outcome"] == "ENTERED"]
        refused = [c for c in bar["candidates"]
                   if c["outcome"] == "REFUSED"]
        self.assertTrue(entered)
        self.assertTrue(refused)
        self.assertLess(entered[0]["capital_used_before"],
                        refused[0]["capital_used_before"],
                        "the refused candidate should have faced more "
                        "consumed capital than the one that entered")

    def test_an_uncontested_bar_is_not_recorded(self):
        """One qualifying candidate is not a portfolio decision, and
        recording every bar would bury the ones that matter."""
        single = go(["XYZ"])
        self.assertEqual(single.allocations, [],
                         "a single-symbol run recorded a contested bar")

    def test_the_contested_count_is_surfaced(self):
        self.assertEqual(go(["AAA", "BBB", "CCC"]).as_dict()[
            "contested_bars"], len(go(["AAA", "BBB", "CCC"]).allocations))
