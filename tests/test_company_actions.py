import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.company.models import ActionType, CorporateAction, Provenance
from agent.company.providers.alpaca_corporate_actions import (
    AlpacaCorporateActions, normalise, to_dividend_events, to_split_events)
from agent.company.crosscheck import cross_check, CONFLICT
from agent.providers.base import DataUnavailable

P = Provenance("alpaca", "/v1/corporate-actions", "2026-10-01T00:00:00+00:00")


class FakeAlpaca:
    data_base = "https://data.example"

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def _get(self, base, path, params=None):
        self.calls.append(dict(params))
        return self.pages[len(self.calls) - 1]


PAGE1 = {"corporate_actions": {
    "cash_dividends": [
        {"symbol": "GIS", "rate": 0.61, "ex_date": "2026-07-10",
         "record_date": "2026-07-11", "payable_date": "2026-08-01",
         "special": False, "id": "d1"},
        {"symbol": "GIS", "rate": 2.0, "ex_date": "2026-03-01",
         "special": True}],
    "forward_splits": [{"symbol": "NVDA", "new_rate": 10, "old_rate": 1,
                        "ex_date": "2024-06-10"}],
    "reverse_splits": [{"symbol": "XYZ", "new_rate": 1, "old_rate": 10,
                        "ex_date": "2025-03-12"}],
    "spin_offs": [{"source_symbol": "AAA", "new_symbol": "BBB",
                   "source_rate": 1, "new_rate": 0.5, "ex_date": "2025-01-02"}],
    "mystery_bucket": [{"symbol": "GIS", "foo": "bar"}]},
    "next_page_token": "t2"}
PAGE2 = {"corporate_actions": {"name_changes": [
    {"old_symbol": "OLD", "new_symbol": "NEW", "process_date": "2025-05-05"}]},
    "next_page_token": None}


class TestProvider(unittest.TestCase):
    def fetch(self):
        fake = FakeAlpaca([PAGE1, PAGE2])
        acts = AlpacaCorporateActions(fake, wall_clock=lambda: 0).fetch(
            "GIS", "2020-01-01", "2026-10-01")
        return acts, fake

    def test_pages_are_followed(self):
        acts, fake = self.fetch()
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1]["page_token"], "t2")

    def test_missing_dates_stay_none(self):
        acts, _ = self.fetch()
        special = [a for a in acts if a.special][0]
        self.assertIsNone(special.record_date)
        self.assertIsNone(special.payable_date)
        self.assertIsNone(special.announcement_date)

    def test_process_date_is_not_promoted_to_ex_date(self):
        acts, _ = self.fetch()
        rename = [a for a in acts if a.type is ActionType.NAME_CHANGE][0]
        self.assertIsNone(rename.ex_date)
        self.assertIsNone(rename.effective_date)

    def test_types_and_ratios(self):
        acts, _ = self.fetch()
        by = {(a.type, a.symbol): a for a in acts}
        self.assertIn((ActionType.FORWARD_SPLIT, "NVDA"), by)
        self.assertIn((ActionType.REVERSE_SPLIT, "XYZ"), by)
        self.assertEqual(by[(ActionType.SPIN_OFF, "AAA")].related_symbol, "BBB")

    def test_unknown_bucket_is_kept_not_dropped(self):
        acts, _ = self.fetch()
        other = [a for a in acts if a.type is ActionType.OTHER]
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0].detail["raw"]["foo"], "bar")

    def test_mislabelled_bucket_does_not_flip_direction(self):
        a = normalise("forward_splits",
                      {"symbol": "X", "new_rate": 1, "old_rate": 10,
                       "ex_date": "2025-01-01"}, "X", P)
        self.assertEqual(a.type, ActionType.REVERSE_SPLIT)

    def test_bridge_special_flag_and_split_direction(self):
        acts, _ = self.fetch()
        kinds = sorted(e.kind for e in to_dividend_events(acts))
        self.assertEqual(kinds, ["REGULAR", "SPECIAL"])
        rev = [s for s in to_split_events(acts) if s.ex_date == "2025-03-12"]
        self.assertEqual(str(rev[0].type), "REVERSE_SPLIT")

    def test_page_cap_raises_instead_of_truncating(self):
        fake = FakeAlpaca([{"corporate_actions": {}, "next_page_token": "t"}] * 25)
        with self.assertRaises(DataUnavailable):
            AlpacaCorporateActions(fake, wall_clock=lambda: 0).fetch(
                "GIS", "2020-01-01", "2026-10-01")

    def test_provenance_is_attached(self):
        acts, _ = self.fetch()
        self.assertTrue(all(a.provenance and a.provenance.provider == "alpaca"
                            for a in acts))


class TestCrossCheck(unittest.TestCase):
    def div(self, amt, prov="alpaca"):
        return CorporateAction(ActionType.CASH_DIVIDEND, "GIS",
                               ex_date="2026-07-10", amount=amt,
                               provenance=Provenance(prov, "s", "t"))

    def test_disagreement_is_preserved_not_resolved(self):
        w = cross_check([self.div(0.61)], [self.div(0.16, "other")])
        self.assertEqual(w[0]["code"], CONFLICT)
        self.assertEqual(w[0]["differences"]["amount"], (0.61, 0.16))
        self.assertEqual(w[0]["resolution"], "NONE_PRESERVED_BOTH")

    def test_agreement_and_single_source_are_not_conflicts(self):
        self.assertEqual(cross_check([self.div(0.61)], [self.div(0.61, "o")]), [])
        self.assertEqual(cross_check([self.div(0.61)], []), [])

    def test_split_ratio_conflict(self):
        a = CorporateAction(ActionType.FORWARD_SPLIT, "X", ex_date="2024-01-01",
                            ratio_new=4, ratio_old=1)
        b = CorporateAction(ActionType.FORWARD_SPLIT, "X", ex_date="2024-01-01",
                            ratio_new=2, ratio_old=1)
        self.assertEqual(len(cross_check([a], [b])), 1)


if __name__ == "__main__":
    unittest.main()


class TestImmutabilityComparesFactsNotFetches(unittest.TestCase):
    """
    Verified live 2026-10-01: re-ingesting AAPL reported all 41 actions as
    conflicting, because the request window was stored inside each
    action's provenance and the window had changed. A HistoryConflict has
    to mean the provider changed the fact, or it is noise that hides one.
    """

    def row(self):
        return {"symbol": "AAPL", "rate": 0.57, "ex_date": "2016-11-03",
                "id": "abc", "special": False}

    def action(self, window, amount=0.57):
        prov = Provenance("alpaca", f"/v1/corporate-actions?{window}", "t1")
        row = self.row()
        row["rate"] = amount
        return normalise("cash_dividends", row, "AAPL", prov)

    def test_the_same_dividend_fetched_twice_is_not_a_conflict(self):
        from agent.company.store import InMemoryCompanyStore
        store = InMemoryCompanyStore()
        a = self.action("start=2016&end=2026")
        b = self.action("start=2016&end=2027")        # window changed
        self.assertTrue(store.put_fact("AAPL", "ACTION", "k", a.as_dict()))
        self.assertFalse(store.put_fact("AAPL", "ACTION", "k", b.as_dict()))

    def test_a_changed_amount_is_still_a_conflict(self):
        from agent.company.store import HistoryConflict, InMemoryCompanyStore
        store = InMemoryCompanyStore()
        store.put_fact("AAPL", "ACTION", "k",
                       self.action("w1").as_dict())
        with self.assertRaises(HistoryConflict):
            store.put_fact("AAPL", "ACTION", "k",
                           self.action("w1", amount=0.99).as_dict())

    def test_provenance_period_describes_the_action_not_the_query(self):
        a = self.action("start=2016&end=2026")
        self.assertEqual(a.provenance.period, "2016-11-03")
        self.assertIn("start=2016", a.provenance.source)

    def test_provenance_is_still_stored(self):
        from agent.company.store import InMemoryCompanyStore
        store = InMemoryCompanyStore()
        store.put_fact("AAPL", "ACTION", "k", self.action("w1").as_dict())
        [stored] = store.facts("AAPL", "ACTION")
        self.assertEqual(stored["provenance"]["provider"], "alpaca")
