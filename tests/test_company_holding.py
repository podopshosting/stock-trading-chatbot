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
