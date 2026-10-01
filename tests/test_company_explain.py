"""
Company questions answered from the record, or declined.

The failure this guards against is specific: a language model will state
a dividend yield or a competitor list from memory, and the reader cannot
tell a remembered number from a measured one. So every answer here is
assembled from stored company intelligence, names the record it came
from, and reports `llm_used: False` - and when the record is silent it
says so instead of producing something plausible.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from agent.company import explain as ce                            # noqa: E402
from test_company_service import (                                 # noqa: E402
    FakeActions, FakeFacts, QUARTERLY, TODAY, div, fp, svc,
)

SUBS = {
    "GIS": {"name": "General Mills", "sic": "2040",
            "sicDescription": "Grain Mill Products", "cik": 40704},
    "CPB": {"name": "Campbell", "sic": "2030",
            "sicDescription": "Canned Fruits & Veg", "cik": 16732},
    "AAPL": {"name": "Apple", "sic": "3571",
             "sicDescription": "Electronic Computers", "cik": 320193},
}


def facts_for(symbol, revenue=120.0, prior=100.0, end="2026-05-31",
              net=12.0, debt=50.0, equity=25.0):
    start = f"{int(end[:4]) - 1}-06-01"
    pstart, pend = f"{int(end[:4]) - 2}-06-01", f"{int(end[:4]) - 1}-05-31"
    return {symbol: {
        "revenue": [fp("revenue", revenue, start, end),
                    fp("revenue", prior, pstart, pend)],
        "net_income": [fp("net_income", net, start, end)],
        "operating_income": [fp("operating_income", net * 2, start, end)],
        "total_debt": [fp("total_debt", debt, None, end)],
        "equity": [fp("equity", equity, None, end)],
        "current_assets": [fp("current_assets", 40.0, None, end)],
        "current_liabilities": [fp("current_liabilities", 20.0, None, end)],
        "operating_cash_flow": [fp("operating_cash_flow", 20.0, start, end)],
        "capex": [fp("capex", 5.0, start, end)],
        # Quarterly EPS as well: earnings records come from reported
        # QUARTERS, so an annual-only fixture would exercise the
        # "no record" path instead of the real one.
        "eps_diluted": _quarters(end)}}


def _quarters(end, n=5):
    """`n` sequential 91-day quarters ending at `end`."""
    from datetime import date, timedelta
    last = date.fromisoformat(end)
    out = []
    for i in range(n):
        q_end = last - timedelta(days=91 * i)
        q_start = q_end - timedelta(days=90)
        out.append(fp("eps_diluted", 1.0 + 0.05 * (n - i),
                      q_start.isoformat(), q_end.isoformat(),
                      fpd=f"Q{(n - i - 1) % 4 + 1}"))
    return list(reversed(out))


def service(actions=None, facts=None, universe=("CPB", "AAPL")):
    data = facts if facts is not None else {}
    s, _store = svc(actions or FakeActions(QUARTERLY),
                    FakeFacts(data, shares={k: 1_000_000 for k in SUBS}),
                    subs=SUBS, universe=universe)
    return s


def ask(question, svc_=None, **kw):
    return ce.explain(question, svc_ or service(), **kw)


class TestClassification(unittest.TestCase):

    CASES = {
        "Does GIS pay a dividend?": "DIVIDEND",
        "what is GIS's yield": "DIVIDEND",
        "When was its last dividend?": "LAST_DIVIDEND",
        "Has GIS cut its dividend?": "DIVIDEND_CUT",
        "is the dividend safe": "DIVIDEND_CUT",
        "When is the next ex-date?": "NEXT_EX_DATE",
        "Has it split before?": "SPLITS",
        "Who are GIS's competitors?": "COMPETITORS",
        "How does GIS compare with CPB?": "COMPARE_WITH",
        "Is revenue growing?": "REVENUE_GROWTH",
        "What are GIS's margins?": "MARGINS",
        "How much debt does it have?": "DEBT",
        "When was its last earnings report?": "LAST_EARNINGS",
        "When is the next earnings?": "NEXT_EARNINGS",
        "Would this look different if we considered holding it?":
            "HOLDING_DIFFERENCE",
        "What corporate actions should I know about?": "CORPORATE_ACTIONS",
    }

    def test_every_listed_question_is_recognised(self):
        for question, intent in self.CASES.items():
            with self.subTest(question=question):
                self.assertEqual(ce.classify(question), intent)

    def test_an_unrelated_question_is_not_forced(self):
        for q in ("tell me a joke", "", "   ", "what is the weather"):
            with self.subTest(q=q):
                self.assertIsNone(ce.classify(q))

    def test_two_symbols_are_read_in_order(self):
        self.assertEqual(ce.symbols("How does GIS compare with CPB?"),
                         ["GIS", "CPB"])

    def test_ordinary_words_are_not_symbols(self):
        self.assertEqual(ce.symbols("Does it pay a dividend?"), [])
        self.assertEqual(ce.symbols("How much debt does it have?"), [])

    def test_a_cut_question_is_not_read_as_a_plain_dividend_question(self):
        self.assertEqual(ce.classify("has GIS ever cut the dividend"),
                         "DIVIDEND_CUT")

    def test_an_ex_date_question_outranks_last_dividend(self):
        self.assertEqual(ce.classify("when is the next ex-dividend date"),
                         "NEXT_EX_DATE")


class TestAnswersComeFromTheRecord(unittest.TestCase):

    def test_a_payer_is_reported_with_its_yield_basis(self):
        out = ask("Does GIS pay a dividend?")
        self.assertTrue(out["grounded"])
        self.assertEqual(out["dividend_stock"], "YES")
        self.assertIn("ACTIVE", out["answer"])
        self.assertIn("price 50.0", out["answer"])
        self.assertIn("dividends", out["sources"])

    def test_a_non_payer_says_which_window_was_searched(self):
        out = ask("Does GIS pay a dividend?", service(FakeActions([])))
        self.assertEqual(out["dividend_stock"], "NO")
        self.assertIn("10-year window", out["answer"])

    def test_a_provider_outage_is_not_answered_from_memory(self):
        out = ask("Does GIS pay a dividend?", service(FakeActions(fail=True)))
        self.assertFalse(out["grounded"])
        self.assertIn("cannot establish", out["answer"])

    def test_the_last_dividend_names_its_dates(self):
        out = ask("When was GIS's last dividend?")
        self.assertIn("2026-07-10", out["answer"])
        self.assertIn("0.61", out["answer"])

    def test_a_special_dividend_is_labelled_as_special(self):
        acts = FakeActions(QUARTERLY + [div("2026-08-01", 3.0, True)])
        out = ask("When was GIS's last dividend?", service(acts))
        self.assertIn("special", out["answer"])

    def test_specials_are_not_reported_as_the_regular_dividend(self):
        acts = FakeActions(QUARTERLY + [div("2026-02-01", 3.0, True)])
        out = ask("Does GIS pay a dividend?", service(acts))
        self.assertIn("not counted as regular", out["answer"])

    def test_a_cut_is_found_in_the_amounts(self):
        cut = [div("2026-07-10", 0.30), div("2026-04-10", 0.61),
               div("2026-01-10", 0.61), div("2025-10-10", 0.61),
               div("2025-07-10", 0.60)]
        out = ask("Has GIS cut its dividend?", service(FakeActions(cut)))
        self.assertIn("decreased", out["answer"])
        self.assertIn("2026-07-10", out["answer"])

    def test_no_cut_is_stated_as_no_cut_within_the_window(self):
        out = ask("Has GIS cut its dividend?")
        self.assertIn("No decrease", out["answer"])
        self.assertIn("window", out["answer"])

    def test_a_cut_answer_mentions_splits_as_an_alternative_cause(self):
        out = ask("Has GIS cut its dividend?")
        self.assertIn("split", out["answer"])

    def test_an_undeclared_next_ex_date_is_not_yet_announced(self):
        out = ask("When is GIS's next ex-date?")
        self.assertIn("not yet announced", out["answer"])
        self.assertIsNone(out["next_ex_date"])

    def test_a_declared_next_ex_date_is_reported_with_the_caveat(self):
        acts = FakeActions(QUARTERLY + [div("2026-10-09", 0.61)])
        out = ask("When is GIS's next ex-date?", service(acts))
        self.assertIn("2026-10-09", out["answer"])
        self.assertIn("not free return", out["answer"])

    def test_splits_are_described_in_words_with_direction(self):
        from agent.company.models import ActionType, CorporateAction
        acts = FakeActions([CorporateAction(
            ActionType.REVERSE_SPLIT, "GIS", ex_date="2025-03-12",
            ratio_new=1, ratio_old=10)])
        out = ask("Has GIS split before?", service(acts))
        self.assertIn("1-for-10 reverse split", out["answer"])

    def test_no_split_is_stated_plainly(self):
        self.assertIn("No stock split", ask("Has GIS split?")["answer"])

    def test_competitors_come_with_structured_reasons(self):
        out = ask("Who are GIS's competitors?")
        self.assertIn("CPB", out["answer"])
        self.assertNotIn("AAPL", out["answer"].split("Each needs")[0])
        self.assertIn("industry reason", out["answer"])

    def test_revenue_growth_names_both_periods(self):
        out = ask("Is GIS revenue growing?",
                  service(facts=facts_for("GIS")))
        self.assertIn("20.00%", out["answer"])
        self.assertIn("2026-05-31", out["answer"])

    def test_margins_name_the_period_and_the_freshness(self):
        out = ask("What are GIS's margins?",
                  service(facts=facts_for("GIS")))
        self.assertIn("2026-05-31", out["answer"])
        self.assertIn("Freshness", out["answer"])

    def test_an_unreported_margin_is_unknown_with_a_reason(self):
        out = ask("What are GIS's margins?",
                  service(facts=facts_for("GIS")))
        self.assertIn("gross margin UNKNOWN", out["answer"])

    def test_debt_names_the_filing_it_came_from(self):
        out = ask("How much debt does GIS have?",
                  service(facts=facts_for("GIS")))
        self.assertIn("total debt", out["answer"])
        self.assertIn("10-K", out["answer"])
        self.assertIn("Debt/equity 2.00", out["answer"])

    def test_last_earnings_reports_that_no_estimate_exists(self):
        out = ask("When was GIS's last earnings report?",
                  service(facts=facts_for("GIS")))
        self.assertIn("never inferred", out["answer"])

    def test_next_earnings_is_declined_with_the_reason(self):
        out = ask("When is GIS's next earnings?",
                  service(facts=facts_for("GIS")))
        self.assertFalse(out["grounded"])
        self.assertIn("not what is scheduled", out["answer"])

    def test_corporate_actions_are_counted_by_type(self):
        out = ask("What corporate actions should I know about for GIS?")
        self.assertIn("CASH_DIVIDEND", out["answer"])


class TestComparisonRefusesMismatchedPeriods(unittest.TestCase):

    def test_aligned_companies_are_compared(self):
        data = {}
        data.update(facts_for("GIS", revenue=120, prior=100))
        data.update(facts_for("CPB", revenue=210, prior=200))
        out = ask("How does GIS compare with CPB?", service(facts=data))
        self.assertTrue(out["grounded"])
        self.assertIn("revenue_growth", out["answer"])
        self.assertIn("not a ranking", out["answer"])

    def test_different_fiscal_year_ends_are_refused_not_blended(self):
        data = {}
        data.update(facts_for("GIS", end="2026-05-31"))
        data.update(facts_for("CPB", end="2025-12-31"))
        out = ask("How does GIS compare with CPB?", service(facts=data))
        self.assertFalse(out["grounded"])
        self.assertIn("different fiscal", out["answer"])
        self.assertIn("2026-05-31", out["answer"])
        self.assertIn("2025-12-31", out["answer"])

    def test_a_comparison_without_a_second_company_asks_for_one(self):
        out = ask("How does GIS compare with it?")
        self.assertFalse(out["grounded"])


class TestItDeclinesRatherThanInvents(unittest.TestCase):

    def test_no_answer_claims_a_language_model(self):
        svc_ = service(facts=facts_for("GIS"))
        for question in TestClassification.CASES:
            with self.subTest(question=question):
                self.assertFalse(ask(question, svc_)["llm_used"])

    def test_the_module_imports_no_model_client(self):
        body = open(os.path.join(REPO, "agent", "company",
                                 "explain.py")).read().lower()
        for banned in ("import openai", "from openai", "chat.completions",
                       "anthropic", "gpt-"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, body)

    def test_a_question_with_no_company_asks_which(self):
        out = ask("Does it pay a dividend?")
        self.assertFalse(out["grounded"])
        self.assertIn("Which company", out["answer"])

    def test_a_default_symbol_can_supply_the_context(self):
        out = ask("Does it pay a dividend?", default_symbol="GIS")
        self.assertTrue(out["grounded"])
        self.assertEqual(out["symbol"], "GIS")

    def test_an_unsupported_question_lists_what_it_can_answer(self):
        out = ask("tell me a joke")
        self.assertFalse(out["grounded"])
        self.assertIn("dividends", out["answer"])

    def test_a_record_read_failure_is_not_papered_over(self):
        class Broken:
            def dividends(self, symbol):
                raise RuntimeError("dynamo unavailable")
        out = ce.explain("Does GIS pay a dividend?", Broken())
        self.assertFalse(out["grounded"])
        self.assertIn("will not answer from memory", out["answer"])


class TestHoldingContextIsNotAnInstruction(unittest.TestCase):

    def test_it_says_so_explicitly(self):
        out = ask("Would GIS look different if we considered holding it?",
                  service(facts=facts_for("GIS")))
        self.assertIn("NOT an instruction", out["answer"])
        self.assertIn("DISABLED", out["answer"])
        self.assertIn("no composite score", out["answer"])

    def test_it_names_the_factors_behind_the_label(self):
        out = ask("Would GIS differ if we held it?",
                  service(facts=facts_for("GIS")))
        self.assertIn(out["label"], out["answer"])
        self.assertIn("Factors:", out["answer"])

    def test_it_does_not_recommend(self):
        out = ask("Would GIS differ if we held it overnight?",
                  service(facts=facts_for("GIS")))
        for word in ("you should", "recommend", "buy it", "sell it"):
            with self.subTest(word=word):
                self.assertNotIn(word, out["answer"].lower())


if __name__ == "__main__":
    unittest.main()
