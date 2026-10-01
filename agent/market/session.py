"""
Market session detection.

Derived from the broker's clock and calendar, not from local server time
and hardcoded hours. Verified against the live Alpaca API on 2026-09-30:

  /v2/clock     {"is_open": true, "next_close": ..., "next_open": ...,
                 "timestamp": "2026-09-30T12:04:52-04:00"}

  /v2/calendar  {"date": "2026-11-27", "open": "09:30", "close": "13:00",
                 "session_open": "0400", "session_close": "1700",
                 "settlement_date": "2026-11-30"}

Two things follow from that shape:

* `is_open` covers the REGULAR session only. It is false during
  pre-market and after-hours, so pre/post cannot be read from the clock
  alone - the calendar's `session_open`/`session_close` are needed.
* Holidays are not flagged; they are simply **absent** from the calendar.
  Thanksgiving 2026-11-26 has no row at all, sitting between 11-25 and
  11-27. A date with no row is a non-trading day.

The early close is real data, not an assumption: the day after
Thanksgiving carries `close: "13:00"` and `session_close: "1700"`.

When the provider cannot be reached the answer is UNKNOWN, never OPEN.
Guessing "probably open" from a weekday would be the one failure mode that
lets the agent act during a halt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, time as time_cls, timedelta
from typing import Dict, List, Optional

from ..observability import log_event
from ..state.models import MarketSession

MARKET_TZ = "America/New_York"


def _market_zone(tz_name: str = MARKET_TZ):
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz_name)


def _parse_hhmm(raw: str) -> Optional[time_cls]:
    """Accept both calendar spellings: "09:30" and "0400"."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        if ":" in raw:
            hh, mm = raw.split(":", 1)
        elif len(raw) == 4:
            hh, mm = raw[:2], raw[2:]
        elif len(raw) == 3:
            hh, mm = raw[:1], raw[1:]
        else:
            return None
        return time_cls(int(hh), int(mm))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TradingDay:
    date: str
    regular_open: Optional[time_cls]
    regular_close: Optional[time_cls]
    extended_open: Optional[time_cls] = None
    extended_close: Optional[time_cls] = None

    @property
    def is_early_close(self) -> bool:
        """Shorter than the usual 16:00 regular close."""
        return bool(self.regular_close and self.regular_close < time_cls(16, 0))

    @classmethod
    def from_calendar_row(cls, row: Dict) -> Optional["TradingDay"]:
        day = row.get("date")
        if not day:
            return None
        return cls(
            date=day,
            regular_open=_parse_hhmm(row.get("open", "")),
            regular_close=_parse_hhmm(row.get("close", "")),
            extended_open=_parse_hhmm(row.get("session_open", "")),
            extended_close=_parse_hhmm(row.get("session_close", "")),
        )


@dataclass
class MarketSessionResult:
    session: MarketSession
    as_of: str
    is_trading_day: bool
    trading_day: Optional[TradingDay] = None
    next_open: Optional[str] = None
    next_close: Optional[str] = None
    is_early_close: bool = False
    warnings: List[str] = field(default_factory=list)
    source: str = "provider"

    @property
    def is_open(self) -> bool:
        """Regular session only. Pre-market and after-hours are not 'open'
        for the purposes of this agent, which does not trade extended
        hours."""
        return self.session is MarketSession.OPEN

    def _minutes_from_now_to(self, wall_time) -> Optional[float]:
        """Minutes from `as_of` to a market-time wall clock time today.

        None whenever it cannot be established - no trading day, no
        timestamp, an unparseable one. None, never a guess: callers
        treat an unknown time-to-close as "do not open positions", and a
        fabricated number would defeat that.
        """
        if wall_time is None or not self.as_of or not self.trading_day:
            return None
        try:
            zone = _market_zone()
            now = datetime.fromisoformat(
                str(self.as_of).replace("Z", "+00:00"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=zone)
            now = now.astimezone(zone)
            target = datetime.combine(now.date(), wall_time, tzinfo=zone)
            return (target - now).total_seconds() / 60.0
        except (TypeError, ValueError):
            return None

    @property
    def minutes_to_close(self) -> Optional[float]:
        """Minutes until the regular close. Negative after it."""
        if not self.trading_day:
            return None
        return self._minutes_from_now_to(self.trading_day.regular_close)

    @property
    def minutes_since_open(self) -> Optional[float]:
        """Minutes since the regular open. Negative before it."""
        if not self.trading_day or self.trading_day.regular_open is None:
            return None
        to_open = self._minutes_from_now_to(self.trading_day.regular_open)
        return None if to_open is None else -to_open

    def as_dict(self) -> Dict:
        return {
            "session": self.session.value,
            "as_of": self.as_of,
            "is_open": self.is_open,
            "is_trading_day": self.is_trading_day,
            "is_early_close": self.is_early_close,
            "minutes_to_close": self.minutes_to_close,
            "minutes_since_open": self.minutes_since_open,
            "regular_open": (self.trading_day.regular_open.isoformat()
                             if self.trading_day and self.trading_day.regular_open
                             else None),
            "regular_close": (self.trading_day.regular_close.isoformat()
                              if self.trading_day and self.trading_day.regular_close
                              else None),
            "next_open": self.next_open,
            "next_close": self.next_close,
            "warnings": list(self.warnings),
            "source": self.source,
        }


class MarketSessionService:
    """Resolves the current market session from the broker's own clock and
    calendar."""

    def __init__(self, provider, tz_name: str = MARKET_TZ,
                 calendar_lookahead_days: int = 7):
        self.provider = provider
        self.tz_name = tz_name
        self.calendar_lookahead_days = calendar_lookahead_days
        self._calendar_cache: Dict[str, TradingDay] = {}
        self._calendar_cached_for: Optional[str] = None

    # -- provider access --------------------------------------------------

    def _fetch_clock(self) -> Optional[Dict]:
        try:
            return self.provider.get_clock()
        except Exception as e:
            log_event("provider_error", operation="get_clock", error=str(e)[:200])
            return None

    def _fetch_calendar(self, start: str, end: str) -> Optional[List[Dict]]:
        try:
            return self.provider.get_calendar(start, end)
        except Exception as e:
            log_event("provider_error", operation="get_calendar",
                      error=str(e)[:200])
            return None

    def _calendar_for(self, today: date_cls) -> Dict[str, TradingDay]:
        """Cached per day: the calendar changes at most daily."""
        key = today.isoformat()
        if self._calendar_cached_for == key and self._calendar_cache:
            return self._calendar_cache

        start = (today - timedelta(days=4)).isoformat()
        end = (today + timedelta(days=self.calendar_lookahead_days)).isoformat()
        rows = self._fetch_calendar(start, end)
        if rows is None:
            return {}

        days: Dict[str, TradingDay] = {}
        for row in rows:
            day = TradingDay.from_calendar_row(row)
            if day is not None:
                days[day.date] = day

        self._calendar_cache = days
        self._calendar_cached_for = key
        return days

    # -- resolution -------------------------------------------------------

    def current(self) -> MarketSessionResult:
        clock = self._fetch_clock()

        if clock is None or "timestamp" not in clock:
            # No authoritative time. UNKNOWN, not a guess from a weekday.
            log_event("market_data_missing", input="market_clock")
            return MarketSessionResult(
                session=MarketSession.UNKNOWN,
                as_of="",
                is_trading_day=False,
                warnings=["market clock unavailable; session is UNKNOWN"],
                source="unavailable",
            )

        now = self._parse_provider_time(clock["timestamp"])
        today = now.date()
        calendar = self._calendar_for(today)
        day = calendar.get(today.isoformat())

        warnings: List[str] = []
        clock_is_open = bool(clock.get("is_open"))

        if day is None:
            # Absent from the calendar means a non-trading day. But the
            # clock is the more authoritative of the two, so if it claims
            # the regular session is open, trust it and say the calendar
            # looks wrong rather than silently contradicting reality.
            if clock_is_open:
                warnings.append(
                    f"{today.isoformat()} is absent from the calendar yet the "
                    "clock reports the market open; trusting the clock"
                )
                return MarketSessionResult(
                    session=MarketSession.OPEN, as_of=clock["timestamp"],
                    is_trading_day=True, next_open=clock.get("next_open"),
                    next_close=clock.get("next_close"), warnings=warnings,
                )
            if not calendar:
                warnings.append("calendar unavailable; holiday detection degraded")
            return MarketSessionResult(
                session=MarketSession.CLOSED, as_of=clock["timestamp"],
                is_trading_day=False, next_open=clock.get("next_open"),
                next_close=clock.get("next_close"), warnings=warnings,
            )

        session = self._classify(now.time(), day, clock_is_open)

        return MarketSessionResult(
            session=session,
            as_of=clock["timestamp"],
            is_trading_day=True,
            trading_day=day,
            next_open=clock.get("next_open"),
            next_close=clock.get("next_close"),
            is_early_close=day.is_early_close,
            warnings=warnings,
        )

    @staticmethod
    def _classify(now_time: time_cls, day: TradingDay,
                  clock_is_open: bool) -> MarketSession:
        """Place `now` within the day's windows.

        The clock's `is_open` decides the regular session, since it is the
        broker's own answer and accounts for halts. The calendar supplies
        the extended-hours boundaries the clock does not expose.
        """
        if clock_is_open:
            return MarketSession.OPEN

        # Not in the regular session: pre-market, after-hours, or closed.
        ext_open = day.extended_open
        ext_close = day.extended_close
        reg_open = day.regular_open
        reg_close = day.regular_close

        if reg_open and now_time < reg_open:
            if ext_open and now_time >= ext_open:
                return MarketSession.PRE_MARKET
            return MarketSession.CLOSED

        if reg_close and now_time >= reg_close:
            if ext_close and now_time < ext_close:
                return MarketSession.AFTER_HOURS
            return MarketSession.CLOSED

        # Inside regular hours by the clock face, yet the broker says the
        # regular session is not open - a halt, or the session has not
        # been opened. Do not report OPEN against the broker's own answer.
        return MarketSession.CLOSED

    def _parse_provider_time(self, raw: str) -> datetime:
        """Parse the provider timestamp and express it in market time.

        Alpaca sends nanosecond precision, which fromisoformat rejects on
        some versions, so the fractional part is trimmed to microseconds.
        """
        text = raw.strip()
        if "." in text:
            head, rest = text.split(".", 1)
            digits = ""
            for ch in rest:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            offset = rest[len(digits):]
            text = f"{head}.{digits[:6]}{offset}"
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(_market_zone(self.tz_name))
