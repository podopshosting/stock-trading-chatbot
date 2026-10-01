"""Dividend status and metrics from stored events. Pure and deterministic.

Rules: one payment never establishes a status; special dividends never
count as regular; missing data yields UNKNOWN; a yield always names the
price it used.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

from .models import (DividendEvent, DividendProfile, DividendStatus,
                     SplitEvent)


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
        profile.next_ex_note = (
            "declared in the corporate-actions feed; a relayed claim, not "
            "a measurement - companies do move these dates")
    elif profile.pays_dividend:
        # Deliberately not inferred from the payment cadence. A payer on a
        # 91-day rhythm makes the next date easy to guess, and a guess
        # presented in the same field as a declared date is worse than a
        # null, because a reader cannot tell which they were given.
        profile.next_ex_note = (
            "no upcoming ex-dividend date has been declared in the "
            "corporate-actions feed; companies usually declare one quarter "
            "at a time, so this means 'not yet announced' rather than "
            "'none coming'. It is never inferred from the payment cadence")
    else:
        profile.next_ex_note = (
            "no upcoming ex-dividend date, and none expected: no regular "
            "dividend was found")
    return profile


# Growth windows, in years. 1 is a simple change; longer spans are
# annualised so they are comparable with each other.
GROWTH_YEARS = (1, 3, 5)
# A prior window must hold a comparable number of payments. A payer that
# moved from four payments to three in a window has not cut its dividend
# by a quarter; the window merely clipped a payment, and reporting that
# as growth would be an artefact of the boundary rather than a fact.
MIN_PAYMENT_RATIO = 0.75


def _split_factor(splits: Sequence[SplitEvent], after: str) -> float:
    """Cumulative split ratio applied strictly after `after`.

    Historical dividends are per-share amounts as declared, in the share
    count of their own day. Comparing them with today's amount without
    this restatement turns Apple's 4-for-1 split into a 75% dividend cut.
    """
    factor = 1.0
    for s in splits or ():
        if s.ex_date > after:
            factor *= s.ratio
    return factor


def _window_sum(events: Sequence[DividendEvent],
                splits: Sequence[SplitEvent],
                start: date, end: date) -> tuple:
    total, count = 0.0, 0
    for e in events:
        if e.kind != "REGULAR":
            continue
        d = _d(e.ex_date)
        if start < d <= end:
            total += e.amount / _split_factor(splits, e.ex_date)
            count += 1
    return round(total, 6), count


def growth(events: Optional[Sequence[DividendEvent]],
           splits: Optional[Sequence[SplitEvent]],
           today: date) -> Dict:
    """Dividend growth over 1, 3 and 5 years, in current share terms.

    Every span reports either a number or a reason it is unknown. A span
    the history does not reach is UNKNOWN, never zero: a company with
    four years of data has not held its dividend flat for five.
    """
    events = list(events or ())
    splits = list(splits or ())
    regular = [e for e in events if e.kind == "REGULAR"]
    out: Dict = {"split_adjusted": True, "spans": {}}
    if not regular:
        out["note"] = "no regular dividends, so growth is not defined"
        for n in GROWTH_YEARS:
            out["spans"][f"{n}y"] = {"growth_pct": None,
                                     "reason": "no regular dividends"}
        return out

    earliest = min(_d(e.ex_date) for e in regular)
    now_total, now_count = _window_sum(events, splits,
                                       today - timedelta(days=365), today)
    out["trailing_12m_adjusted"] = now_total

    for n in GROWTH_YEARS:
        key = f"{n}y"
        span = {"growth_pct": None, "annualised": n > 1}
        prior_end = today - timedelta(days=365 * n)
        prior_start = prior_end - timedelta(days=365)
        if earliest > prior_start:
            span["reason"] = (
                f"history begins {earliest.isoformat()}, which does not "
                f"cover the 12 months to {prior_end.isoformat()}")
            out["spans"][key] = span
            continue
        then_total, then_count = _window_sum(events, splits,
                                             prior_start, prior_end)
        if not then_total or not now_total:
            span["reason"] = "one of the two windows holds no payment"
            out["spans"][key] = span
            continue
        if now_count and then_count < now_count * MIN_PAYMENT_RATIO:
            span["reason"] = (
                f"the earlier window holds {then_count} payment(s) against "
                f"{now_count} now, so a comparison would measure the "
                f"window boundary rather than the dividend")
            out["spans"][key] = span
            continue
        ratio = now_total / then_total
        pct = (ratio ** (1.0 / n) - 1.0) * 100 if n > 1 else (ratio - 1) * 100
        span.update({"growth_pct": round(pct, 4),
                     "from_amount": then_total, "to_amount": now_total,
                     "from_window": f"{prior_start.isoformat()}..{prior_end.isoformat()}",
                     "payments": {"then": then_count, "now": now_count}})
        out["spans"][key] = span

    if splits:
        out["note"] = (
            f"{len(splits)} split(s) in the window; historical amounts are "
            "restated into current share terms, without which a 4-for-1 "
            "split reads as a 75% cut")
    return out
