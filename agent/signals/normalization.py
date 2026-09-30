"""
Signal magnitude normalisation.

The problem this solves: before this module, every signal carried a
hardcoded constant. RSI fired at 0.85 whether it read 71 or 95. MACD
fired at 0.70 whether the histogram was -0.01 or -8.00. A "strength"
that cannot vary is not a strength, and it made magnitude and agreement
indistinguishable - both were just "confidence".

Two rules govern everything here.

**Never compare raw currency across securities.** A $1 MACD histogram is
enormous on a $10 stock and noise on a $1,000 one. Every magnitude is
therefore expressed as a ratio - to price, to the security's own
volatility, or to the width of its own bands.

**A signal that has just crossed its threshold is weak, not absent.**
Each map has a floor, so a reading one tick past its cutoff produces a
small positive magnitude rather than zero. Zero is reserved for "not
directional at all".

All maps are saturating rather than clipped:

    saturate(r, k) = r / (r + k)

which is smooth, bounded in [0, 1), and reaches 0.5 at r = k. It never
reaches 1.0, which is deliberate: no reading should ever be maximal.

The scale constants below are choices, not discovered truths. They were
set so that a typical crossing lands near the middle of the range on
liquid large caps, and they have not been validated against out-of-sample
results. Tests assert the *properties* that must hold - monotonicity,
bounds, volatility invariance - rather than pinning these numbers, so
they can be revised without rewriting the suite.
"""
from __future__ import annotations

from typing import Optional

# A signal exactly at its threshold still counts for something.
STRENGTH_FLOOR = 0.25

# Below this, volatility is treated as a floor rather than a divisor.
# A security whose 20-day return standard deviation is under 0.3% is
# either halted, barely traded, or has bad data; dividing by it would
# turn rounding error into a maximal signal.
MIN_VOLATILITY = 0.003

# Scale constants, in units of "ratio at which magnitude reaches 0.5".
MACD_SCALE = 0.25          # |histogram| / price, in units of daily vol
MA_SPREAD_SCALE = 0.60     # |SMA spread| / price, in units of daily vol
MOMENTUM_SCALE = 1.20      # excess beyond the momentum threshold, in vol units
RSI_EXCESS_SCALE = 12.0    # RSI points beyond 30/70
BOLLINGER_SCALE = 0.35     # excess beyond the band, as a fraction of band half-width


def saturate(ratio: float, scale: float) -> float:
    """Map [0, inf) -> [0, 1), reaching 0.5 at ratio == scale."""
    if ratio <= 0 or scale <= 0:
        return 0.0
    return ratio / (ratio + scale)


def scaled(ratio: float, scale: float, floor: float = STRENGTH_FLOOR) -> float:
    """Saturating map with a floor for a signal that just crossed."""
    if ratio <= 0:
        return floor
    return floor + (1.0 - floor) * saturate(ratio, scale)


def effective_volatility(realized_vol: Optional[float]) -> float:
    """Volatility to divide by, floored and never None.

    Falling back to the floor when volatility is unknown keeps a missing
    input from silently inflating every magnitude for that symbol.
    """
    if realized_vol is None or realized_vol <= 0:
        return MIN_VOLATILITY
    return max(realized_vol, MIN_VOLATILITY)


def macd_strength(histogram: Optional[float], price: Optional[float],
                  realized_vol: Optional[float]) -> float:
    """How emphatic a MACD crossover is.

    The histogram is the distance between the MACD line and its signal
    line, so its absolute size is what "emphatic" means here. Expressed
    as a fraction of price, then in units of the security's own daily
    volatility.
    """
    if histogram is None or not price or price <= 0:
        return STRENGTH_FLOOR
    ratio = (abs(histogram) / price) / effective_volatility(realized_vol)
    return scaled(ratio, MACD_SCALE)


def rsi_strength(rsi_value: Optional[float], oversold: float = 30.0,
                 overbought: float = 70.0) -> float:
    """How far past its threshold RSI sits.

    RSI is already a bounded 0-100 oscillator, so it needs no volatility
    normalisation - it is scale-free by construction. RSI 72 must be
    weaker than RSI 86, which the old constant 0.85 could not express.
    """
    if rsi_value is None:
        return STRENGTH_FLOOR
    if rsi_value < oversold:
        excess = oversold - rsi_value
    elif rsi_value > overbought:
        excess = rsi_value - overbought
    else:
        return 0.0
    return scaled(excess, RSI_EXCESS_SCALE)


def ma_spread_strength(short_ma: Optional[float], long_ma: Optional[float],
                       price: Optional[float],
                       realized_vol: Optional[float]) -> float:
    """Separation between two moving averages.

    Normalised distance `(short - long) / price`, then in volatility
    units, so a 1% separation on a placid name outranks 1% on a wild one.
    """
    if short_ma is None or long_ma is None or not price or price <= 0:
        return STRENGTH_FLOOR
    ratio = (abs(short_ma - long_ma) / price) / effective_volatility(realized_vol)
    return scaled(ratio, MA_SPREAD_SCALE)


def momentum_strength(momentum_percent: Optional[float], threshold: float,
                      realized_vol: Optional[float]) -> float:
    """Excess beyond the momentum threshold, in volatility units.

    A 6% ten-day move is unremarkable on a name that routinely moves 4% a
    day and notable on one that moves 0.5%.
    """
    if momentum_percent is None:
        return STRENGTH_FLOOR
    excess = abs(momentum_percent) - threshold
    if excess <= 0:
        return 0.0
    # momentum is in percent; volatility is a fraction, hence the /100.
    ratio = (excess / 100.0) / effective_volatility(realized_vol)
    return scaled(ratio, MOMENTUM_SCALE)


def bollinger_strength(price: Optional[float], upper: Optional[float],
                       lower: Optional[float]) -> float:
    """How far outside its band price closed.

    Measured against the band's own half-width, which already embeds two
    standard deviations of the security's recent dispersion - so this is
    self-normalising and needs no separate volatility term.
    """
    if price is None or upper is None or lower is None or upper <= lower:
        return STRENGTH_FLOOR
    half_width = (upper - lower) / 2.0
    if half_width <= 0:
        return STRENGTH_FLOOR
    if price > upper:
        excess = price - upper
    elif price < lower:
        excess = lower - price
    else:
        return 0.0
    return scaled(excess / half_width, BOLLINGER_SCALE)
