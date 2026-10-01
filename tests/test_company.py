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


class TestCadenceIsJudgedOnTheMajorityNotTheWorstPair(unittest.TestCase):
    """
    Verified live 2026-10-01: COST's 37 regular intervals all fall between
    63 and 112 days (median 91) and GE has 36 of 39 near its median, yet
    both were reported IRREGULAR because max(gap) - min(gap) exceeded a
    fixed spread. One odd interval in a decade is calendar drift, not an
    irregular dividend.
    """

    def events(self, gaps, amount=0.61):
        from datetime import timedelta
        day = date(2016, 1, 10)
        out = [DividendEvent(day.isoformat(), amount)]
        for g in gaps:
            day = day + timedelta(days=g)
            out.append(DividendEvent(day.isoformat(), amount))
        return out, day

    def classify_with_gaps(self, gaps):
        events, last = self.events(gaps)
        return classify(events, last)

    def test_the_real_cost_interval_pattern_is_active(self):
        gaps = [98, 92, 91, 84, 112, 70, 91, 91, 112, 63, 98, 84, 91, 91,
                98, 84, 91, 91, 98, 84, 91, 91, 98, 91, 112, 70, 91, 84,
                92, 98, 98, 84, 91, 91, 91, 91, 84]
        self.assertEqual(self.classify_with_gaps(gaps).status,
                         DividendStatus.ACTIVE)

    def test_a_few_anomalies_in_a_decade_do_not_flip_the_status(self):
        gaps = [91] * 36 + [0, 126, 120]
        self.assertEqual(self.classify_with_gaps(gaps).status,
                         DividendStatus.ACTIVE)

    def test_genuinely_erratic_spacing_is_still_irregular(self):
        gaps = [27, 231, 162, 40, 300]
        self.assertEqual(self.classify_with_gaps(gaps).status,
                         DividendStatus.IRREGULAR)

    def test_a_majority_off_cadence_is_irregular(self):
        gaps = [91, 91, 240, 300, 30, 400]
        self.assertEqual(self.classify_with_gaps(gaps).status,
                         DividendStatus.IRREGULAR)

    def test_the_reason_counts_the_intervals(self):
        prof = self.classify_with_gaps([27, 231, 162, 40, 300])
        self.assertIn("intervals are near the typical", prof.reason)

    def test_a_stopped_payer_is_still_suspended_not_irregular(self):
        events, last = self.events([91] * 20)
        from datetime import timedelta
        self.assertEqual(classify(events, last + timedelta(days=400)).status,
                         DividendStatus.SUSPENDED)
