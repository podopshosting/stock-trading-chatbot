import os, sys, unittest
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.company import (DividendEvent, DividendStatus, SplitEvent,
                           SplitType, classify, with_metrics, summarise,
                           explains_price_drop)

TODAY = date(2026, 10, 1)


def quarterly(n=8, kind="REGULAR", last="2026-09-10"):
    y, m, d = map(int, last.split("-"))
    out = []
    for i in range(n):
        mm = m - 3 * i
        yy = y
        while mm < 1:
            mm += 12; yy -= 1
        out.append(DividendEvent(f"{yy}-{mm:02d}-{d:02d}", 0.61, kind=kind))
    return out


class TestDividendStatus(unittest.TestCase):
    def test_quarterly_payer_is_active(self):
        p = classify(quarterly(), TODAY)
        self.assertEqual(p.status, DividendStatus.ACTIVE)
        self.assertTrue(p.pays_dividend)

    def test_no_data_is_unknown_not_no_dividend(self):
        p = classify(None, TODAY)
        self.assertEqual(p.status, DividendStatus.UNKNOWN)
        self.assertIsNone(p.pays_dividend)

    def test_empty_partial_history_is_unknown(self):
        self.assertEqual(classify([], TODAY).status, DividendStatus.UNKNOWN)

    def test_empty_complete_history_is_no_dividend(self):
        p = classify([], TODAY, history_complete=True)
        self.assertEqual(p.status, DividendStatus.NO_DIVIDEND)
        self.assertFalse(p.pays_dividend)

    def test_special_only_is_not_a_regular_payer(self):
        p = classify(quarterly(3, kind="SPECIAL"), TODAY)
        self.assertEqual(p.status, DividendStatus.IRREGULAR)
        self.assertFalse(p.pays_dividend)

    def test_special_dividend_not_counted_as_regular(self):
        ev = quarterly() + [DividendEvent("2026-06-01", 5.0, kind="SPECIAL")]
        p = with_metrics(classify(ev, TODAY), ev, TODAY, 100.0, "2026-10-01")
        self.assertAlmostEqual(p.trailing_12m_amount, 0.61 * 4, places=4)

    def test_single_payment_does_not_establish_active(self):
        p = classify(quarterly(1), TODAY)
        self.assertNotEqual(p.status, DividendStatus.ACTIVE)
        self.assertIn("one payment", p.reason)

    def test_long_gap_is_suspended(self):
        p = classify(quarterly(8, last="2025-03-10"), TODAY)
        self.assertEqual(p.status, DividendStatus.SUSPENDED)

    def test_erratic_spacing_is_irregular(self):
        dates = ["2025-01-05", "2025-02-01", "2025-09-20", "2026-03-01"]
        p = classify([DividendEvent(d, 0.5) for d in dates], TODAY)
        self.assertEqual(p.status, DividendStatus.IRREGULAR)

    def test_yield_names_its_price_basis(self):
        ev = quarterly()
        p = with_metrics(classify(ev, TODAY), ev, TODAY, 50.0, "2026-09-30")
        self.assertIn("50.0", p.yield_price_basis)
        self.assertIn("2026-09-30", p.yield_price_basis)

    def test_no_price_means_no_yield(self):
        ev = quarterly()
        p = with_metrics(classify(ev, TODAY), ev, TODAY, None, None)
        self.assertIsNone(p.trailing_yield_pct)

    def test_next_ex_date_and_countdown(self):
        ev = quarterly() + [DividendEvent("2026-10-09", 0.61)]
        p = with_metrics(classify(ev, TODAY), ev, TODAY, 100.0, "x")
        self.assertEqual(p.next_ex_date, "2026-10-09")
        self.assertEqual(p.days_until_ex, 8)


class TestSplits(unittest.TestCase):
    def test_reverse_split_is_not_forward(self):
        self.assertEqual(SplitEvent("2026-05-01", 1, 10).type,
                         SplitType.REVERSE_SPLIT)
        self.assertEqual(SplitEvent("2026-05-01", 4, 1).type,
                         SplitType.FORWARD_SPLIT)

    def test_summary_flags_recent(self):
        s = summarise([SplitEvent("2026-06-01", 10, 1)], TODAY)
        self.assertTrue(s["recent"])
        self.assertEqual(s["count"], 1)

    def test_split_day_drop_is_not_called_a_crash(self):
        why = explains_price_drop([SplitEvent("2026-06-10", 10, 1)],
                                  "2026-06-10", -90.0)
        self.assertIn("split", why)

    def test_a_real_drop_on_another_day_is_not_explained_away(self):
        self.assertIsNone(explains_price_drop(
            [SplitEvent("2026-06-10", 10, 1)], "2026-06-11", -30.0))


if __name__ == "__main__":
    unittest.main()
