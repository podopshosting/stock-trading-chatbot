"""Resolving the date in a question, and refusing to guess one.

"What happened yesterday?" asked on a Monday means Friday, because the
market was shut at the weekend and there is no session to report. A
chat answer that silently reports an empty Sunday reads as "nothing
happened", which is a different and much worse answer than "there was
no session".

WHAT THIS DOES NOT DO

It does not decide whether a date HAS data. That is the caller's job,
reading the structured records. This module only turns language into a
date, and says when it cannot.

AMBIGUITY IS REFUSED, NOT RESOLVED

"Thursday" with no other context could be the Thursday just gone or the
one coming. The past is the only one a record can exist for, so the
most recent past Thursday is chosen - and that choice is REPORTED in
the resolution so an answer can state which date it used. A date
silently chosen is a date the reader cannot check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class DateResolution:
    """A date, how it was arrived at, and whether it is a trading day."""
    resolved_date: Optional[str]
    phrase: Optional[str]
    basis: str
    is_trading_day: Optional[bool] = None
    note: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.resolved_date is not None

    def to_dict(self) -> dict:
        return {
            "resolved_date": self.resolved_date,
            "phrase": self.phrase,
            "basis": self.basis,
            "is_trading_day": self.is_trading_day,
            "note": self.note,
        }


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _previous_trading_day(d: date) -> date:
    """The last weekday at or before `d`.

    Weekends only. Market holidays are NOT handled here, and that
    limitation is reported rather than hidden: a holiday would resolve
    to a date with no session, and the caller's own "no session on this
    date" answer is then correct anyway.
    """
    while _is_weekend(d):
        d -= timedelta(days=1)
    return d


def resolve(query: str, today: Optional[date] = None,
            known_sessions: Optional[Sequence[str]] = None
            ) -> DateResolution:
    """The session date a question is about, or an unresolved answer.

    `known_sessions` are the dates for which records actually exist. If
    a resolved date is not among them, that is reported in the note -
    the resolution still stands, because "the Tuesday you asked about
    has no session recorded" is a better answer than silently moving to
    a Tuesday that does.
    """
    today = today or datetime.utcnow().date()
    q = (query or "").lower()

    def finish(d: date, phrase: str, basis: str,
               note: Optional[str] = None) -> DateResolution:
        iso = d.isoformat()
        extra = note
        if known_sessions is not None and iso not in known_sessions:
            missing = (f"no session is recorded for {iso}")
            extra = f"{note}; {missing}" if note else missing
        return DateResolution(resolved_date=iso, phrase=phrase,
                              basis=basis,
                              is_trading_day=not _is_weekend(d),
                              note=extra)

    # An explicit ISO date wins: the asker already did the work.
    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if iso_match:
        try:
            d = date(int(iso_match.group(1)), int(iso_match.group(2)),
                     int(iso_match.group(3)))
        except ValueError:
            return DateResolution(
                None, iso_match.group(0), UNRESOLVED,
                note=f"{iso_match.group(0)} is not a real date")
        return finish(d, iso_match.group(0), "EXPLICIT_ISO_DATE")

    # "October 1", "1 October", with an optional year.
    for name, number in MONTHS.items():
        m = re.search(rf"\b{name}\s+(\d{{1,2}})(?:,?\s*(\d{{4}}))?\b", q)
        if not m:
            m = re.search(rf"\b(\d{{1,2}})\s+{name}(?:,?\s*(\d{{4}}))?\b", q)
        if m:
            day = int(m.group(1))
            year = int(m.group(2)) if m.group(2) else today.year
            try:
                d = date(year, number, day)
            except ValueError:
                return DateResolution(
                    None, m.group(0), UNRESOLVED,
                    note=f"{m.group(0)} is not a real date")
            note = None
            if m.group(2) is None and d > today:
                # "October 1" asked in September means last year's, not
                # a date in the future that cannot have records.
                d = date(year - 1, number, day)
                note = (f"the year was not given and {name} {day} has not "
                        f"happened yet this year, so {d.year} was used")
            return finish(d, m.group(0), "EXPLICIT_MONTH_DAY", note)

    if "today" in q:
        return finish(today, "today", "TODAY")

    if "yesterday" in q:
        d = today - timedelta(days=1)
        if _is_weekend(d):
            adjusted = _previous_trading_day(d)
            return finish(
                adjusted, "yesterday", "YESTERDAY_ADJUSTED_TO_TRADING_DAY",
                f"{d.isoformat()} was a weekend with no session, so the "
                f"last trading day was used")
        return finish(d, "yesterday", "YESTERDAY")

    # "last Thursday" / "on Friday". Always the most recent PAST one:
    # a future weekday cannot have a record.
    for name, index in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", q):
            delta = (today.weekday() - index) % 7
            if delta == 0:
                delta = 7            # "Monday" asked on a Monday means
                                     # the previous Monday, not today
            d = today - timedelta(days=delta)
            return finish(
                d, name, "MOST_RECENT_PAST_WEEKDAY",
                f"{name} is ambiguous; the most recent past {name} was "
                f"used because a future date cannot have records")

    # "3 days ago", "2 sessions ago"
    ago = re.search(r"\b(\d{1,3})\s*(day|days|session|sessions)\s*ago\b", q)
    if ago:
        n = int(ago.group(1))
        if "session" in ago.group(2):
            # Count back over weekdays only, which is what "sessions"
            # means. Holidays are still not handled.
            d, counted = today, 0
            while counted < n:
                d -= timedelta(days=1)
                if not _is_weekend(d):
                    counted += 1
            return finish(d, ago.group(0), "N_SESSIONS_AGO")
        return finish(today - timedelta(days=n), ago.group(0), "N_DAYS_AGO")

    return DateResolution(
        None, None, UNRESOLVED,
        note=("no date was named in the question; say a date, 'yesterday', "
              "a weekday, or 'N sessions ago'"))


DATE_QUESTION_MARKERS = (
    "yesterday", "today", "last week", "ago", "happened", "on october",
    "what trades", "what orders", "what positions", "why didn't",
    "why did not", "why no trade",
)


def looks_like_a_date_question(query: str) -> bool:
    """Whether a question is ABOUT a past session.

    Used to decide whether to ground an answer in the dated records at
    all. Deliberately permissive: a false positive costs one extra read
    and still produces a grounded answer, while a false negative sends
    the question to a generic reply when the records held the answer.
    """
    q = (query or "").lower()
    if any(marker in q for marker in DATE_QUESTION_MARKERS):
        return True
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", q):
        return True
    if any(re.search(rf"\b{name}\b", q) for name in WEEKDAYS):
        return True
    return any(re.search(rf"\b{name}\s+\d{{1,2}}\b", q) for name in MONTHS)
