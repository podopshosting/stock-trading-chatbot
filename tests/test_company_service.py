import os, sys, unittest
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.company.models import (ActionType, CorporateAction, FinancialPeriod,
                                  Provenance)
from agent.company.service import CompanyService
from agent.company.store import (HistoryConflict, InMemoryCompanyStore)
from agent.company import peers as PE

TODAY = date(2026, 10, 1)
PROV = Provenance("alpaca", "x", "t")


def div(ex, amt, special=False):
    return CorporateAction(ActionType.CASH_DIVIDEND, "GIS", ex_date=ex,
                           amount=amt, special=special, provenance=PROV)


class FakeActions:
    def __init__(self, acts=None, fail=False):
        self.acts, self.fail, self.calls = acts or [], fail, 0

    def fetch(self, symbol, start, end, types=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return [a for a in self.acts if a.symbol == symbol or True]


def fp(c, v, s, e, fpd="FY", fy=2026):
    return FinancialPeriod(c, v, "USD", s, e, fy, fpd, "10-K", e, "a")


class FakeFacts:
    def __init__(self, data=None, fail=False):
        self.data, self.fail = data or {}, fail

    def fetch(self, symbol):
        if self.fail:
            raise RuntimeError("sec down")
        return self.data.get(symbol, {})


def svc(acts=None, facts=None, subs=None, universe=()):
    subs = subs or {"GIS": {"name": "General Mills", "sic": "2040",
                            "sicDescription": "Grain Mill Products", "cik": 40704}}
    store = InMemoryCompanyStore()
    s = CompanyService(store, acts or FakeActions(), facts or FakeFacts(),
                       submissions=lambda sym: subs.get(sym, {}),
                       price=lambda sym: (50.0, "2026-09-30"),
                       today=lambda: TODAY, clock=lambda: 1_790_000_000.0,
                       universe=universe)
    return s, store


QUARTERLY = [div(f"2026-{m:02d}-10", 0.61) for m in (1, 4, 7)] + \
            [div("2025-10-10", 0.60), div("2025-07-10", 0.60),
             div("2025-04-10", 0.60)]


class TestDividendsEndToEnd(unittest.TestCase):
    def test_payer_is_yes_with_yield_basis(self):
        s, _ = svc(FakeActions(QUARTERLY))
        d = s.dividends("GIS")
        self.assertEqual(d["dividend_stock"], "YES")
        self.assertEqual(d["status"], "ACTIVE")
        self.assertIn("price 50.0", d["yield_price_basis"])

    def test_empty_provider_result_is_a_windowed_no_dividend(self):
        s, _ = svc(FakeActions([]))
        d = s.dividends("GIS")
        self.assertEqual(d["dividend_stock"], "NO")
        self.assertIn("10-year window", d["reason"])

    def test_provider_outage_is_unknown_not_no_dividend(self):
        s, _ = svc(FakeActions(fail=True))
        d = s.dividends("GIS")
        self.assertEqual(d["status"], "UNKNOWN")
        self.assertIsNone(d["pays_dividend"])

    def test_special_dividend_is_labelled(self):
        s, _ = svc(FakeActions(QUARTERLY + [div("2026-02-01", 3.0, True)]))
        d = s.dividends("GIS")
        self.assertEqual(d["special_dividends"], ["2026-02-01"])
        kinds = {h["kind"] for h in d["history"]}
        self.assertEqual(kinds, {"REGULAR", "SPECIAL"})

    def test_refetch_is_served_from_the_store_within_ttl(self):
        a = FakeActions(QUARTERLY)
        s, _ = svc(a)
        s.dividends("GIS"); s.dividends("GIS")
        self.assertEqual(a.calls, 1)


class TestSplitsEndToEnd(unittest.TestCase):
    def test_timeline_words_not_ambiguous_ratios(self):
        acts = [CorporateAction(ActionType.FORWARD_SPLIT, "X", ex_date="2024-06-10",
                                ratio_new=10, ratio_old=1, provenance=PROV),
                CorporateAction(ActionType.REVERSE_SPLIT, "X", ex_date="2025-03-12",
                                ratio_new=1, ratio_old=10, provenance=PROV)]
        s, _ = svc(FakeActions(acts))
        t = s.splits("GIS")["timeline"]
        self.assertEqual(t[0]["label"], "1-for-10 reverse split")
        self.assertEqual(t[1]["label"], "10-for-1 forward split")


class TestStoreImmutability(unittest.TestCase):
    def test_same_fact_rewrite_is_a_noop(self):
        st = InMemoryCompanyStore()
        self.assertTrue(st.put_fact("GIS", "DIVIDEND", "a", {"v": 1, "retrieved_at": "1"}))
        self.assertFalse(st.put_fact("GIS", "DIVIDEND", "a", {"v": 1, "retrieved_at": "2"}))

    def test_changed_history_is_refused_not_overwritten(self):
        st = InMemoryCompanyStore()
        st.put_fact("GIS", "DIVIDEND", "a", {"v": 1})
        with self.assertRaises(HistoryConflict):
            st.put_fact("GIS", "DIVIDEND", "a", {"v": 2})
        self.assertEqual(st.facts("GIS", "DIVIDEND"), [{"v": 1}])

    def test_snapshots_are_versioned_not_replaced(self):
        st = InMemoryCompanyStore()
        st.put_snapshot("GIS", "HOLDING", {"label": "A"}, "t1")
        st.put_snapshot("GIS", "HOLDING", {"label": "B"}, "t2")
        self.assertEqual(len(st.versions("GIS", "HOLDING")), 2)
        self.assertEqual(st.latest("GIS", "HOLDING")["payload"]["label"], "B")

    def test_unstructured_peer_set_cannot_be_persisted(self):
        st = InMemoryCompanyStore()
        bad = PE.PeerSet("GIS", peers=[{"symbol": "TSLA", "reasons": []}]).as_dict()
        with self.assertRaises(PE.UnstructuredPeer):
            st.put_snapshot("GIS", "PEERSET", bad, "t")
        self.assertIsNone(st.latest("GIS", "PEERSET"))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            InMemoryCompanyStore().put_fact("GIS", "WHATEVER", "a", {})


class TestFundamentalsAndHolding(unittest.TestCase):
    def facts(self, end="2026-05-31"):
        s = f"{int(end[:4]) - 1}-06-01"
        return {"GIS": {
            "revenue": [fp("revenue", 120, s, end), fp("revenue", 100,
                        f"{int(end[:4]) - 2}-06-01", f"{int(end[:4]) - 1}-05-31")],
            "net_income": [fp("net_income", 12, s, end)],
            "operating_cash_flow": [fp("operating_cash_flow", 20, s, end)],
            "capex": [fp("capex", 5, s, end)]}}

    def test_fundamentals_report_freshness_from_the_period(self):
        s, _ = svc(facts=FakeFacts(self.facts()))
        self.assertEqual(s.fundamentals("GIS")["freshness"], "CURRENT")

    def test_stale_statement_is_labelled_stale_and_blocks_holding_context(self):
        s, _ = svc(FakeActions(QUARTERLY), FakeFacts(self.facts("2022-05-31")))
        self.assertEqual(s.fundamentals("GIS")["freshness"], "STALE")
        self.assertEqual(s.holding_context("GIS")["label"], "INSUFFICIENT_DATA")

    def test_sec_outage_degrades_the_section_only(self):
        s, _ = svc(FakeActions(QUARTERLY), FakeFacts(fail=True))
        self.assertEqual(s.fundamentals("GIS")["status"], "UNKNOWN")
        self.assertEqual(s.dividends("GIS")["status"], "ACTIVE")

    def test_holding_context_is_never_an_instruction(self):
        s, _ = svc(FakeActions(QUARTERLY), FakeFacts(self.facts()))
        h = s.holding_context("GIS")
        self.assertFalse(h["is_execution_instruction"])
        self.assertEqual(h["overnight_execution"], "DISABLED")

    def test_earnings_never_invents_estimates(self):
        s, _ = svc(facts=FakeFacts(self.facts()))
        e = s.earnings("GIS")
        self.assertIn("UNAVAILABLE", e["estimates"])
        self.assertTrue(all(r["eps_estimate"] is None for r in e["records"]))


class TestPeersEndToEnd(unittest.TestCase):
    SUBS = {"GIS": {"name": "General Mills", "sic": "2040",
                    "sicDescription": "Grain Mill Products", "cik": 1},
            "CPB": {"name": "Campbell", "sic": "2000",
                    "sicDescription": "Food and Kindred Products", "cik": 2},
            "AAPL": {"name": "Apple", "sic": "3571",
                     "sicDescription": "Electronic Computers", "cik": 3},
            "JPM": {"name": "JPM", "sic": "6022",
                    "sicDescription": "State Commercial Banks", "cik": 4}}

    def test_selects_food_peers_and_rejects_others(self):
        s, st = svc(subs=self.SUBS, universe=["CPB", "AAPL", "JPM"])
        out = s.peers("GIS")
        self.assertEqual([p["symbol"] for p in out["peers"]], ["CPB"])
        rej = {r["symbol"]: r["reasons"] for r in out["rejected"]}
        self.assertIn("UNRELATED_BUSINESS", rej["JPM"])
        self.assertIsNotNone(st.latest("GIS", "PEERSET"))

    def test_candidate_profiles_are_cached(self):
        calls = []
        subs = dict(self.SUBS)
        s, _ = svc(subs=subs, universe=["CPB"])
        orig = s._submissions
        s._submissions = lambda sym: (calls.append(sym), orig(sym))[1]
        s.peers("GIS"); s.peers("GIS")
        self.assertEqual(calls.count("CPB"), 2)   # lite + full on the first pass only


if __name__ == "__main__":
    unittest.main()
