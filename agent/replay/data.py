"""
Point-in-time data access.

A bar series and an evidence feed that physically cannot return
anything the clock has not reached. The strategy code under replay is
handed one of these instead of a live data service, and is otherwise
unchanged - which is the point. Replay must exercise the same code that
runs live, or it is testing a different system than the one that will
trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from .clock import LookaheadError, ReplayClock


@dataclass(frozen=True)
class Bar:
    """One period of price data.

    Immutable, because a replay that can alter history is not a replay.
    """
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

    def as_dict(self) -> Dict:
        return {"timestamp": self.timestamp, "open": self.open,
                "high": self.high, "low": self.low, "close": self.close,
                "volume": self.volume}


class PointInTimeSeries:
    """A bar series gated by a clock.

    The important method is `closes_through_now`, which is what the
    signal engine consumes. It returns bars up to and including the
    current one, because at a bar's close its own close is known.

    What must NOT happen is filling an order at that same close. Knowing
    the close and transacting at it is the classic backtest inflation:
    the decision uses information from the instant of the fill. Fills go
    through `next_open` instead, so an order decided on bar N executes
    on bar N+1.
    """

    def __init__(self, symbol: str, bars: Sequence[Bar], clock: ReplayClock,
                 require_adjusted: bool = True):
        self.symbol = symbol
        self._bars: List[Bar] = list(bars)
        self._clock = clock
        self._validate_ordering()
        self.discontinuities = find_discontinuities(self._bars)
        if require_adjusted:
            self._refuse_unadjusted_splits()

    def _refuse_unadjusted_splits(self) -> None:
        """Refuse to run over a price gap shaped like a split.

        Fail closed, and only for split-shaped gaps. An unexplained 40%
        fall is left alone: it may be exactly the event the run exists to
        study, and raising on it would push callers to disable the check
        altogether - which would also disable it for the real splits.
        """
        suspect = [d for d in self.discontinuities
                   if d["matches_plausible_split"]]
        if not suspect:
            return
        first = suspect[0]
        raise UnadjustedCorporateAction(
            f"{self.symbol} has {len(suspect)} price gap(s) consistent with "
            f"an unadjusted split: at {first['at']} price moved "
            f"{first['change_pct']}%, implying a "
            f"{first['nearest_ratio']}-for-1. Adjust the series with "
            f"adjust_bars_for_splits() before replaying it; a replay over "
            f"an unadjusted split measures a crash that never happened.")

    def _validate_ordering(self) -> None:
        """Out-of-order bars would silently defeat the clock."""
        for earlier, later in zip(self._bars, self._bars[1:]):
            if later.timestamp <= earlier.timestamp:
                raise ValueError(
                    f"{self.symbol} bars are not strictly increasing in "
                    f"time: {earlier.timestamp} then {later.timestamp}")

    def __len__(self) -> int:
        return len(self._bars)

    @property
    def all_timestamps(self) -> List[str]:
        """Used by the driver to schedule the run, not by strategy code."""
        return [b.timestamp for b in self._bars]

    def current(self) -> Bar:
        self._clock.assert_visible(self._clock.index,
                                   f"{self.symbol} current bar")
        if self._clock.index >= len(self._bars):
            raise LookaheadError(
                f"{self.symbol} has no bar at index {self._clock.index}")
        return self._bars[self._clock.index]

    def bar(self, index: int) -> Bar:
        self._clock.assert_visible(index, f"{self.symbol} bar")
        return self._bars[index]

    def closes_through_now(self, lookback: Optional[int] = None
                           ) -> List[float]:
        """Closing prices up to and including the current bar."""
        self._clock.assert_visible(self._clock.index,
                                   f"{self.symbol} closes")
        end = min(self._clock.index + 1, len(self._bars))
        window = self._bars[:end]
        if lookback is not None:
            window = window[-lookback:]
        return [b.close for b in window]

    def bars_through_now(self, lookback: Optional[int] = None) -> List[Bar]:
        self._clock.assert_visible(self._clock.index,
                                   f"{self.symbol} bars")
        end = min(self._clock.index + 1, len(self._bars))
        window = self._bars[:end]
        if lookback is not None:
            window = window[-lookback:]
        return list(window)

    def next_open(self) -> Optional[float]:
        """The price an order decided now would actually transact at.

        Deliberately the ONLY way to obtain a future price, and it
        returns a single number rather than a bar, so a caller cannot
        reach the next bar's high or low and size a position against
        information it will not have.

        None at the end of the series: an order decided on the final bar
        has nothing to fill against, and inventing a fill there would
        silently add a trade that could not have happened.
        """
        nxt = self._clock.index + 1
        if nxt >= len(self._bars):
            return None
        return self._bars[nxt].open

    def next_bar_range(self) -> Optional[Dict]:
        """High and low of the next bar, for stop-fill modelling only.

        Used by the replay broker to decide whether a stop would have
        been hit, which requires knowing where price went. Strategy code
        must never call this - it is what separates "the stop triggered"
        from "the strategy knew the range in advance".
        """
        nxt = self._clock.index + 1
        if nxt >= len(self._bars):
            return None
        bar = self._bars[nxt]
        return {"open": bar.open, "high": bar.high, "low": bar.low,
                "close": bar.close, "timestamp": bar.timestamp}

    def final_close(self) -> Optional[float]:
        """For valuing a position at the end of a run, not for deciding."""
        return self._bars[-1].close if self._bars else None


class PointInTimeEvidence:
    """An evidence feed gated by publication time.

    News dated 14:30 must not be visible to a decision made at 14:00.
    This is where lookahead is easiest to introduce by accident, because
    a headline feels like context rather than data.
    """

    def __init__(self, items: Iterable[Dict], clock: ReplayClock,
                 timestamp_key: str = "published_at"):
        self._items = list(items)
        self._clock = clock
        self._key = timestamp_key
        self._undated = [i for i in self._items if not i.get(timestamp_key)]

    @property
    def undated_count(self) -> int:
        """Items with no publication time.

        These are never served. Reported so a run can say how much of
        its evidence had to be discarded rather than quietly proceeding
        on a thinner feed than expected.
        """
        return len(self._undated)

    def visible(self, symbol: Optional[str] = None) -> List[Dict]:
        items = [i for i in self._items if self._clock.is_visible(
            i.get(self._key))]
        if symbol is not None:
            items = [i for i in items if i.get("symbol") in (None, symbol)]
        return items

    def visible_count(self) -> int:
        return len(self.visible())


# --- corporate actions -----------------------------------------------
#
# A replay over an unadjusted split is not a pessimistic replay, it is a
# meaningless one. NVIDIA's 10-for-1 shows up as an 90% overnight fall:
# every momentum rule reads a crash, every stop fires, and the run
# produces confident numbers about an event that never happened.
#
# The same arithmetic as the dividend-growth adjustment, applied to
# prices instead of payments.

# A gap must be this large before a split is even considered, so ordinary
# volatility is never "explained" by a corporate action.
DISCONTINUITY_THRESHOLD = 0.25
# Ratios a real split plausibly takes. A gap matching none of these is
# reported as unexplained rather than quietly adjusted away: an
# unexplained 40% gap may be real, and inventing a split to smooth it
# would erase the very event worth studying.
PLAUSIBLE_SPLIT_RATIOS = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20,
                          1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 1 / 20)
RATIO_TOLERANCE = 0.08


class UnadjustedCorporateAction(Exception):
    """Raised when a series contains a price gap consistent with a split.

    Deliberately an exception and not a warning. A warning in a log is
    not read by the person reading the equity curve six weeks later.
    """


def adjust_bars_for_splits(bars: Sequence[Bar],
                           splits: Sequence[Dict]) -> List[Bar]:
    """Back-adjust prices and volumes for splits.

    Prices before a split are divided by the ratio and volumes
    multiplied by it, so the series is expressed throughout in the share
    terms of its final bar. `splits` are dicts of {ex_date, ratio}.
    """
    out: List[Bar] = []
    for bar in bars:
        factor = 1.0
        for s in splits or ():
            ex = str(s.get("ex_date") or "")
            if ex and bar.timestamp < ex:
                factor *= float(s["ratio"])
        if factor == 1.0:
            out.append(bar)
            continue
        out.append(Bar(timestamp=bar.timestamp,
                       open=bar.open / factor, high=bar.high / factor,
                       low=bar.low / factor, close=bar.close / factor,
                       volume=bar.volume * factor))
    return out


def find_discontinuities(bars: Sequence[Bar]) -> List[Dict]:
    """Price gaps between one bar's close and the next bar's open.

    Reports every large gap, and says whether it matches a plausible
    split ratio. Judgement about what to do is left to the caller, since
    "suspicious" and "proven" are different claims.
    """
    found: List[Dict] = []
    for earlier, later in zip(bars, bars[1:]):
        if earlier.close <= 0 or later.open <= 0:
            continue
        ratio = later.open / earlier.close
        if abs(ratio - 1.0) < DISCONTINUITY_THRESHOLD:
            continue
        implied = 1.0 / ratio
        match = next((r for r in PLAUSIBLE_SPLIT_RATIOS
                      if abs(implied - r) <= RATIO_TOLERANCE * r), None)
        found.append({
            "at": later.timestamp, "previous_close": earlier.close,
            "next_open": later.open, "change_pct": round((ratio - 1) * 100, 4),
            "implied_split_ratio": round(implied, 4),
            "matches_plausible_split": match is not None,
            "nearest_ratio": match,
        })
    return found


class PointInTimeFundamentals:
    """Company facts gated by FILING date, never by period end.

    This is the subtlest leak available to a replay. A quarter ending
    27 June is not public on 27 June; it is filed weeks later. Gating on
    the period end hands the strategy five or six weeks of hindsight
    about results nobody had yet, and because the data is genuinely
    historical the run looks impeccable.

    Company Intelligence measures freshness from the period end, which
    is right for "how stale is this view of the company" and wrong for
    "was this knowable then". Both questions are legitimate; using one
    answer for the other is the bug.
    """

    def __init__(self, facts: Iterable[Dict], clock: ReplayClock,
                 filed_key: str = "filed"):
        self._facts = list(facts)
        self._clock = clock
        self._key = filed_key
        # A fact with no filing date cannot be shown to have been
        # knowable. Served never, counted always, so a run can report
        # how much of its fundamental data it had to discard rather than
        # quietly proceeding on a thinner basis than intended.
        self._undated = [f for f in self._facts if not f.get(filed_key)]

    @property
    def undated_count(self) -> int:
        return len(self._undated)

    @property
    def total_count(self) -> int:
        return len(self._facts)

    def visible(self, concept: Optional[str] = None) -> List[Dict]:
        # A clock with no timestamp treats everything as visible, which
        # is reasonable for a clock and catastrophic here: it would serve
        # every filing ever made. Fail closed instead - an ungated feed
        # is the leak this class exists to prevent.
        if self._clock.timestamp is None:
            return []
        out = [f for f in self._facts
               if f.get(self._key) and self._clock.is_visible(f[self._key])]
        if concept is not None:
            out = [f for f in out if f.get("concept") == concept]
        return out

    def latest(self, concept: Optional[str] = None) -> Optional[Dict]:
        """The most recently FILED fact that was public by now.

        Ordered by filing date, then period end. Ordering by period end
        alone would let a restatement filed later but covering an earlier
        quarter displace the figure actually in front of the market.
        """
        rows = self.visible(concept)
        if not rows:
            return None
        return max(rows, key=lambda f: (f.get(self._key) or "",
                                        f.get("period_end") or ""))

    def withheld(self) -> List[Dict]:
        """Facts that exist but were not yet public. For reporting."""
        return [f for f in self._facts
                if f.get(self._key)
                and not self._clock.is_visible(f[self._key])]

    def coverage(self) -> Dict:
        visible = self.visible()
        return {"total": len(self._facts), "visible": len(visible),
                "withheld_not_yet_filed": len(self.withheld()),
                "discarded_no_filing_date": self.undated_count,
                "gated_on": self._key,
                "note": ("gated on filing date, not period end: a quarter "
                         "is not public on the day it ends")}


# --- survivorship ----------------------------------------------------
#
# A universe drawn from the symbols that exist today cannot contain the
# ones that failed. Replaying 2020 against the S&P 500 as constituted in
# 2026 is a backtest of the companies that made it, which is not a
# strategy result - it is a reading of the selection.
#
# This cannot be fixed by code: the missing symbols are missing. It can
# only be disclosed, so a result is never read as cleaner than its
# universe. Hence a declaration that the caller must make, and a verdict
# carried alongside the numbers.

SURVIVORSHIP_UNKNOWN = "UNKNOWN"
SURVIVORSHIP_POINT_IN_TIME = "POINT_IN_TIME"
SURVIVORSHIP_BIASED = "SURVIVORSHIP_BIASED"


@dataclass(frozen=True)
class Universe:
    """The symbols a run may consider, and what is known about them.

    `as_of_listing_date` is the declaration that matters: True means the
    membership was taken as it stood at the start of the replay window,
    including names since delisted. False means it was taken later, so
    the failures are absent.
    """
    symbols: Sequence[str]
    as_of_listing_date: Optional[bool] = None
    delisted: Sequence[str] = field(default_factory=tuple)
    source: Optional[str] = None

    @property
    def survivorship(self) -> str:
        """Derived, with no setter, so it cannot be overridden to look
        better than the declaration supports."""
        if self.as_of_listing_date is None:
            return SURVIVORSHIP_UNKNOWN
        if not self.as_of_listing_date:
            return SURVIVORSHIP_BIASED
        return SURVIVORSHIP_POINT_IN_TIME

    @property
    def is_biased(self) -> bool:
        """UNKNOWN counts as biased. Not knowing how a universe was
        built is not the same as knowing it was built correctly, and the
        consequence for reading the result is identical."""
        return self.survivorship != SURVIVORSHIP_POINT_IN_TIME

    def as_dict(self) -> Dict:
        return {
            "symbols": list(self.symbols),
            "symbol_count": len(self.symbols),
            "delisted_included": list(self.delisted),
            "survivorship": self.survivorship,
            "is_biased": self.is_biased,
            "source": self.source,
            "caveat": self._caveat(),
        }

    def _caveat(self) -> str:
        if self.survivorship == SURVIVORSHIP_POINT_IN_TIME:
            return (
                f"membership as at the start of the window, including "
                f"{len(self.delisted)} name(s) since delisted; results are "
                f"not inflated by selection")
        if self.survivorship == SURVIVORSHIP_BIASED:
            return (
                "membership taken after the window, so companies that "
                "failed are absent; returns are biased upward by an "
                "unknown amount and are not comparable with a "
                "point-in-time run")
        return (
            "how this universe was constructed was not declared, so "
            "whether it excludes failed companies is unknown; treat it as "
            "biased, because not knowing is not the same as being right")
