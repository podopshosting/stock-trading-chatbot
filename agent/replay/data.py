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
