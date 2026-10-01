import os, sys, unittest
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.company.models import CompanyProfile, SplitEvent
from agent.company import peers as PE
from agent.company import compare as C
from agent.company.classification import sector_for_sic


def prof(sym, sic, cap, active=True, industry=None):
    return CompanyProfile(sym, sic=sic, sector=sector_for_sic(sic),
                          industry=industry or (f"SIC-{sic[:3]}" if sic else None),
                          market_cap=cap, active=active)


GIS = prof("GIS", "2040", 40e9)
CANDS = [prof("CPB", "2000", 12e9), prof("KHC", "2030", 35e9),
         prof("AAPL", "3571", 3000e9), prof("BANK", "6022", 40e9), prof("XOM", "2911", 400e9),
         prof("DEAD", "2040", 5e9, active=False),
         prof("NOINFO", None, 10e9),
         prof("TINY", "2040", 1e8)]


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.ps = PE.select(GIS, CANDS)

    def peers(self):
        return {p["symbol"]: p["reasons"] for p in self.ps.peers}

    def rejected(self):
        return {r["symbol"]: r["reasons"] for r in self.ps.rejected}

    def test_food_companies_are_peers_with_structured_reasons(self):
        self.assertIn("SAME_SIC_MAJOR", self.peers()["KHC"])
        self.assertIn("SIMILAR_MARKET_CAP", self.peers()["KHC"])
        self.assertIn("CPB", self.peers())

    def test_unrelated_company_is_rejected_with_reason(self):
        self.assertIn("UNRELATED_BUSINESS", self.rejected()["BANK"])
        self.assertEqual(self.rejected()["AAPL"], ["DIFFERENT_INDUSTRY"])
        for sym in ("AAPL", "BANK", "XOM"):
            self.assertNotIn(sym, self.peers())

    def test_same_sector_alone_is_not_enough(self):
        other = prof("MFG", "3100", 40e9)      # manufacturing, other major group
        ps = PE.select(GIS, [other])
        self.assertEqual(ps.peers, [])
        self.assertIn("DIFFERENT_INDUSTRY", ps.rejected[0]["reasons"])

    def test_market_cap_similarity_alone_never_makes_a_peer(self):
        twin = prof("TWIN", "7372", 40e9)       # same cap, software
        self.assertEqual(PE.select(GIS, [twin]).peers, [])

    def test_inactive_insufficient_and_outlier(self):
        r = self.rejected()
        self.assertEqual(r["DEAD"], ["INACTIVE_SECURITY"])
        self.assertEqual(r["NOINFO"], ["INSUFFICIENT_DATA"])
        self.assertEqual(r["TINY"], ["MARKET_CAP_OUTLIER"])

    def test_subject_without_classification_selects_nothing(self):
        ps = PE.select(CompanyProfile("ZZZ"), CANDS)
        self.assertEqual(ps.peers, [])
        self.assertIn("INSUFFICIENT_SUBJECT_CLASSIFICATION", ps.warnings)

    def test_every_peer_has_a_reason(self):
        self.assertTrue(all(p["reasons"] for p in self.ps.peers))


class TestLLMCannotCreatePeers(unittest.TestCase):
    def test_invented_peer_is_rejected_and_not_persisted(self):
        ps = PE.select(GIS, CANDS)
        before = {p["symbol"] for p in ps.peers}
        PE.refine_with_llm(ps, [{"symbol": "TSLA", "note": "sells food?"},
                                {"symbol": "KHC", "note": "packaged foods"}])
        self.assertEqual({p["symbol"] for p in ps.peers}, before)
        self.assertEqual(ps.llm_rejected[0]["reason"],
                         "LLM_INVENTED_PEER_REJECTED")
        self.assertEqual(ps.llm_notes["KHC"], "packaged foods")

    def test_validation_blocks_a_hand_inserted_peer(self):
        ps = PE.PeerSet("GIS", peers=[{"symbol": "TSLA", "reasons": []}])
        with self.assertRaises(PE.UnstructuredPeer):
            PE.validate(ps)

    def test_validation_blocks_sector_only_or_cap_only_reasons(self):
        for reasons in (["SAME_SECTOR"], ["SIMILAR_MARKET_CAP"],
                        ["LLM_SUGGESTED"]):
            ps = PE.PeerSet("GIS", peers=[{"symbol": "X", "reasons": reasons}])
            with self.assertRaises(PE.UnstructuredPeer):
                PE.validate(ps)


class TestComparison(unittest.TestCase):
    def m(self, v, end="2026-06-30"):
        return {"value": v, "period_end": end}

    def test_median_range_percentile(self):
        r = C.compare_metric("gross_margin", self.m(0.35),
                             {"A": self.m(0.30), "B": self.m(0.40),
                              "C": self.m(0.20)})
        self.assertEqual(r["peer_median"], 0.30)
        self.assertEqual((r["peer_min"], r["peer_max"]), (0.20, 0.40))
        self.assertEqual(r["subject_percentile"], 66.7)
        self.assertIn("not a ranking", r["note"])

    def test_misaligned_peer_is_excluded_as_period_mismatch(self):
        r = C.compare_metric("net_margin", self.m(0.1, "2026-05-31"),
                             {"A": self.m(0.2, "2026-03-31"),
                              "B": self.m(0.3, "2026-05-30")})
        self.assertEqual(r["peers"], {"B": 0.3})
        self.assertEqual(r["excluded"][0]["reason"], "PERIOD_MISMATCH")

    def test_all_misaligned_is_unknown_not_a_blend(self):
        r = C.compare_metric("x", self.m(1, "2026-05-31"),
                             {"A": self.m(2, "2025-03-31")})
        self.assertEqual(r["status"], "UNKNOWN")

    def test_unknown_subject(self):
        self.assertEqual(C.compare_metric("x", None, {})["status"], "UNKNOWN")


class TestPerformance(unittest.TestCase):
    TODAY = date(2026, 10, 1)

    def series(self):
        # raw closes: 100 until a 10:1 forward split on 2026-06-10, then 10
        rows = []
        d = date(2025, 9, 1)
        from datetime import timedelta
        while d <= self.TODAY:
            rows.append({"date": d.isoformat(),
                         "close": 100.0 if d < date(2026, 6, 10) else 10.0})
            d += timedelta(days=1)
        return rows

    def test_raw_series_without_adjustment_shows_a_false_crash(self):
        p = C.performance(self.series(), self.TODAY, adjustment="split")
        self.assertAlmostEqual(p["1Y"], -0.9)           # the false spike

    def test_raw_series_is_adjusted_using_split_history(self):
        p = C.performance(self.series(), self.TODAY,
                          [SplitEvent("2026-06-10", 10, 1)], adjustment="raw")
        self.assertAlmostEqual(p["1Y"], 0.0)

    def test_horizons_without_enough_history_are_unknown(self):
        p = C.performance(self.series(), self.TODAY, adjustment="split")
        self.assertEqual(p["5Y"], "UNKNOWN")

    def test_bad_adjustment_value_is_refused(self):
        with self.assertRaises(ValueError):
            C.performance(self.series(), self.TODAY, adjustment="auto")


if __name__ == "__main__":
    unittest.main()
