"""Dividend status and metrics from stored events. Pure and deterministic.

Rules: one payment never establishes a status; special dividends never
count as regular; missing data yields UNKNOWN; a yield always names the
price it used.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional, Sequence

from .models import DividendEvent, DividendProfile, DividendStatus


# A payment counts as "on cadence" within this fraction of the typical
# interval, and this fraction of payments must be on cadence for the
# schedule to count as regular. Calendar drift moves a quarterly ex-date
# by a few weeks; a cut or a restructuring moves it by months.
CADENCE_TOLERANCE = 0.35
CADENCE_MAJORITY = 0.8


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def classify(events: Optional[Sequence[DividendEvent]], today: date,
             history_complete: bool = False) -> DividendProfile:
    if events is None:
        return DividendProfile(DividendStatus.UNKNOWN, None,
                               "no dividend data retrieved")
    regular = sorted((e for e in events if e.kind == "REGULAR"),
                     key=lambda e: _d(e.ex_date))
    if not regular:
        if any(e.kind != "REGULAR" for e in events):
            return DividendProfile(
                DividendStatus.IRREGULAR, False,
                "only special/non-regular distributions on record")
        if history_complete:
            return DividendProfile(DividendStatus.NO_DIVIDEND, False,
                                   "complete history shows no dividends")
        # An empty list from a partial window is not evidence of absence.
        return DividendProfile(DividendStatus.UNKNOWN, None,
                               "no dividends seen, history not known complete")
    last = regular[-1]
    gap_days = (today - _d(last.ex_date)).days
    prof = DividendProfile(DividendStatus.UNKNOWN, None, "", last_regular=last)
    if len(regular) < 3:
        prof.reason = ("fewer than 3 regular payments; one payment does not "
                       "establish a pattern")
        prof.pays_dividend = True if gap_days <= 400 else None
        return prof
    gaps = [(_d(b.ex_date) - _d(a.ex_date)).days
            for a, b in zip(regular, regular[1:])]
    typical = sorted(gaps)[len(gaps) // 2]
    # Judged on how MOST payments are spaced, not on the worst pair.
    # max(gaps) - min(gaps) made two odd gaps out of 37 enough to call a
    # reliable quarterly payer irregular: verified live on COST (every
    # gap between 63 and 112 days, median 91) and on GE (36 of 39 gaps
    # near the median, yet reported IRREGULAR).
    near = sum(1 for g in gaps if abs(g - typical) <= CADENCE_TOLERANCE * typical)
    if near < CADENCE_MAJORITY * len(gaps):
        prof.status, prof.pays_dividend = DividendStatus.IRREGULAR, True
        prof.reason = (f"only {near} of {len(gaps)} intervals are near the "
                       f"typical {typical} days")
    elif gap_days > typical * 2 + 30:
        prof.status = DividendStatus.SUSPENDED
        prof.pays_dividend = False
        prof.reason = (f"no regular payment for {gap_days} days against a "
                       f"typical {typical}-day cadence")
    else:
        prof.status, prof.pays_dividend = DividendStatus.ACTIVE, True
        prof.reason = f"regular cadence of about {typical} days"
    return prof


def with_metrics(profile: DividendProfile, events: Sequence[DividendEvent],
                 today: date, price: Optional[float],
                 price_asof: Optional[str]) -> DividendProfile:
    cutoff = today - timedelta(days=365)
    ttm = [e for e in events if e.kind == "REGULAR"
           and cutoff < _d(e.ex_date) <= today]
    if ttm:
        profile.trailing_12m_amount = round(sum(e.amount for e in ttm), 6)
    if profile.trailing_12m_amount and price and price > 0:
        profile.trailing_yield_pct = round(
            100 * profile.trailing_12m_amount / price, 4)
        profile.yield_price_basis = f"price {price} as of {price_asof}"
    upcoming = sorted((e for e in events if _d(e.ex_date) >= today),
                      key=lambda e: _d(e.ex_date))
    if upcoming:
        profile.next_ex_date = upcoming[0].ex_date
        profile.days_until_ex = (_d(upcoming[0].ex_date) - today).days
    return profile
