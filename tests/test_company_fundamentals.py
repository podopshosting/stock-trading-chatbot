import os, sys, unittest
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.company.models import FinancialPeriod
from agent.company import fundamentals as F
from agent.company.providers.sec_companyfacts import normalise, SECCompanyFacts
from agent.providers.base import DataUnavailable


def P(concept, value, start, end, fy=2026, fp="Q1", filed=None, form="10-Q",
      unit="USD"):
    return FinancialPeriod(concept, value, unit, start, end, fy, fp, form,
                           filed or end, "acc")


QS = [("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"),
      ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]


def quarters(vals, concept="revenue"):
    return [P(concept, v, s, e) for v, (s, e) in zip(vals, QS)]


class TestSelection(unittest.TestCase):
    def test_ytd_is_never_a_quarter(self):
        ytd = P("revenue", 900, "2026-01-01", "2026-09-30")   # 9 months
        self.assertIsNone(F.kind(ytd))
        self.assertEqual(F.collapse([ytd], "Q"), [])

    def test_restatement_wins_by_filing_date(self):
        a = P("revenue", 100, "2026-04-01", "2026-06-30", filed="2026-08-01")
        b = P("revenue", 105, "2026-04-01", "2026-06-30", filed="2026-11-01")
        self.assertEqual(F.collapse([a, b], "Q")[0].value, 105)

    def test_ttm_from_four_sequential_quarters(self):
        t = F.ttm(quarters([10, 20, 30, 40]))
        self.assertEqual(t["value"], 100)
        self.assertEqual(len(t["contributing"]), 4)

    def test_ttm_rejects_three_quarters(self):
        t = F.ttm(quarters([10, 20, 30, 40])[:3])
        self.assertEqual(t["value"], F.UNKNOWN)

    def test_ttm_not_built_from_three_quarters_plus_annual(self):
        annual = P("revenue", 400, "2025-07-01", "2026-06-30", fp="FY",
                   form="10-K")
        t = F.ttm(quarters([10, 20, 30, 40])[:3] + [annual])
        self.assertEqual(t["value"], F.UNKNOWN)

    def test_ttm_rejects_a_missing_quarter(self):
        q = quarters([10, 20, 30, 40])
        del q[1]
        q.insert(0, P("revenue", 5, "2025-01-01", "2025-03-31"))
        t = F.ttm(q)
        self.assertEqual(t["value"], F.UNKNOWN)
        self.assertIn("missing quarter", t["reason"])

    def test_ttm_duplicates_do_not_double_count(self):
        q = quarters([10, 20, 30, 40])
        t = F.ttm(q + [q[3]])
        self.assertEqual(t["value"], 100)


class TestFreshness(unittest.TestCase):
    TODAY = date(2026, 10, 1)

    def test_bands(self):
        self.assertEqual(F.freshness("2026-06-30", self.TODAY), "CURRENT")
        self.assertEqual(F.freshness("2026-02-28", self.TODAY), "AGING")
        self.assertEqual(F.freshness("2023-06-30", self.TODAY), "STALE")
        self.assertEqual(F.freshness(None, self.TODAY), "UNKNOWN")

    def test_a_freshly_retrieved_old_statement_is_stale(self):
        old = {"revenue": [P("revenue", 1, "2022-07-01", "2023-06-30",
                             fp="FY", form="10-K", filed="2026-10-01")]}
        s = F.summarise(old, self.TODAY)
        self.assertEqual(s["freshness"], "STALE")


class TestAlignment(unittest.TestCase):
    def test_same_label_different_calendar_is_a_mismatch(self):
        gis_q1 = P("revenue", 1, "2025-06-01", "2025-08-31", fp="Q1")
        peer_q1 = P("revenue", 1, "2025-01-01", "2025-03-31", fp="Q1")
        self.assertEqual(F.compare(gis_q1, peer_q1)["status"],
                         F.PERIOD_MISMATCH)

    def test_nearby_period_ends_align(self):
        a = P("revenue", 1, "2025-07-01", "2025-09-30")
        b = P("revenue", 1, "2025-07-05", "2025-10-04")
        self.assertEqual(F.compare(a, b)["status"], "ALIGNED")

    def test_quarter_never_aligns_with_year(self):
        q = P("revenue", 1, "2025-07-01", "2025-09-30")
        y = P("revenue", 1, "2024-10-01", "2025-09-30", fp="FY")
        self.assertEqual(F.compare(q, y)["status"], F.PERIOD_MISMATCH)


class TestDerived(unittest.TestCase):
    def test_margin(self):
        rev = P("revenue", 200, "2025-07-01", "2026-06-30", fp="FY")
        gp = P("gross_profit", 50, "2025-07-01", "2026-06-30", fp="FY")
        self.assertEqual(F.ratio(gp, rev, "gross_margin")["value"], 0.25)

    def test_zero_or_missing_denominator_is_unknown(self):
        z = P("revenue", 0, "2025-07-01", "2026-06-30", fp="FY")
        n = P("net_income", 5, "2025-07-01", "2026-06-30", fp="FY")
        self.assertEqual(F.ratio(n, z, "m")["value"], F.UNKNOWN)
        self.assertEqual(F.ratio(n, None, "m")["value"], F.UNKNOWN)

    def test_different_period_terms_are_unknown(self):
        a = P("net_income", 5, "2025-07-01", "2026-06-30", fp="FY")
        b = P("revenue", 50, "2024-07-01", "2025-06-30", fp="FY")
        self.assertIn("different periods", F.ratio(a, b, "m")["reason"])

    def test_growth_needs_the_prior_year_period(self):
        cur = P("revenue", 120, "2025-07-01", "2026-06-30", fp="FY")
        old = P("revenue", 100, "2024-07-01", "2025-06-30", fp="FY")
        g = F.yoy_growth([cur, old], "FY", "revenue_growth")
        self.assertAlmostEqual(g["value"], 0.2)
        gap = P("revenue", 100, "2022-07-01", "2023-06-30", fp="FY")
        self.assertEqual(F.yoy_growth([cur, gap], "FY", "g")["value"],
                         F.UNKNOWN)

    def test_summary_fcf_and_unknowns(self):
        facts = {
            "revenue": [P("revenue", 200, "2025-07-01", "2026-06-30", fp="FY")],
            "operating_cash_flow": [P("operating_cash_flow", 60, "2025-07-01",
                                      "2026-06-30", fp="FY")],
            "capex": [P("capex", 15, "2025-07-01", "2026-06-30", fp="FY")]}
        s = F.summarise(facts, date(2026, 10, 1))
        self.assertEqual(s["cash_flow"]["free_cash_flow"]["value"], 45)
        self.assertEqual(s["balance"]["equity"], F.UNKNOWN)
        dte = [d for d in s["derived"] if d["metric"] == "debt_to_equity"][0]
        self.assertEqual(dte["value"], F.UNKNOWN)


class TestSecProvider(unittest.TestCase):
    PAYLOAD = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"start": "2025-06-01", "end": "2026-05-31", "val": 19000,
             "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-07-20",
             "accn": "a1"}]}},
        "SalesRevenueNet": {"units": {"USD": [
            {"start": "2020-06-01", "end": "2021-05-31", "val": 1,
             "fy": 2021, "fp": "FY", "form": "10-K", "filed": "2021-07-20"}]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            {"start": "2025-06-01", "end": "2026-05-31", "val": 4.2,
             "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-07-20"}]}}}}}

    def test_normalises_with_explicit_periods(self):
        out = normalise(self.PAYLOAD, "t", "src")
        r = out["revenue"][0]
        self.assertEqual((r.period_start, r.period_end, r.fiscal_period),
                         ("2025-06-01", "2026-05-31", "FY"))
        self.assertEqual(out["eps_diluted"][0].unit, "USD/shares")

    def test_concepts_are_not_spliced(self):
        out = normalise(self.PAYLOAD, "t", "src")
        self.assertEqual(len(out["revenue"]), 1)   # older SalesRevenueNet ignored

    def test_a_small_side_concept_does_not_beat_the_real_revenue_series(self):
        """Live GIS: 'Revenues' held one old 2.0B item while the real
        revenue sat under RevenueFromContractWithCustomer..."""
        payload = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [
                {"start": "2023-05-29", "end": "2024-05-26", "val": 2037800000,
                 "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-06-26"}]}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                {"start": "2025-05-26", "end": "2026-05-31", "val": 19000000000,
                 "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-07-01"}]}}}}}
        out = normalise(payload, "t", "s")
        self.assertEqual(out["revenue"][0].value, 19000000000)

    def test_proxy_statement_facts_are_ignored(self):
        """Live GIS: a DEF 14A net income filed later replaced the 10-K's."""
        payload = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [
            {"start": "2025-05-26", "end": "2026-05-31", "val": -2000000000,
             "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-07-01"},
            {"start": "2025-05-26", "end": "2026-05-31", "val": -85000000,
             "fy": None, "fp": None, "form": "DEF 14A", "filed": "2026-08-13"}]}}}}}
        out = normalise(payload, "t", "s")
        self.assertEqual([p.value for p in out["net_income"]], [-2000000000])

    def test_quarter_reported_in_a_10k_does_not_keep_the_FY_label(self):
        payload = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {
            "USD/shares": [{"start": "2025-12-01", "end": "2026-02-22", "val": 0.56,
             "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-07-01"}]}}}}}
        out = normalise(payload, "t", "s")
        self.assertIsNone(out["eps_diluted"][0].fiscal_period)

    def test_requires_contact_user_agent(self):
        with self.assertRaises(ValueError):
            SECCompanyFacts("agent", lambda s: (1, "x"))

    def test_http_failure_is_unavailable(self):
        class H:
            def get(self, *a, **k):
                class R: status_code = 404
                return R()
        with self.assertRaises(DataUnavailable):
            SECCompanyFacts("t (a@b.co)", lambda s: (1, "x"), http=H(),
                            sleep=lambda s: None).fetch("X")


if __name__ == "__main__":
    unittest.main()
