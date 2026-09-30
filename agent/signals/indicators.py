"""
Canonical indicator arithmetic.

Pure functions over a list of closes, oldest first. No I/O, no provider,
no config: given the same prices they return the same numbers, forever.

This is the single home for the maths. `ml_agent_lite.py` (which the
production `/chatbot` Lambda runs) carries an equivalent implementation,
and `tests/test_signal_equivalence.py` asserts the two agree to floating
point on random inputs. That test exists because the two *will* drift
otherwise, and a silent divergence between what the chatbot says and
what the agent computes is worse than either being wrong on its own.

Every function returns None rather than a substitute when it cannot be
computed. There is no default RSI, no assumed moving average, and no
zero-filled history: a missing indicator must stay missing all the way
to the reader.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

# Minimum closes each indicator needs. Stated rather than implied so a
# caller can explain *why* something is unavailable instead of just
# reporting that it is.
MIN_BARS = {
    "sma_20": 20,
    "sma_50": 50,
    "sma_200": 200,
    "rsi": 15,          # period + 1
    "macd": 34,         # slow + signal - 1
    "bollinger": 20,
    "momentum_10d": 11,
    "volatility": 21,
    "atr": 15,
}


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def std_dev(values: Sequence[float]) -> float:
    """Population standard deviation, matching the Bollinger convention
    used throughout this project."""
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    variance = sum((x - avg) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def sma(prices: Sequence[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return mean(prices[-period:])


def ema_series(prices: Sequence[float], period: int) -> List[float]:
    """Full EMA series, seeded with the SMA of the first `period` values.

    A series rather than a scalar because the MACD signal line is an EMA
    *of the MACD line*, so the MACD line must exist over time.
    """
    if len(prices) < period:
        return []
    multiplier = 2 / (period + 1)
    value = mean(prices[:period])
    out = [value]
    for price in prices[period:]:
        value = (price - value) * multiplier + value
        out.append(value)
    return out


def ema(prices: Sequence[float], period: int) -> Optional[float]:
    series = ema_series(prices, period)
    return series[-1] if series else None


def rsi(prices: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder-style RSI using a simple average of the last `period`
    gains and losses - the convention this project has always used."""
    if len(prices) < period + 1:
        return None

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    if len(gains) < period:
        return None

    avg_gain = mean(gains[-period:])
    avg_loss = mean(losses[-period:])

    if avg_loss == 0:
        # A price that never fell is only maximally overbought if it also
        # rose. With no gains AND no losses the series did not move at
        # all, and RSI is undefined: 0/0, not infinity.
        #
        # The inherited implementation short-circuited on avg_loss == 0
        # alone, so a halted, frozen or thinly-quoted security returned
        # RSI 100 and cast an overbought SELL vote on the strength of
        # having done nothing. 50 is the honest reading for no movement.
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(prices: Sequence[float], fast: int = 12, slow: int = 26,
         signal_period: int = 9) -> Optional[Dict[str, float]]:
    """MACD with a real signal line.

    ACCEPTED BASELINE - do not alter the arithmetic.

    This once set `signal = macd * 0.9`, which is not a signal line. The
    consequence was not cosmetic: histogram became 0.1*macd, so it always
    carried the sign of the MACD line and the crossover test `macd >
    signal` reduced to `macd > 0`, i.e. to `EMA12 > EMA26` - a plain
    trend comparison. MACD's entire purpose is the crossover and it
    carried none of it. Over 300 random walks the histogram sign matched
    the MACD sign 300 times out of 300.

    The signal line is the `signal_period` EMA of the MACD line, needing
    `slow + signal_period - 1` closes. Below that this returns None.
    """
    fast_series = ema_series(prices, fast)
    slow_series = ema_series(prices, slow)
    if not fast_series or not slow_series:
        return None

    # The series start at different points in `prices` - the fast one
    # begins earlier - so they must be aligned before subtracting.
    offset = slow - fast
    macd_line = [fast_series[i + offset] - slow_series[i]
                 for i in range(len(slow_series))]

    signal_series = ema_series(macd_line, signal_period)
    if not signal_series:
        return None

    line = macd_line[-1]
    sig = signal_series[-1]
    return {"macd": line, "signal": sig, "histogram": line - sig}


def bollinger_bands(prices: Sequence[float], period: int = 20,
                    num_std: float = 2.0) -> Optional[Dict[str, float]]:
    if len(prices) < period:
        return None
    middle = sma(prices, period)
    sd = std_dev(prices[-period:])
    return {
        "upper": middle + num_std * sd,
        "middle": middle,
        "lower": middle - num_std * sd,
        "std_dev": sd,
    }


def momentum_pct(prices: Sequence[float], lookback: int = 10) -> Optional[float]:
    """Percentage change over `lookback` bars.

    Sign convention: positive means price is higher than it was
    `lookback` bars ago. Returns None rather than 0.0 when the history is
    short, because "no change" and "cannot tell" are different claims.
    """
    if len(prices) < lookback + 1:
        return None
    past = prices[-(lookback + 1)]
    if past == 0:
        return None
    return (prices[-1] - past) / past * 100.0


def daily_returns(prices: Sequence[float]) -> List[float]:
    out = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev:
            out.append((prices[i] - prev) / prev)
    return out


def realized_volatility(prices: Sequence[float], period: int = 20
                        ) -> Optional[float]:
    """Standard deviation of daily returns, as a fraction.

    This is the unit that makes magnitudes comparable across securities:
    a $1 MACD histogram means something very different on a $10 stock
    than on a $1,000 one, and dividing by price alone still understates
    the difference between a placid utility and a volatile small cap.
    """
    if len(prices) < period + 1:
        return None
    rets = daily_returns(prices[-(period + 1):])
    if len(rets) < 2:
        return None
    return std_dev(rets)


def volatility_pct(prices: Sequence[float], period: int = 20
                   ) -> Optional[float]:
    """Price dispersion over `period` closes, as a percent of the last
    close. Kept because the existing risk assessment uses it."""
    if len(prices) < period:
        return None
    last = prices[-1]
    if not last:
        return None
    return (std_dev(prices[-period:]) / last) * 100.0


def compute_all(prices: Sequence[float]) -> Dict[str, Optional[object]]:
    """Every indicator at once, each independently None-able.

    One pass so a caller does not have to remember which functions exist
    or what history each needs.
    """
    return {
        "price": prices[-1] if prices else None,
        "bars": len(prices),
        "sma_20": sma(prices, 20),
        "sma_50": sma(prices, 50),
        "sma_200": sma(prices, 200),
        "rsi": rsi(prices, 14),
        "macd": macd(prices),
        "bollinger": bollinger_bands(prices, 20),
        "momentum_10d": momentum_pct(prices, 10),
        "realized_volatility": realized_volatility(prices, 20),
        "volatility_pct": volatility_pct(prices, 20),
    }
