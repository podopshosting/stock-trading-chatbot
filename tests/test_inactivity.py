"""
Why the agent did not trade.

A quiet day and a broken day look identical unless the system can say
which it was. The failure mode this guards against is an explanation
that lists every box ticked rather than the one that actually bound, and
an explanation that reads as a tuning recommendation when a reached
ceiling is the system working.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.autonomy.inactivity import (                           # noqa: E402
    CATEGORY, MEANING, STAGE_ORDER, WOULD_HAVE_TRADED,
    explain_inactivity,
)


def refused(symbol, *codes):
    return {"symbol": symbol, "outcome": "REFUSED",
            "risk": {"reason_codes": list(codes)}}


def entered(symbol):
    return {"symbol": symbol, "outcome": "ENTERED", "risk": {}}


class TestTheBindingConstraintIsNamed(unittest.TestCase):

    def test_the_first_constraint_in_pipeline_order_wins(self):
        """A hypothesis too weak was never going to be sized, so listing
        INSUFFICIENT_CAPITAL as well would double-count one decision."""
        out = explain_inactivity([refused(
            "AAA", "INSUFFICIENT_CAPITAL", "HYPOTHESIS_TOO_WEAK",
            "NOT_LONG_ONLY")])
        codes = [s["code"] for s in out["stages"]]
        self.assertEqual(codes, ["HYPOTHESIS_TOO_WEAK"])

    def test_a_protective_state_outranks_everything(self):
        out = explain_inactivity([refused(
            "AAA", "HYPOTHESIS_TOO_WEAK", "SPREAD_TOO_WIDE",
            "DAILY_RISK_LOCK")])
        self.assertEqual(out["stages"][0]["code"], "DAILY_RISK_LOCK")
        self.assertEqual(out["stages"][0]["category"], "PROTECTION")

    def test_stale_data_outranks_the_opportunity_question(self):
        """If we could not see, we cannot say the setup was absent."""
        out = explain_inactivity([refused("AAA", "HYPOTHESIS_TOO_WEAK",
                                          "STALE_MARKET_DATA")])
        self.assertEqual(out["stages"][0]["code"], "STALE_MARKET_DATA")
        self.assertEqual(out["stages"][0]["category"], "DATA")

    def test_each_decision_is_counted_once(self):
        rows = [refused("A", "HYPOTHESIS_TOO_WEAK", "NOT_LONG_ONLY"),
                refused("B", "HYPOTHESIS_TOO_WEAK"),
                refused("C", "SPREAD_TOO_WIDE")]
        out = explain_inactivity(rows)
        self.assertEqual(sum(s["decisions"] for s in out["stages"]), 3)

    def test_symbols_are_listed_without_duplicates(self):
        out = explain_inactivity([refused("A", "SPREAD_TOO_WIDE"),
                                  refused("A", "SPREAD_TOO_WIDE")])
        self.assertEqual(out["stages"][0]["symbols"], ["A"])
        self.assertEqual(out["stages"][0]["decisions"], 2)

    def test_an_unrecognised_code_is_not_dropped(self):
        out = explain_inactivity([refused("A", "SOMETHING_NEW")])
        self.assertEqual(out["stages"][0]["code"], "SOMETHING_NEW")
        self.assertEqual(out["stages"][0]["category"], "UNKNOWN")

    def test_every_ordered_stage_has_a_category(self):
        for code in STAGE_ORDER:
            with self.subTest(code=code):
                self.assertIn(code, CATEGORY)


class TestTheHeadlineSaysWhichKindOfQuietDayItWas(unittest.TestCase):

    def test_no_opportunity_is_distinguished_from_no_room(self):
        market = explain_inactivity([refused("A", "HYPOTHESIS_TOO_WEAK")])
        self.assertIn("did not offer a qualifying setup", market["headline"])
        self.assertEqual(market["would_have_traded_with_more_room"], 0)

        room = explain_inactivity([refused("A", "DAILY_CAPITAL_EXCEEDED")])
        self.assertIn("want of room", room["headline"])
        self.assertEqual(room["would_have_traded_with_more_room"], 1)

    def test_an_empty_decision_log_says_nothing_reached_the_governor(self):
        out = explain_inactivity([])
        self.assertIn("No candidate reached the Risk Governor",
                      out["headline"])

    def test_a_day_that_traded_says_so(self):
        out = explain_inactivity([entered("A"),
                                  refused("B", "HYPOTHESIS_TOO_WEAK")])
        self.assertIn("1 position(s) were opened", out["headline"])
        self.assertEqual(out["funnel"]["entered"], 1)

    def test_a_reached_ceiling_is_not_framed_as_a_limit_to_raise(self):
        out = explain_inactivity([refused("A", "DAILY_CAPITAL_EXCEEDED")])
        self.assertIn("not a limit to raise", out["note"])
        self.assertIn("ceiling, not a target",
                      out["stages"][0]["meaning"])

    def test_the_funnel_accounts_for_every_decision(self):
        rows = [entered("A"), refused("B", "SPREAD_TOO_WIDE"),
                {"symbol": "C", "outcome": "NOT_EVALUATED"}]
        out = explain_inactivity(rows)
        funnel = out["funnel"]
        self.assertEqual(funnel["decisions_recorded"], 3)
        self.assertEqual(funnel["entered"] + funnel["refused_by_risk"]
                         + funnel["not_evaluated"], 3)

    def test_only_budget_and_protection_mean_it_would_have_traded(self):
        self.assertEqual(WOULD_HAVE_TRADED, {"BUDGET", "PROTECTION"})
        for code, category in CATEGORY.items():
            if category in ("MARKET", "QUALITY", "DATA", "POLICY", "TIMING"):
                with self.subTest(code=code):
                    out = explain_inactivity([refused("A", code)])
                    self.assertEqual(
                        out["would_have_traded_with_more_room"], 0)

    def test_every_meaning_describes_rather_than_recommends(self):
        """Word boundaries, not substrings: "try" matches inside "entry",
        which is how this check first produced a false positive."""
        import re
        banned = (r"should", r"increase\b", r"raise\b", r"recommend",
                  r"\btry\b", r"\bloosen\b", r"\brelax\b")
        for code, text in MEANING.items():
            for pattern in banned:
                with self.subTest(code=code, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text.lower()),
                                      f"{code}: {text}")


class TestTodaysRealShape(unittest.TestCase):
    """The 2026-10-01 session: 2 entries, 38 refusals, 47 not evaluated."""

    def rows(self):
        out = [entered("MSFT"), entered("ASML")]
        for i in range(29):
            out.append(refused(f"S{i}", "HYPOTHESIS_NOT_ACTIONABLE",
                               "NOT_LONG_ONLY", "INSUFFICIENT_CAPITAL"))
        for i in range(6):
            out.append(refused(f"W{i}", "HYPOTHESIS_TOO_WEAK"))
        for i in range(3):
            out.append(refused(f"X{i}", "SPREAD_TOO_WIDE"))
        out += [{"symbol": f"N{i}", "outcome": "NOT_EVALUATED"}
                for i in range(47)]
        return out

    def test_it_reads_as_no_qualifying_setup_not_as_no_capital(self):
        out = explain_inactivity(self.rows(), scanned=8)
        self.assertEqual(out["funnel"]["entered"], 2)
        self.assertEqual(out["by_category"].get("BUDGET", 0), 0)
        self.assertEqual(out["stages"][0]["code"],
                         "HYPOTHESIS_NOT_ACTIONABLE")

    def test_the_scanned_count_is_carried_through(self):
        out = explain_inactivity(self.rows(), scanned=8)
        self.assertEqual(out["funnel"]["symbols_scanned"], 8)


if __name__ == "__main__":
    unittest.main()
