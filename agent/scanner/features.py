"""
Scanner feature calculation.

Deliberately few features: this is a funnel that decides where to look,
not the Signal Engine. Pure functions over normalised data — no provider
types, no network.

**On relative volume.** Comparing a partial session's volume against a
full-day average is not a ratio of comparable quantities, and calling it
"relative volume" would be a quiet lie. What is computed here is:

    projected_full_session_volume / 20-day average daily volume

where the projection divides observed session volume by the fraction of
the regular session elapsed. That denominator is stated in
`relative_volume_basis` on every record. It is *not* a same-time-of-day
comparison, which would need intraday volume history this system does not
collect yet. Early in the session the projection is noisy, and the
docs say so.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .models import ScannerFeatures, ScannerSnapshot

RELATIVE_VOLUME_BASIS = (
    "projected_full_session_volume / 20d_average_daily_volume; "
    "projection = session_volume / elapsed_session_fraction. "
    "NOT a same-time-of-day comparison."
)

REGULAR_SESSION_MINUTES = 390.0       # 09:30-16:00 ET


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base <= 0:
        return None
    return (current - base) / base * 100.0


def _return_over(bars: List, minutes: int, timeframe_minutes: int,
                 current_price: Optional[float]) -> Optional[float]:
    """Percentage return over the last `minutes`, from oldest-first bars."""
    if current_price is None or not bars:
        return None
    steps = max(1, int(round(minutes / timeframe_minutes)))
    if len(bars) < steps + 1:
        return None
    base = bars[-(steps + 1)].close
    return _pct_change(current_price, base)


def elapsed_session_fraction(minutes_elapsed: Optional[float]) -> Optional[float]:
    """Fraction of the regular session completed, clamped to (0, 1].

    Returns None when unknown: projecting from an unknown elapsed time
    would invent a denominator.
    """
    if minutes_elapsed is None:
        return None
    if minutes_elapsed <= 0:
        return None
    return min(1.0, minutes_elapsed / REGULAR_SESSION_MINUTES)


def relative_volume(session_volume: Optional[int],
                    avg_daily_volume: Optional[float],
                    session_fraction: Optional[float]) -> Optional[float]:
    """See the module docstring for exactly what this ratio means."""
    if not session_volume or not avg_daily_volume or avg_daily_volume <= 0:
        return None
    if session_fraction is None or session_fraction <= 0:
        return None
    projected = session_volume / session_fraction
    return projected / avg_daily_volume


def compute_features(snapshot: ScannerSnapshot,
                     intraday_bars: Optional[List] = None,
                     timeframe_minutes: int = 5,
                     benchmark_change_pct: Optional[float] = None,
                     benchmark_symbol: str = "",
                     minutes_elapsed: Optional[float] = None
                     ) -> ScannerFeatures:
    bars = list(intraday_bars or [])
    price = snapshot.price

    feat = ScannerFeatures(
        symbol=snapshot.symbol,
        intraday_bars_used=len(bars),
        relative_volume_basis=RELATIVE_VOLUME_BASIS,
        benchmark_symbol=benchmark_symbol,
    )

    # Session direction. Prefer the session open; fall back to the previous
    # close before the session has opened.
    baseline = snapshot.session_open or snapshot.previous_close
    feat.session_change_pct = _pct_change(price, baseline)

    feat.return_5m = _return_over(bars, 5, timeframe_minutes, price)
    feat.return_15m = _return_over(bars, 15, timeframe_minutes, price)
    feat.return_30m = _return_over(bars, 30, timeframe_minutes, price)

    feat.relative_volume = relative_volume(
        snapshot.volume, snapshot.avg_daily_volume,
        elapsed_session_fraction(minutes_elapsed),
    )

    if snapshot.vwap and snapshot.vwap > 0 and price is not None:
        feat.distance_from_vwap_pct = _pct_change(price, snapshot.vwap)
        # A deadband: sitting on VWAP is not being above it.
        if abs(feat.distance_from_vwap_pct) < 0.05:
            feat.above_vwap = None
        else:
            feat.above_vwap = feat.distance_from_vwap_pct > 0

    high, low = snapshot.day_high, snapshot.day_low
    if high is not None and low is not None and price is not None and high > low:
        feat.range_position = (price - low) / (high - low)
        feat.distance_from_high_pct = _pct_change(price, high)
        feat.distance_from_low_pct = _pct_change(price, low)

    # Relative strength: how the symbol moved versus the benchmark on the
    # session, in percentage points. Simple on purpose - the multi-factor
    # model belongs to Milestone 5.
    if feat.session_change_pct is not None and benchmark_change_pct is not None:
        feat.market_relative_strength = (
            feat.session_change_pct - benchmark_change_pct
        )

    return feat


def average_daily_volume(daily_bars: List, lookback: int = 20,
                         exclude_today: bool = True) -> Optional[float]:
    """Mean daily volume over the lookback window.

    The current, partial session is excluded by default: including it
    would drag the average down all morning and make every symbol look
    unusually active.
    """
    volumes = [b.volume for b in daily_bars if getattr(b, "volume", 0)]
    if exclude_today and len(volumes) > 1:
        volumes = volumes[:-1]
    if not volumes:
        return None
    window = volumes[-lookback:]
    return sum(window) / len(window)
