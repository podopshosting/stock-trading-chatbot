"""
Tests for MarketSessionService (Milestone 3).

Calendar and clock payloads are shaped exactly as the live Alpaca API
returned them on 2026-09-30, including the two properties that drive the
design:

* `is_open` covers the REGULAR session only, so it cannot distinguish
  pre-market from closed on its own.
* Non-trading days are ABSENT from the calendar rather than flagged.
  Thanksgiving 2026-11-26 has no row between 11-25 and 11-27.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.market import MarketSessionService, TradingDay  # noqa: E402
from agent.state.models import MarketSession  # noqa: E402


def cal_row(date, open_="09:30", close="16:00",
            session_open="0400", session_close="2000"):
    return {"date": date, "open": open_, "close": close,
            "session_open": session_open, "session_close": session_close,
            "settlement_date": date}


# A real week: Thanksgiving (11-26) absent, 11-27 an early close at 13:00.
THANKSGIVING_WEEK = [
    cal_row("2026-11-24"),
    cal_row("2026-11-25"),
    cal_row("2026-11-27", close="13:00", session_close="1700"),
    cal_row("2026-11-30"),
]

ORDINARY_WEEK = [cal_row(f"2026-09-{d}") for d in
                 ("28", "29", "30")] + [cal_row("2026-10-01")]


class FakeProvider:
    def __init__(self, clock=None, calendar=None,
                 clock_error=None, calendar_error=None):
        self._clock = clock
        self._calendar = calendar if calendar is not None else ORDINARY_WEEK
        self._clock_error = clock_error
        self._calendar_error = calendar_error
        self.clock_calls = 0
        self.calendar_calls = 0

    def get_clock(self):
        self.clock_calls += 1
        if self._clock_error:
            raise self._clock_error
        return self._clock

    def get_calendar(self, start, end):
        self.calendar_calls += 1
        if self._calendar_error:
            raise self._calendar_error
        return self._calendar


def clock(timestamp, is_open):
    return {"is_open": is_open, "timestamp": timestamp,
            "next_open": "2026-10-01T09:30:00-04:00",
            "next_close": "2026-09-30T16:00:00-04:00"}


class TestRegularSession(unittest.TestCase):
    def test_open_during_regular_hours(self):
        p = FakeProvider(clock("2026-09-30T12:11:33.278138646-04:00", True))
        r = MarketSessionService(p).current()

        self.assertIs(r.session, MarketSession.OPEN)
        self.assertTrue(r.is_open)
        self.assertTrue(r.is_trading_day)
        self.assertFalse(r.is_early_close)

    def test_nanosecond_timestamps_are_parsed(self):
        """Alpaca sends 9 fractional digits, which fromisoformat rejects on
        some versions."""
        p = FakeProvider(clock("2026-09-30T12:11:33.278138646-04:00", True))
        self.assertIs(MarketSessionService(p).current().session,
                      MarketSession.OPEN)

    def test_z_suffix_timestamps_are_parsed(self):
        p = FakeProvider(clock("2026-09-30T16:11:33Z", True))
        self.assertIs(MarketSessionService(p).current().session,
                      MarketSession.OPEN)


class TestPreAndPostMarket(unittest.TestCase):
    def test_pre_market(self):
        """07:00 ET: inside extended hours, before the regular open."""
        p = FakeProvider(clock("2026-09-30T07:00:00-04:00", False))
        r = MarketSessionService(p).current()
        self.assertIs(r.session, MarketSession.PRE_MARKET)
        self.assertFalse(r.is_open, "pre-market is not 'open' for this agent")
        self.assertTrue(r.is_trading_day)

    def test_after_hours(self):
        """18:00 ET: after the regular close, inside extended hours."""
        p = FakeProvider(clock("2026-09-30T18:00:00-04:00", False))
        self.assertIs(MarketSessionService(p).current().session,
                      MarketSession.AFTER_HOURS)

    def test_before_extended_open_is_closed(self):
        """03:00 ET, ahead of the 04:00 extended open."""
        p = FakeProvider(clock("2026-09-30T03:00:00-04:00", False))
        self.assertIs(MarketSessionService(p).current().session,
                      MarketSession.CLOSED)

    def test_after_extended_close_is_closed(self):
        """21:00 ET, past the 20:00 extended close."""
        p = FakeProvider(clock("2026-09-30T21:00:00-04:00", False))
        self.assertIs(MarketSessionService(p).current().session,
                      MarketSession.CLOSED)


class TestHolidaysAndEarlyCloses(unittest.TestCase):
    def test_holiday_is_detected_from_its_absence(self):
        """Thanksgiving has no calendar row at all."""
        p = FakeProvider(clock("2026-11-26T11:00:00-05:00", False),
                         calendar=THANKSGIVING_WEEK)
        r = MarketSessionService(p).current()

        self.assertIs(r.session, MarketSession.CLOSED)
        self.assertFalse(r.is_trading_day)

    def test_early_close_day_is_flagged(self):
        p = FakeProvider(clock("2026-11-27T11:00:00-05:00", True),
                         calendar=THANKSGIVING_WEEK)
        r = MarketSessionService(p).current()

        self.assertIs(r.session, MarketSession.OPEN)
        self.assertTrue(r.is_early_close)
        self.assertEqual(r.trading_day.regular_close.isoformat(), "13:00:00")

    def test_after_an_early_close_is_after_hours_not_open(self):
        """14:00 on a 13:00 close day. A hardcoded 16:00 assumption would
        report this as open."""
        p = FakeProvider(clock("2026-11-27T14:00:00-05:00", False),
                         calendar=THANKSGIVING_WEEK)
        r = MarketSessionService(p).current()

        self.assertIs(r.session, MarketSession.AFTER_HOURS)
        self.assertFalse(r.is_open)

    def test_weekend_is_closed(self):
        p = FakeProvider(clock("2026-10-03T12:00:00-04:00", False),
                         calendar=ORDINARY_WEEK)
        r = MarketSessionService(p).current()
        self.assertIs(r.session, MarketSession.CLOSED)
        self.assertFalse(r.is_trading_day)


class TestProviderFailure(unittest.TestCase):
    def test_clock_failure_yields_unknown_never_open(self):
        """Guessing 'probably open' from a weekday is the one failure mode
        that would let the agent act during a halt."""
        p = FakeProvider(clock_error=RuntimeError("api down"))
        r = MarketSessionService(p).current()

        self.assertIs(r.session, MarketSession.UNKNOWN)
        self.assertFalse(r.is_open)
        self.assertEqual(r.source, "unavailable")
        self.assertTrue(r.warnings)

    def test_missing_clock_payload_yields_unknown(self):
        r = MarketSessionService(FakeProvider(clock=None)).current()
        self.assertIs(r.session, MarketSession.UNKNOWN)

    def test_calendar_failure_degrades_but_still_answers(self):
        """Without the calendar, holiday detection is degraded; the clock
        still decides the regular session."""
        p = FakeProvider(clock("2026-09-30T12:00:00-04:00", True),
                         calendar_error=RuntimeError("calendar down"))
        r = MarketSessionService(p).current()

        self.assertIs(r.session, MarketSession.OPEN)
        self.assertTrue(any("calendar" in w or "clock" in w
                            for w in r.warnings))

    def test_clock_wins_when_it_contradicts_a_missing_calendar_row(self):
        """The clock is the broker's live answer; a calendar gap should not
        override it into CLOSED."""
        p = FakeProvider(clock("2026-11-26T11:00:00-05:00", True),
                         calendar=THANKSGIVING_WEEK)
        r = MarketSessionService(p).current()

        self.assertIs(r.session, MarketSession.OPEN)
        self.assertTrue(r.is_trading_day)
        self.assertTrue(any("absent from the calendar" in w for w in r.warnings))

    def test_halt_inside_regular_hours_is_not_reported_open(self):
        """Clock face says mid-session but the broker says not open."""
        p = FakeProvider(clock("2026-09-30T12:00:00-04:00", False))
        self.assertIs(MarketSessionService(p).current().session,
                      MarketSession.CLOSED)


class TestRequestDiscipline(unittest.TestCase):
    def test_calendar_is_fetched_once_per_day(self):
        p = FakeProvider(clock("2026-09-30T12:00:00-04:00", True))
        svc = MarketSessionService(p)
        for _ in range(5):
            svc.current()
        self.assertEqual(p.calendar_calls, 1,
                         f"calendar should be cached per day, got {p.calendar_calls}")
        self.assertEqual(p.clock_calls, 5, "the clock must stay live")


class TestTradingDayParsing(unittest.TestCase):
    def test_both_calendar_time_spellings(self):
        day = TradingDay.from_calendar_row(cal_row("2026-09-30"))
        self.assertEqual(day.regular_open.isoformat(), "09:30:00")
        self.assertEqual(day.extended_open.isoformat(), "04:00:00")
        self.assertEqual(day.extended_close.isoformat(), "20:00:00")

    def test_early_close_detection(self):
        self.assertTrue(TradingDay.from_calendar_row(
            cal_row("2026-11-27", close="13:00")).is_early_close)
        self.assertFalse(TradingDay.from_calendar_row(
            cal_row("2026-11-30")).is_early_close)

    def test_row_without_a_date_is_rejected(self):
        self.assertIsNone(TradingDay.from_calendar_row({"open": "09:30"}))

    def test_serialisation_includes_the_fields_operators_need(self):
        p = FakeProvider(clock("2026-09-30T12:00:00-04:00", True))
        d = MarketSessionService(p).current().as_dict()
        for key in ("session", "is_open", "is_trading_day", "is_early_close",
                    "regular_open", "regular_close", "next_open", "next_close",
                    "warnings", "source", "as_of"):
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
