import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.company.holding_context import holding_context, OVERNIGHT_EXECUTION
from agent.company.models import DividendProfile, DividendStatus


def fund(growth=0.05, dte=1.0, cr=1.5, fcf=100, ni=100):
    def m(n, v): return {"metric": n, "value": v}
    return {"derived": [m("revenue_growth", growth), m("debt_to_equity", dte),
                        m("current_ratio", cr)],
            "income": {"net_income": {"value": ni}},
            "cash_flow": {"free_cash_flow": {"value": fcf}}}


ACTIVE = DividendProfile(DividendStatus.ACTIVE, True, "x")
EPS_UP = {"eps_yoy": {"value": 0.10}}


class TestHolding(unittest.TestCase):
    def run_(self, **kw):
        a = dict(dividend=ACTIVE, fundamentals=fund(), earnings_trends=EPS_UP,
                 splits=None, days_to_earnings=None, freshness="CURRENT")
        a.update(kw)
        return holding_context(**a)

    def test_favorable_needs_three_positives_and_no_cautions(self):
        self.assertEqual(self.run_()["label"], "FAVORABLE")

    def test_sparse_data_is_insufficient_not_neutral(self):
        r = self.run_(dividend=None, fundamentals=None, earnings_trends=None)
        self.assertEqual(r["label"], "INSUFFICIENT_DATA")

    def test_stale_fundamentals_cannot_yield_a_judgement(self):
        self.assertEqual(self.run_(freshness="STALE")["label"],
                         "INSUFFICIENT_DATA")

    def test_two_negatives_are_unfavorable(self):
        r = self.run_(fundamentals=fund(growth=-0.2, fcf=-5),
                      earnings_trends={"eps_yoy": {"value": 0.1}})
        self.assertEqual(r["label"], "UNFAVORABLE")

    def test_upcoming_earnings_adds_a_caution(self):
        r = self.run_(days_to_earnings=3)
        self.assertEqual(r["label"], "NEUTRAL")
        self.assertTrue(any(f["factor"] == "upcoming_earnings_risk"
                            for f in r["factors"]))

    def test_ex_dividend_is_flagged_as_not_free_return(self):
        d = DividendProfile(DividendStatus.ACTIVE, True, "x", days_until_ex=2)
        r = self.run_(dividend=d)
        f = [x for x in r["factors"] if x["factor"] == "ex_dividend_adjustment"]
        self.assertIn("not free return", f[0]["why"])

    def test_never_an_instruction_never_a_score(self):
        r = self.run_()
        self.assertFalse(r["is_execution_instruction"])
        self.assertIsNone(r["composite_score"])
        self.assertEqual(r["overnight_execution"], "DISABLED")
        self.assertEqual(OVERNIGHT_EXECUTION, "DISABLED")

    def test_every_factor_names_its_reason(self):
        for f in self.run_()["factors"]:
            self.assertTrue(f["why"])

    def test_no_dividend_company_is_not_penalised(self):
        d = DividendProfile(DividendStatus.NO_DIVIDEND, False, "x")
        r = self.run_(dividend=d)
        f = [x for x in r["factors"] if x["factor"] == "dividend_profile"][0]
        self.assertEqual(f["state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()


class TestAgingDataCapsTheVerdict(unittest.TestCase):
    """
    Found by live validation 2026-10-01: COST read FAVORABLE while its
    fundamentals were AGING. Freshness was held out of the factor tally
    (it is not a fact about the company) and was then ignored entirely,
    so a favourable reading could rest on a stale quarter.
    """

    def run_(self, freshness):
        return holding_context(
            dividend=ACTIVE, fundamentals=fund(), earnings_trends=EPS_UP,
            splits=None, days_to_earnings=None, freshness=freshness)

    def test_current_data_can_still_be_favorable(self):
        """The control: without it, capping could hide a verdict that was
        never reachable."""
        self.assertEqual(self.run_("CURRENT")["label"], "FAVORABLE")

    def test_aging_data_is_capped_at_neutral(self):
        out = self.run_("AGING")
        self.assertEqual(out["label"], "NEUTRAL")
        self.assertIn("AGING", out["why"])
        self.assertIn("not shown to be current", out["why"])

    def test_stale_data_still_yields_insufficient(self):
        self.assertEqual(self.run_("STALE")["label"], "INSUFFICIENT_DATA")

    def test_unknown_freshness_is_not_promoted_to_favorable(self):
        self.assertNotEqual(self.run_("UNKNOWN")["label"], "FAVORABLE")

    def test_the_freshness_factor_is_still_reported(self):
        out = self.run_("AGING")
        factor = [f for f in out["factors"]
                  if f["factor"] == "data_freshness"][0]
        self.assertEqual(factor["state"], "CAUTION")

    def test_absent_freshness_is_not_promoted_to_favorable(self):
        out = holding_context(
            dividend=ACTIVE, fundamentals=fund(), earnings_trends=EPS_UP,
            splits=None, days_to_earnings=None, freshness=None)
        self.assertEqual(out["label"], "NEUTRAL")
        self.assertIn("unknown age", out["why"])

    def test_favorable_requires_current_not_merely_not_aging(self):
        """An allowlist: a freshness label nobody has seen before must
        not reach FAVORABLE by default."""
        out = holding_context(
            dividend=ACTIVE, fundamentals=fund(), earnings_trends=EPS_UP,
            splits=None, days_to_earnings=None, freshness="SOME_NEW_LABEL")
        self.assertEqual(out["label"], "NEUTRAL")
