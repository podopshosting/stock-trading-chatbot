"""What exactly is being predicted.

Every defect in a prediction system that matters is in this file or in
the feature timestamps, because both decide where "now" ends. A target
computed from a price the agent could not have traded at is not a
prediction, it is a measurement of the answer.

THE FILL BOUNDARY

A decision taken on bar N cannot transact at bar N's close - that price
is only known once the bar is over, and by then the opportunity is in
the past. The replay engine already models this: a decision on bar N
fills at bar N+1's open. Every target here measures from that SAME
price, so a prediction's subject is the trade the agent could actually
have had.

Measuring from bar N's close would make every target better and every
one of them fiction.

UNRESOLVED IS NOT ZERO

A 60-minute target on the last bar of a dataset has no answer yet. That
is UNRESOLVED - represented as None, carried as None, excluded from
scoring with a count, and never coerced to 0.0. A zero forward return
is a real and informative outcome (the price did not move); conflating
it with "we do not know" would teach a model that the end of a dataset
is a flat market.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

# Target names. Strings rather than an enum because they are stored and
# compared across process boundaries, and a renamed enum member would
# silently stop matching historical rows.
FORWARD_RETURN = "FORWARD_RETURN"
DIRECTION = "DIRECTION"
MAX_FAVORABLE_EXCURSION = "MAX_FAVORABLE_EXCURSION"
MAX_ADVERSE_EXCURSION = "MAX_ADVERSE_EXCURSION"
STOP_BEFORE_TARGET = "STOP_BEFORE_TARGET"

HORIZONS_MINUTES = (5, 15, 30, 60)

# Direction classes. FLAT is a real outcome, not a missing value: a
# price that moves less than the band is a fact about the market.
UP = "UP"
DOWN = "DOWN"
FLAT = "FLAT"

# Below this absolute move, direction is FLAT. Chosen as a tenth of the
# strategy's 3% stop distance: a move that small cannot produce a trade
# outcome either way, so calling it UP would be labelling noise.
DIRECTION_FLAT_BAND_PCT = 0.30

# The REPLAY ENGINE's fixed stop and target distances.
#
# Named for their provenance, because they are not the live agent's. The
# live hypothesis engine derives a stop from realised volatility -
# `suggest_stop_distance()` returns somewhere between
# MIN_STOP_DISTANCE_PCT and MAX_STOP_DISTANCE_PCT, currently 1% to 8% -
# so a live trade's stop depends on the symbol and the session. The
# replay engine applies a flat 3%.
#
# That difference is not cosmetic. Any R-multiple measured against 3%
# describes the simulator's strategy, not the deployed one, and a
# conclusion drawn from it transfers only to the extent the volatility
# happened to imply 3%. `stop_before_target()` therefore accepts the
# distance, and callers predicting about LIVE trades must pass the
# hypothesis's own `suggested_stop_distance_pct` rather than let these
# defaults answer for it.
#
# Mirrored rather than imported: `agent.replay` imports the risk and
# hypothesis packages, so importing it here would drag the whole
# decision path into a research module - and the structural guard only
# reads literal import lines, so it would pass while the coupling was
# real. Tests assert the mirror matches, which is the check that cannot
# be satisfied vacuously.
REPLAY_STOP_DISTANCE_PCT = 3.0
REPLAY_TARGET_DISTANCE_PCT = 6.0

# Retained aliases so a caller cannot read `STOP_DISTANCE_PCT` and
# believe it is the live agent's.
STOP_DISTANCE_PCT = REPLAY_STOP_DISTANCE_PCT
TARGET_DISTANCE_PCT = REPLAY_TARGET_DISTANCE_PCT

# Resolution outcomes for STOP_BEFORE_TARGET. Four values, not a
# boolean, because "neither was reached" and "we cannot tell" are
# different facts and a boolean would have to lie about one of them.
STOP_FIRST = "STOP_FIRST"
TARGET_FIRST = "TARGET_FIRST"
NEITHER = "NEITHER"
AMBIGUOUS_SAME_BAR = "AMBIGUOUS_SAME_BAR"


@dataclass(frozen=True)
class Target:
    """One resolved or unresolved target.

    `value is None` means UNRESOLVED and `reason` says why. Frozen so a
    resolved target cannot be edited after the fact - a label that can
    be rewritten is not evidence.
    """
    name: str
    horizon_minutes: Optional[int]
    value: Optional[Any]
    reason: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "horizon_minutes": self.horizon_minutes,
            "value": self.value,
            "resolved": self.resolved,
            "reason": self.reason,
        }


def _price(bar: Any, field: str) -> Optional[float]:
    """Read a price off a bar, tolerating object or mapping, refusing
    to invent one.

    Returns None for an absent field rather than 0.0. A bar with no
    high is unusable; a bar with a high of zero is a different and much
    stranger claim.
    """
    if isinstance(bar, dict):
        raw = bar.get(field)
    else:
        raw = getattr(bar, field, None)
    if raw is None:
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    # A non-positive price is not a price. Equities do not trade at or
    # below zero, so this is corrupt input rather than a market event,
    # and letting it through would produce an infinite return.
    return out if out > 0 else None


def entry_price(bars: Sequence[Any], decision_index: int) -> Optional[float]:
    """The price a decision at `decision_index` could have filled at.

    The next bar's open, matching the replay engine. None when the
    decision is on the final bar: there is no next open, so there is no
    tradeable price and therefore nothing to predict about.
    """
    nxt = decision_index + 1
    if nxt >= len(bars) or decision_index < 0:
        return None
    return _price(bars[nxt], "open")


def _bars_for_horizon(bars: Sequence[Any], decision_index: int,
                      horizon_minutes: int,
                      bar_interval_seconds: float) -> Optional[List[Any]]:
    """The bars that fall inside the horizon, starting at the fill bar.

    None when the horizon runs past the data. Returning a short window
    instead would silently relabel a 60-minute target as whatever
    happened to be available, which is the quietest possible way to
    make a model look good on recent data.
    """
    if bar_interval_seconds <= 0:
        return None
    needed = int(round(horizon_minutes * 60.0 / bar_interval_seconds))
    if needed < 1:
        # A horizon shorter than one bar cannot be measured at this
        # resolution. Rounding it up to one bar would answer a
        # different question than the one asked.
        return None
    start = decision_index + 1
    end = start + needed
    if start >= len(bars) or end > len(bars):
        return None
    return list(bars[start:end])


def forward_return(bars: Sequence[Any], decision_index: int,
                   horizon_minutes: int,
                   bar_interval_seconds: float = 60.0) -> Target:
    """Percent return from the fill price to the close of the horizon."""
    t = lambda v, r=None: Target(  # noqa: E731
        FORWARD_RETURN, horizon_minutes, v, r)

    entry = entry_price(bars, decision_index)
    if entry is None:
        return t(None, "NO_TRADEABLE_ENTRY_PRICE")
    window = _bars_for_horizon(bars, decision_index, horizon_minutes,
                               bar_interval_seconds)
    if window is None:
        return t(None, "HORIZON_EXCEEDS_AVAILABLE_DATA")
    close = _price(window[-1], "close")
    if close is None:
        return t(None, "HORIZON_END_BAR_HAS_NO_CLOSE")
    return t(round((close - entry) / entry * 100.0, 6))


def direction(bars: Sequence[Any], decision_index: int,
              horizon_minutes: int,
              bar_interval_seconds: float = 60.0,
              flat_band_pct: float = DIRECTION_FLAT_BAND_PCT) -> Target:
    """UP, DOWN or FLAT over the horizon.

    Derived from forward_return so the two can never disagree about the
    same window. Computing it independently would create two sources of
    truth about one fact.
    """
    fr = forward_return(bars, decision_index, horizon_minutes,
                        bar_interval_seconds)
    if not fr.resolved:
        return Target(DIRECTION, horizon_minutes, None, fr.reason)
    pct = float(fr.value)
    if abs(pct) < flat_band_pct:
        label = FLAT
    else:
        label = UP if pct > 0 else DOWN
    return Target(DIRECTION, horizon_minutes, label)


def excursions(bars: Sequence[Any], decision_index: int,
               horizon_minutes: int,
               bar_interval_seconds: float = 60.0) -> tuple:
    """Best and worst percentage moves reached during the horizon.

    MFE from the highs, MAE from the lows, both relative to the fill
    price. These bound what any stop or target could have achieved, so
    they answer "was the move available" separately from "did the close
    capture it".

    MFE is floored at zero and MAE capped at zero: an excursion is how
    far price went in that direction, and a negative favorable
    excursion is not a concept.
    """
    name_f, name_a = MAX_FAVORABLE_EXCURSION, MAX_ADVERSE_EXCURSION
    entry = entry_price(bars, decision_index)
    if entry is None:
        r = "NO_TRADEABLE_ENTRY_PRICE"
        return (Target(name_f, horizon_minutes, None, r),
                Target(name_a, horizon_minutes, None, r))
    window = _bars_for_horizon(bars, decision_index, horizon_minutes,
                               bar_interval_seconds)
    if window is None:
        r = "HORIZON_EXCEEDS_AVAILABLE_DATA"
        return (Target(name_f, horizon_minutes, None, r),
                Target(name_a, horizon_minutes, None, r))

    highs = [_price(b, "high") for b in window]
    lows = [_price(b, "low") for b in window]
    # One unusable bar inside the window makes the extreme unknowable,
    # not merely less precise: the missing bar could contain the high.
    if any(h is None for h in highs) or any(l is None for l in lows):
        r = "INCOMPLETE_BARS_IN_HORIZON"
        return (Target(name_f, horizon_minutes, None, r),
                Target(name_a, horizon_minutes, None, r))

    mfe = max(0.0, (max(highs) - entry) / entry * 100.0)
    mae = min(0.0, (min(lows) - entry) / entry * 100.0)
    return (Target(name_f, horizon_minutes, round(mfe, 6)),
            Target(name_a, horizon_minutes, round(mae, 6)))


def stop_before_target(bars: Sequence[Any], decision_index: int,
                       horizon_minutes: Optional[int] = None,
                       bar_interval_seconds: float = 60.0,
                       stop_pct: float = REPLAY_STOP_DISTANCE_PCT,
                       target_pct: float = REPLAY_TARGET_DISTANCE_PCT) -> Target:
    """Which of the stop and the target was reached first.

    The target the strategy actually cares about: of the two exits that
    end a trade, which one arrives. Walked bar by bar rather than
    compared on extremes, because `max(high) >= target` and
    `min(low) <= stop` can both be true across a window while only one
    of them happened first.

    A bar whose range spans BOTH levels returns AMBIGUOUS_SAME_BAR. At
    this resolution the order inside that bar is genuinely unknown, and
    both available guesses are wrong in a direction that matters:
    assuming the target first flatters the strategy, assuming the stop
    first slanders it. So it is neither, and it is counted.

    `horizon_minutes=None` walks to the end of the data, which is the
    honest default for a question with no natural deadline.

    The default distances are the REPLAY engine's fixed 3% and 6%. The
    live agent's stop is volatility-derived and usually neither, so a
    prediction about a live trade must pass that trade's own stop.
    """
    name = STOP_BEFORE_TARGET
    entry = entry_price(bars, decision_index)
    if entry is None:
        return Target(name, horizon_minutes, None,
                      "NO_TRADEABLE_ENTRY_PRICE")

    if horizon_minutes is None:
        window = list(bars[decision_index + 1:])
        if not window:
            return Target(name, None, None, "NO_BARS_AFTER_ENTRY")
    else:
        window = _bars_for_horizon(bars, decision_index, horizon_minutes,
                                   bar_interval_seconds)
        if window is None:
            return Target(name, horizon_minutes, None,
                          "HORIZON_EXCEEDS_AVAILABLE_DATA")

    stop_level = entry * (1.0 - stop_pct / 100.0)
    target_level = entry * (1.0 + target_pct / 100.0)

    for bar in window:
        high, low = _price(bar, "high"), _price(bar, "low")
        if high is None or low is None:
            # Stop at the gap rather than stepping over it. A level
            # might have been touched inside the bar we cannot read, so
            # anything found later is not provably first.
            return Target(name, horizon_minutes, None,
                          "INCOMPLETE_BAR_BEFORE_RESOLUTION")
        hit_stop = low <= stop_level
        hit_target = high >= target_level
        if hit_stop and hit_target:
            return Target(name, horizon_minutes, AMBIGUOUS_SAME_BAR)
        if hit_stop:
            return Target(name, horizon_minutes, STOP_FIRST)
        if hit_target:
            return Target(name, horizon_minutes, TARGET_FIRST)
    return Target(name, horizon_minutes, NEITHER)


def resolve_all(bars: Sequence[Any], decision_index: int,
                bar_interval_seconds: float = 60.0,
                horizons: Sequence[int] = HORIZONS_MINUTES) -> List[Target]:
    """Every target for one decision point, resolved or not.

    Unresolved targets are returned rather than dropped, so a caller
    can see that a target was ASKED FOR and could not be answered. A
    list that silently omits them makes "no 60-minute label" look
    identical to "nobody wanted one".
    """
    out: List[Target] = []
    for h in horizons:
        out.append(forward_return(bars, decision_index, h,
                                  bar_interval_seconds))
        out.append(direction(bars, decision_index, h, bar_interval_seconds))
        mfe, mae = excursions(bars, decision_index, h, bar_interval_seconds)
        out.extend((mfe, mae))
    out.append(stop_before_target(bars, decision_index, None,
                                  bar_interval_seconds))
    return out
