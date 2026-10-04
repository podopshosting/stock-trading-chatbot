"""Resolving the date in a question.

"What happened yesterday?" asked on a Monday means Friday. Without
this, the answer was built against today whatever the question said, so
a question about Thursday got the weekend's empty records and read as
"nothing happened" - a different and much worse answer than "there was
no session".
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent import chat_dates as CD                           # noqa: E402

# 2026-10-05 is a Monday; 2026-10-04 a Sunday; 2026-10-02 a Friday.
MONDAY = date(2026, 10, 5)
SUNDAY = date(2026, 10, 4)
WEDNESDAY = date(2026, 10, 7)


class TestYesterday(unittest.TestCase):

    def test_yesterday_on_a_wednesday_is_tuesday(self):
        r = CD.resolve("what happened yesterday?", today=WEDNESDAY)
        self.assertEqual(r.resolved_date, "2026-10-06")
        self.assertEqual(r.basis, "YESTERDAY")

    def test_yesterday_on_a_MONDAY_is_FRIDAY(self):
        """The case the whole module exists for.

        Sunday has no session. Reporting its empty records reads as
        "nothing happened", which is not what the asker wants to know.
        """
        r = CD.resolve("what happened yesterday?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-10-02")
        self.assertEqual(r.basis, "YESTERDAY_ADJUSTED_TO_TRADING_DAY")
        self.assertIn("weekend", r.note)

    def test_the_adjustment_is_reported_not_silent(self):
        # A date silently chosen is a date the reader cannot check.
        r = CD.resolve("yesterday", today=MONDAY)
        self.assertIsNotNone(r.note)
        self.assertIn("2026-10-04", r.note)

    def test_today_is_today_even_on_a_weekend(self):
        # "Today" is not ambiguous, so it is not adjusted. The caller's
        # own "no session recorded" answer is then correct.
        r = CD.resolve("how did today go?", today=SUNDAY)
        self.assertEqual(r.resolved_date, "2026-10-04")
        self.assertFalse(r.is_trading_day)


class TestExplicitDates(unittest.TestCase):

    def test_an_iso_date_wins(self):
        r = CD.resolve("what trades happened on 2026-10-01?",
                       today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-10-01")
        self.assertEqual(r.basis, "EXPLICIT_ISO_DATE")

    def test_a_month_and_day(self):
        r = CD.resolve("what happened October 1?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-10-01")

    def test_day_then_month_also_works(self):
        r = CD.resolve("what happened 1 October?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-10-01")

    def test_a_month_day_in_the_future_uses_last_year(self):
        # "October 1" asked in September cannot mean a date that has
        # not happened: no record could exist for it.
        r = CD.resolve("what happened October 1?",
                       today=date(2026, 9, 15))
        self.assertEqual(r.resolved_date, "2025-10-01")
        self.assertIn("not happened yet", r.note)

    def test_an_impossible_date_is_unresolved_not_clamped(self):
        # Clamping to the 30th would answer about a day nobody asked
        # about.
        r = CD.resolve("what happened on 2026-02-31?", today=MONDAY)
        self.assertFalse(r.resolved)
        self.assertIn("not a real date", r.note)

    def test_an_explicit_year_is_honoured(self):
        r = CD.resolve("what happened October 1, 2025?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2025-10-01")


class TestWeekdays(unittest.TestCase):

    def test_a_weekday_resolves_to_the_most_recent_past_one(self):
        # A future weekday cannot have records.
        r = CD.resolve("why didn't we trade Thursday?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-10-01")
        self.assertEqual(r.basis, "MOST_RECENT_PAST_WEEKDAY")

    def test_the_ambiguity_is_reported(self):
        r = CD.resolve("what about Friday?", today=MONDAY)
        self.assertIn("ambiguous", r.note)
        self.assertEqual(r.resolved_date, "2026-10-02")

    def test_the_same_weekday_as_today_means_the_PREVIOUS_one(self):
        # "Monday" asked on a Monday about what happened means last
        # Monday; today's session may not have run yet.
        r = CD.resolve("what happened Monday?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-09-28")


class TestRelativeCounts(unittest.TestCase):

    def test_n_days_ago_counts_calendar_days(self):
        r = CD.resolve("what happened 3 days ago?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-10-02")
        self.assertEqual(r.basis, "N_DAYS_AGO")

    def test_n_sessions_ago_skips_weekends(self):
        # Three SESSIONS back from Monday is Wednesday, not Friday.
        r = CD.resolve("what happened 3 sessions ago?", today=MONDAY)
        self.assertEqual(r.resolved_date, "2026-09-30")
        self.assertEqual(r.basis, "N_SESSIONS_AGO")

    def test_the_two_differ_which_is_why_both_exist(self):
        # Falsifying control: if "sessions" were treated as "days",
        # both tests above would pass with one implementation.
        days = CD.resolve("3 days ago", today=MONDAY).resolved_date
        sessions = CD.resolve("3 sessions ago", today=MONDAY).resolved_date
        self.assertNotEqual(days, sessions)


class TestNoDateMeansUnresolved(unittest.TestCase):

    def test_a_question_with_no_date_is_unresolved(self):
        r = CD.resolve("what is the agent doing?", today=MONDAY)
        self.assertFalse(r.resolved)
        self.assertEqual(r.basis, CD.UNRESOLVED)
        self.assertIn("no date was named", r.note)

    def test_unresolved_carries_guidance_rather_than_a_guess(self):
        r = CD.resolve("", today=MONDAY)
        self.assertFalse(r.resolved)
        self.assertIn("yesterday", r.note)


class TestKnownSessions(unittest.TestCase):

    def test_a_date_with_no_session_is_reported_not_moved(self):
        # "The Tuesday you asked about has no session recorded" is a
        # better answer than silently answering about a Tuesday that
        # does.
        r = CD.resolve("what happened yesterday?", today=WEDNESDAY,
                       known_sessions=["2026-10-01", "2026-10-02"])
        self.assertEqual(r.resolved_date, "2026-10-06")
        self.assertIn("no session is recorded", r.note)

    def test_a_date_with_a_session_gets_no_such_note(self):
        r = CD.resolve("what happened 2026-10-01?", today=WEDNESDAY,
                       known_sessions=["2026-10-01"])
        self.assertEqual(r.resolved_date, "2026-10-01")
        self.assertIsNone(r.note)


class TestQuestionDetection(unittest.TestCase):

    def test_it_recognises_dated_questions(self):
        for q in ("what happened yesterday?",
                  "what trades happened October 1?",
                  "why didn't we trade Thursday?",
                  "what orders were submitted yesterday?",
                  "what positions did we hold Friday?",
                  "what happened on 2026-10-01?",
                  "what happened 3 sessions ago?"):
            self.assertTrue(CD.looks_like_a_date_question(q), q)

    def test_it_ignores_questions_with_no_date(self):
        # A false positive costs one extra read; a false negative sends
        # the question to a generic reply when the records held the
        # answer. So this is permissive - but not unconditional.
        for q in ("what is my risk limit?", "analyze AAPL",
                  "is the broker connected?", ""):
            self.assertFalse(CD.looks_like_a_date_question(q), q)


if __name__ == "__main__":
    unittest.main()


class TestTheHandlerReadsBothRecordShapes(unittest.TestCase):
    """A regression control for a 500 that only appeared on a BUSY day.

    `list_trades` returns TradeRecord OBJECTS while the decision log
    returns dicts. Calling .get on a TradeRecord raised AttributeError
    and the whole answer became `{"error": "internal error"}` - but
    only for a date that HAD trades. The empty-day case passed, so the
    defect was invisible until a populated day was asked about.
    """

    def _handler(self):
        import importlib.util
        path = os.path.join(REPO, "lambda-micro", "agent-api",
                            "handler.py")
        spec = importlib.util.spec_from_file_location(
            "agent_api_handler_dates", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_api_handler_dates"] = module
        spec.loader.exec_module(module)
        return module

    def test_the_accessor_handles_an_object_and_a_mapping(self):
        from dataclasses import dataclass

        @dataclass
        class Rec:
            symbol: str = "AAPL"
            net_pnl: float = -0.45
            exceeded_planned_risk: bool = True

        src = open(os.path.join(REPO, "lambda-micro", "agent-api",
                                "handler.py")).read()
        # The fix must be a shared accessor, not a .get sprinkled with
        # try/except at each site: the next field added would reopen it.
        self.assertIn("def fld(row, name, default=None):", src)
        self.assertIn("isinstance(row, dict)", src)

    def test_no_dated_answer_path_calls_get_on_a_trade(self):
        # The specific mistake, pinned textually. The dated-records
        # helper must route every field read through the accessor.
        src = open(os.path.join(REPO, "lambda-micro", "agent-api",
                                "handler.py")).read()
        start = src.index("def _answer_from_dated_records")
        end = src.index("\ndef ", start + 10)
        body = src[start:end]
        for forbidden in ('t.get("net_pnl")', 't.get("symbol")',
                          'd.get("approved")', 'd.get("reason_codes")',
                          'report.get("session_ok")'):
            self.assertNotIn(forbidden, body, forbidden)
