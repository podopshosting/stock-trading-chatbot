import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.company.models import EarningsRecord, FinancialPeriod, Provenance
from agent.company import earnings as E

PROV = Provenance("alphavantage", "EARNINGS", "t")


def fp(c, v, s, e):
    return FinancialPeriod(c, v, "USD", s, e, 2026, "Q1", "10-Q", e, "a")


class TestRecords(unittest.TestCase):
    AV = {"quarterlyEarnings": [
        {"fiscalDateEnding": "2026-06-30", "reportedDate": "2026-07-30",
         "reportedEPS": "1.50", "estimatedEPS": "1.40",
         "reportTime": "pre-market"},
        {"fiscalDateEnding": "2026-03-31", "reportedEPS": "None",
         "estimatedEPS": "1.00"}]}

    def test_actual_and_estimate_are_not_swapped(self):
        r = E.from_alpha_vantage(self.AV, PROV)[-1]
        self.assertEqual(r.eps_actual, 1.50)
        self.assertEqual(r.eps_estimate, 1.40)
        self.assertAlmostEqual(r.eps_surprise, 0.10)

    def test_surprise_sign_follows_actual_minus_estimate(self):
        r = EarningsRecord("2026-06-30", eps_actual=1.0, eps_estimate=1.2)
        self.assertLess(r.eps_surprise, 0)

    def test_string_none_is_missing_not_zero(self):
        r = E.from_alpha_vantage(self.AV, PROV)[0]
        self.assertIsNone(r.eps_actual)
        self.assertIsNone(r.eps_surprise)

    def test_timing_mapped_and_unknown_kept(self):
        recs = E.from_alpha_vantage(self.AV, PROV)
        self.assertEqual(recs[-1].report_timing, "BMO")
        self.assertEqual(recs[0].report_timing, "UNKNOWN")

    def test_sec_gives_actuals_and_never_an_estimate(self):
        facts = {"eps_diluted": [fp("eps_diluted", 0.9, "2026-04-01", "2026-06-30")],
                 "revenue": [fp("revenue", 100, "2026-04-01", "2026-06-30")]}
        [r] = E.from_sec(facts)
        self.assertEqual(r.eps_actual, 0.9)
        self.assertIsNone(r.eps_estimate)
        self.assertIsNone(r.eps_surprise)
        self.assertIsNone(r.report_date)


class TestTrends(unittest.TestCase):
    def recs(self, n, est=True):
        out = []
        for i in range(n):
            y, m = 2024 + i // 4, (i % 4) * 3 + 3
            end = f"{y}-{m:02d}-28"
            out.append(EarningsRecord(end, eps_actual=1.0 + 0.1 * i,
                                      eps_estimate=(1.0 + 0.1 * i - 0.05)
                                      if est else None,
                                      revenue_actual=100.0 + i))
        return out

    def test_one_quarter_is_not_a_trend(self):
        t = E.trends(self.recs(1))
        self.assertEqual(t["eps_beat_miss"], E.UNKNOWN)
        self.assertEqual(t["eps_yoy"]["value"], E.UNKNOWN)

    def test_beat_sequence_needs_four_estimates(self):
        self.assertEqual(E.trends(self.recs(3))["eps_beat_miss"], E.UNKNOWN)
        t = E.trends(self.recs(5))
        self.assertEqual(t["eps_beat_miss"], ["BEAT"] * 4)

    def test_no_estimates_means_unknown_with_reason(self):
        t = E.trends(self.recs(6, est=False))
        self.assertEqual(t["eps_beat_miss"], E.UNKNOWN)
        self.assertIn("never inferred", t["estimates_note"])

    def test_yoy_uses_the_year_ago_quarter(self):
        t = E.trends(self.recs(5))
        self.assertAlmostEqual(t["eps_yoy"]["value"], 1.4 / 1.0 - 1)

    def test_yoy_unknown_when_prior_year_nonpositive(self):
        r = [EarningsRecord("2025-06-28", eps_actual=-0.5),
             EarningsRecord("2026-06-28", eps_actual=0.5)]
        self.assertEqual(E.trends(r)["eps_yoy"]["value"], E.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
