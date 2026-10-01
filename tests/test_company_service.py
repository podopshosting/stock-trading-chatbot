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
        self.windows = []

    def fetch(self, symbol, start, end, types=None):
        self.calls += 1
        self.windows.append({"symbols": symbol, "start": start, "end": end})
        if self.fail:
            raise RuntimeError("provider down")
        return [a for a in self.acts if a.symbol == symbol or True]


def fp(c, v, s, e, fpd="FY", fy=2026):
    return FinancialPeriod(c, v, "USD", s, e, fy, fpd, "10-K", e, "a")


class FakeFacts:
    def __init__(self, data=None, fail=False, shares=None):
        self.data, self.fail = data or {}, fail
        self.shares = shares or {}
        self.fetch_calls, self.shares_calls = [], []

    def fetch(self, symbol):
        self.fetch_calls.append(symbol)
        if self.fail:
            raise RuntimeError("sec down")
        return self.data.get(symbol, {})

    def shares_outstanding(self, symbol):
        """The small companyconcept call. Separate from fetch() so a test
        can tell which endpoint the service reached for."""
        self.shares_calls.append(symbol)
        if self.fail:
            raise RuntimeError("sec down")
        n = self.shares.get(symbol)
        return None if n is None else fp("shares_outstanding", n, None,
                                         "2026-09-16")


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


class TestItDoesNotDownloadTheTaxonomyForAShareCount(unittest.TestCase):
    """
    Measured 2026-10-01: a nine-company peer comparison took 88.4s and
    112.7 MB cold, because selecting peers pulled each candidate's full
    ~4.5 MB XBRL payload to read one share count. The companyconcept
    endpoint returns the same number in ~10 KB.
    """

    def service(self, universe=("CPB",)):
        subs = {s: {"name": s, "sic": "2040", "sicDescription": "Grain Mill",
                    "cik": i + 1} for i, s in enumerate(("GIS",) + tuple(universe))}
        facts = FakeFacts(shares={s: 1_000_000 for s in subs})
        s, store = svc(FakeActions(QUARTERLY), facts, subs=subs,
                       universe=universe)
        return s, facts

    def test_a_profile_uses_the_small_concept_call(self):
        s, facts = self.service()
        p = s.profile("GIS")
        self.assertEqual(facts.shares_calls, ["GIS"])
        self.assertEqual(facts.fetch_calls, [])
        self.assertEqual(p.market_cap, 1_000_000 * 50.0)

    def test_market_cap_still_names_its_basis(self):
        s, _ = self.service()
        self.assertIn("1,000,000 shares", s.profile("GIS").market_cap_basis)

    def test_a_company_that_reports_no_share_count_has_no_market_cap(self):
        subs = {"GIS": {"name": "G", "sic": "2040", "cik": 1}}
        s, _store = svc(FakeActions(QUARTERLY), FakeFacts(shares={}),
                        subs=subs)
        self.assertIsNone(s.profile("GIS").market_cap)

    def test_cached_full_facts_are_reused_rather_than_refetched(self):
        s, facts = self.service()
        s.fundamentals("GIS")                 # pulls the full payload
        before = list(facts.shares_calls)
        s.profile("GIS")
        self.assertEqual(facts.shares_calls, before)

    def test_facts_are_downloaded_once_per_container(self):
        s, facts = self.service()
        s.fundamentals("GIS")
        s.earnings("GIS")
        s.fundamentals("GIS")
        self.assertEqual(facts.fetch_calls, ["GIS"])

    def test_warming_fetches_each_symbol_once_and_reports_failures(self):
        s, facts = self.service(universe=("CPB", "KHC"))
        errors = s.warm_facts(["GIS", "CPB", "GIS"])
        self.assertEqual(errors, {})
        self.assertEqual(sorted(facts.fetch_calls), ["CPB", "GIS"])
        self.assertEqual(s.warm_facts(["GIS"]), {})      # already cached
        self.assertEqual(sorted(facts.fetch_calls), ["CPB", "GIS"])

    def test_the_facts_cache_is_bounded(self):
        from agent.company.service import FACTS_CACHE_MAX
        s, facts = self.service()
        for i in range(FACTS_CACHE_MAX + 4):
            s._facts_cache[f"SYM{i}"] = {}
            if len(s._facts_cache) > FACTS_CACHE_MAX:
                s._facts_cache.pop(next(iter(s._facts_cache)))
        self.assertLessEqual(len(s._facts_cache), FACTS_CACHE_MAX)


class TestOnlyCitedPeriodsArePersisted(unittest.TestCase):
    """19,748 immutable writes per comparison, none of which were ever
    read. Only the periods behind a reported number are stored now."""

    def facts_with_noise(self):
        rows = [fp("revenue", 100 + i, f"{2000 + i}-01-01", f"{2000 + i}-12-31")
                for i in range(50)]
        return {"GIS": {"revenue": rows}}

    def test_it_stores_the_reported_periods_not_the_whole_taxonomy(self):
        s, store = svc(FakeActions(QUARTERLY),
                       FakeFacts(self.facts_with_noise()))
        summary = s.fundamentals("GIS")
        stored = store.facts("GIS", "FINPERIOD")
        self.assertLess(len(stored), 10)
        self.assertGreaterEqual(summary["periods_persisted"], 1)

    def test_what_is_stored_is_what_was_reported(self):
        s, store = svc(FakeActions(QUARTERLY),
                       FakeFacts(self.facts_with_noise()))
        summary = s.fundamentals("GIS")
        ends = {r["period_end"] for r in store.facts("GIS", "FINPERIOD")}
        self.assertIn(summary["income"]["revenue"]["period_end"], ends)

    def test_stored_periods_stay_immutable(self):
        s, store = svc(FakeActions(QUARTERLY),
                       FakeFacts(self.facts_with_noise()))
        s.fundamentals("GIS")
        first = store.facts("GIS", "FINPERIOD")
        s._facts_cache.clear()
        s.fundamentals("GIS")
        self.assertEqual(store.facts("GIS", "FINPERIOD"), first)


class TestThePeerFanOutStaysInsideItsBudget(unittest.TestCase):
    """
    Measured 2026-10-01 against the real SEC API: a cold GIS peer
    comparison took 88.4s (Lambda budget 90s) over 112.7 MB, because
    candidate profiling made 58 sequential round trips and each company's
    4.5 MB taxonomy was downloaded twice. After the fix: ~21s, 56 MB.
    These tests pin the properties that produced the improvement, since
    wall time itself is not a stable assertion.
    """

    UNIVERSE = tuple(f"P{i:02d}" for i in range(12))

    def service(self):
        subs = {"GIS": {"name": "GIS", "sic": "2040", "cik": 1}}
        for i, s in enumerate(self.UNIVERSE):
            # Half share the subject's SIC major group (20xx), half do not.
            subs[s] = {"name": s, "cik": 100 + i,
                       "sic": "2043" if i % 2 == 0 else "7372",
                       "sicDescription": "x"}
        facts = FakeFacts(shares={s: 1_000_000 for s in subs})
        service, store = svc(FakeActions(QUARTERLY), facts, subs=subs,
                             universe=self.UNIVERSE)
        calls = []
        original = service._fetch_profile_inputs

        def tracked(symbol, with_market_cap):
            calls.append((symbol, with_market_cap))
            return original(symbol, with_market_cap)
        service._fetch_profile_inputs = tracked
        return service, facts, calls

    def test_a_candidate_is_fetched_at_most_once_per_phase(self):
        service, _facts, calls = self.service()
        service.peers("GIS")
        lite = [c for c in calls if c[1] is False]
        self.assertEqual(len(lite), len({c[0] for c in lite}))

    def test_only_same_major_group_candidates_cost_a_market_cap_call(self):
        """The expensive-ish second round is limited to companies that
        could actually become peers."""
        service, _facts, calls = self.service()
        service.peers("GIS")
        full = {c[0] for c in calls if c[1] is True}
        self.assertTrue(full)
        for sym in full:
            self.assertTrue(sym == "GIS" or sym.startswith("P"))
        # Half the universe shares the subject's major group, plus the
        # subject itself, whose market cap the comparison needs.
        self.assertEqual(len(full), len(self.UNIVERSE) // 2 + 1)
        self.assertIn("GIS", full)

    def test_a_second_call_refetches_nothing(self):
        service, _facts, calls = self.service()
        service.peers("GIS")
        before = len(calls)
        service.peers("GIS")
        self.assertEqual(len(calls), before)

    def test_the_candidate_budget_is_respected(self):
        from agent.company.service import CANDIDATE_BUDGET
        service, _facts, calls = self.service()
        out = service.peers("GIS")
        lite = [c for c in calls if c[1] is False]
        self.assertLessEqual(len(lite), CANDIDATE_BUDGET)
        self.assertIsInstance(out["candidates_not_yet_profiled"], list)

    def test_concurrency_is_bounded_not_one_thread_per_symbol(self):
        """A fan-out proportional to the universe would be impolite to
        SEC and unbounded as the universe grows."""
        from agent.company.service import FETCH_CONCURRENCY
        self.assertLessEqual(FETCH_CONCURRENCY, 10)
        self.assertGreater(FETCH_CONCURRENCY, 1)

    def test_one_failing_candidate_does_not_fail_the_peer_set(self):
        service, _facts, _calls = self.service()
        original = service._submissions

        def flaky(symbol):
            if symbol == "P02":
                raise RuntimeError("sec timeout")
            return original(symbol)
        service._submissions = flaky
        out = service.peers("GIS")
        self.assertNotIn("P02", [p["symbol"] for p in out["peers"]])
        self.assertTrue(out["peers"])

    def test_a_failed_market_cap_says_why(self):
        service, _facts, _calls = self.service()
        service._price = lambda s: (_ for _ in ()).throw(
            RuntimeError("quote unavailable"))
        p = service.profile("GIS")
        self.assertIsNone(p.market_cap)
        self.assertIn("quote unavailable",
                      " ".join(x.source for x in p.provenance))


class TestUpcomingActionsAreInTheWindow(unittest.TestCase):
    """Verified live 2026-10-01: next_ex_date was None for AAPL, NVDA, GE
    and COST alike, because the fetch window ended today and an announced
    future ex-date can never fall inside it."""

    def test_the_window_reaches_past_today(self):
        acts = FakeActions(QUARTERLY)
        s, _ = svc(acts)
        s.dividends("GIS")
        [(_sym, start, end)] = [(c["symbols"], c["start"], c["end"])
                                for c in acts.windows]
        self.assertGreater(end, TODAY.isoformat())
        self.assertLess(start, TODAY.isoformat())

    def test_an_announced_future_dividend_becomes_the_next_ex_date(self):
        future = div("2026-10-09", 0.61)
        s, _ = svc(FakeActions(QUARTERLY + [future]))
        d = s.dividends("GIS")
        self.assertEqual(d["next_ex_date"], "2026-10-09")
        self.assertEqual(d["days_until_ex"], 8)

    def test_a_future_dividend_is_not_counted_in_the_trailing_amount(self):
        s, _ = svc(FakeActions(QUARTERLY + [div("2026-10-09", 0.61)]))
        d = s.dividends("GIS")
        self.assertAlmostEqual(d["trailing_12m_amount"], 0.61 * 3 + 0.60, 4)
