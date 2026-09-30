"""
The canonical quantitative signal engine.

Answers one question and refuses the others:

    What direction do the independent market signals support, how
    strongly, and where do they disagree?

It does not answer whether to trade, at what price, in what size, or
with what stop. Those are trade-hypothesis concepts and belong behind
the Risk Governor.

Three quantities are computed and kept apart, because compressing them
into one "confidence" percentage is exactly what made the old output
unreadable:

  direction          which way the evidence points
  signal_agreement   purely structural - how many independent groups
                     agree, and whether their own members agreed
  signal_magnitude   purely about size - how far the underlying readings
                     sit beyond their thresholds, volatility-normalised

A result can be BUY with agreement 1.00 and magnitude 0.31 (everything
agrees, nothing emphatically) or BUY with agreement 0.50 and magnitude
0.88 (one group, but shouting). Those are different pieces of evidence.
The old model multiplied them together and reported a single number, so
they were indistinguishable.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from . import indicators as ind
from . import normalization as norm
from .models import (
    ALL_GROUPS, INDICATOR_GROUPS, CorrelationGroup, DataFreshness,
    GroupDirection, QuantitativeSignalResult, SignalDirection,
    SignalGroupResult, SignalResult, StrengthBand,
)

# --- thresholds ----------------------------------------------------------
# Every cutoff the engine applies, in one place, and recorded on each
# SignalResult so a stored reading can be re-derived later even if a rule
# changes underneath it.

RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
RSI_NEUTRAL_LOW = 40.0
RSI_NEUTRAL_HIGH = 60.0

# The 20/50 crossover needs separation, not just an ordering: two
# averages within 2% of each other are effectively the same line, and
# treating that as a trend produces a signal that flips on noise.
MA_CROSS_BAND = 0.02

MOMENTUM_THRESHOLD_PCT = 5.0

# A crossover this small, relative to price, is floating-point noise
# rather than a reading. Without it `bullish = line > signal` classifies
# a frozen series - where the MACD line and its signal line are both
# exactly 0.0 - as a bearish crossover, so a halted security casts a SELL
# vote for having done nothing. This does not alter the MACD arithmetic:
# the histogram is still line - signal. It only declines to call a
# numerically-zero difference a crossover.
MACD_FLAT_EPSILON = 1e-6

# Minimum closes before the engine will produce any analysis at all.
MIN_HISTORY = 50

# Agreement/magnitude cutoffs for the display band.
STRONG_AGREEMENT = 0.99
MODERATE_AGREEMENT = 0.60
STRONG_MAGNITUDE = 0.60
MODERATE_MAGNITUDE = 0.40


def _no_signal(indicator: str, reason: str,
               freshness: DataFreshness) -> SignalResult:
    """An indicator that could not be computed.

    NO_SIGNAL, never NEUTRAL: absence of evidence must not be recorded
    as evidence of balance.
    """
    return SignalResult(
        indicator=indicator,
        group=INDICATOR_GROUPS[indicator],
        direction=SignalDirection.NO_SIGNAL,
        strength=0.0,
        reason=reason,
        freshness=freshness,
    )


# --- individual indicators ----------------------------------------------

def rsi_signal(rsi_value: Optional[float],
               freshness: DataFreshness) -> SignalResult:
    """RSI votes only at the extremes.

    A reading of 66 is "moderately strong", not a buy. Translating
    relative strength into a directional vote would double-count trend,
    which RSI is measured to correlate with (r = -0.571 against 10-day
    momentum over random walks - the same quantity read the other way).
    """
    if rsi_value is None:
        return _no_signal("rsi", "needs 15 closes", freshness)

    thresholds = {"oversold": RSI_OVERSOLD, "overbought": RSI_OVERBOUGHT,
                  "neutral_low": RSI_NEUTRAL_LOW,
                  "neutral_high": RSI_NEUTRAL_HIGH}

    if rsi_value < RSI_OVERSOLD:
        direction, zone = SignalDirection.BUY, "oversold"
        reason = f"RSI {rsi_value:.1f} is below the oversold threshold of {RSI_OVERSOLD:.0f}"
    elif rsi_value > RSI_OVERBOUGHT:
        direction, zone = SignalDirection.SELL, "overbought"
        reason = f"RSI {rsi_value:.1f} is above the overbought threshold of {RSI_OVERBOUGHT:.0f}"
    elif RSI_NEUTRAL_LOW <= rsi_value <= RSI_NEUTRAL_HIGH:
        direction, zone = SignalDirection.NEUTRAL, "neutral"
        reason = f"RSI {rsi_value:.1f} is in the neutral 40-60 band"
    else:
        direction = SignalDirection.NEUTRAL
        zone = "neutral_high" if rsi_value > RSI_NEUTRAL_HIGH else "neutral_low"
        reason = (f"RSI {rsi_value:.1f} is {'elevated' if rsi_value > 60 else 'soft'} "
                  f"but has not crossed a threshold, so it casts no vote")

    return SignalResult(
        indicator="rsi", group=CorrelationGroup.MOMENTUM, direction=direction,
        strength=norm.rsi_strength(rsi_value),
        raw_values={"rsi": rsi_value}, thresholds=thresholds,
        reason=reason, freshness=freshness,
    )


def macd_signal(macd_data: Optional[Dict], price: Optional[float],
                realized_vol: Optional[float],
                freshness: DataFreshness) -> SignalResult:
    """The crossover is the indicator.

    `macd > signal` is the reading, not `macd > 0`. A positive MACD line
    falling below its own 9-period EMA is a bearish crossover, and the
    old implementation could not express that state at all.
    """
    if not macd_data:
        return _no_signal("macd", "needs 34 closes for a 9-period signal line",
                          freshness)

    line = macd_data["macd"]
    sig = macd_data["signal"]
    hist = macd_data["histogram"]

    scale = abs(price) if price else 1.0
    if abs(hist) <= MACD_FLAT_EPSILON * scale:
        direction = SignalDirection.NEUTRAL
        reason = ("MACD is sitting on its signal line; there is no crossover "
                  "to read")
    elif line > sig:
        direction = SignalDirection.BUY
        reason = "MACD is above its 9-period EMA signal line"
    else:
        direction = SignalDirection.SELL
        reason = "MACD is below its 9-period EMA signal line"

    return SignalResult(
        indicator="macd", group=CorrelationGroup.MOMENTUM,
        direction=direction,
        strength=norm.macd_strength(hist, price, realized_vol),
        raw_values={"macd": line, "signal": sig, "histogram": hist,
                    "price": price},
        thresholds={"fast": 12, "slow": 26, "signal_period": 9,
                    "crossover_at": 0.0,
                    "flat_epsilon_rel": MACD_FLAT_EPSILON},
        reason=reason, freshness=freshness,
    )


def ma_crossover_signal(sma20: Optional[float], sma50: Optional[float],
                        price: Optional[float],
                        realized_vol: Optional[float],
                        freshness: DataFreshness) -> SignalResult:
    if sma20 is None or sma50 is None:
        return _no_signal("ma_crossover", "needs 50 closes", freshness)

    thresholds = {"separation_band": MA_CROSS_BAND}
    if sma20 > sma50 * (1 + MA_CROSS_BAND):
        direction = SignalDirection.BUY
        reason = (f"SMA20 ({sma20:.2f}) is more than "
                  f"{MA_CROSS_BAND * 100:.0f}% above SMA50 ({sma50:.2f})")
    elif sma20 < sma50 * (1 - MA_CROSS_BAND):
        direction = SignalDirection.SELL
        reason = (f"SMA20 ({sma20:.2f}) is more than "
                  f"{MA_CROSS_BAND * 100:.0f}% below SMA50 ({sma50:.2f})")
    else:
        direction = SignalDirection.NEUTRAL
        reason = (f"SMA20 and SMA50 are within {MA_CROSS_BAND * 100:.0f}% of "
                  f"each other, which is not a trend")

    return SignalResult(
        indicator="ma_crossover", group=CorrelationGroup.TREND,
        direction=direction,
        strength=norm.ma_spread_strength(sma20, sma50, price, realized_vol),
        raw_values={"sma_20": sma20, "sma_50": sma50, "price": price},
        thresholds=thresholds, reason=reason, freshness=freshness,
    )


def golden_cross_signal(sma50: Optional[float], sma200: Optional[float],
                        price: Optional[float],
                        realized_vol: Optional[float],
                        freshness: DataFreshness) -> SignalResult:
    """Long-horizon trend. Kept separate from the 20/50 crossover rather
    than merged: they are correlated (r = +0.237) but not the same
    observation, and the trend group is what handles that correlation."""
    if sma50 is None or sma200 is None:
        return _no_signal("golden_cross", "needs 200 closes", freshness)

    bullish = sma50 > sma200
    return SignalResult(
        indicator="golden_cross", group=CorrelationGroup.TREND,
        direction=SignalDirection.BUY if bullish else SignalDirection.SELL,
        strength=norm.ma_spread_strength(sma50, sma200, price, realized_vol),
        raw_values={"sma_50": sma50, "sma_200": sma200, "price": price},
        thresholds={"crossover_at": 0.0},
        reason=(f"SMA50 ({sma50:.2f}) is above SMA200 ({sma200:.2f})"
                if bullish else
                f"SMA50 ({sma50:.2f}) is below SMA200 ({sma200:.2f})"),
        freshness=freshness,
    )


def momentum_signal(momentum_10d: Optional[float],
                    realized_vol: Optional[float],
                    freshness: DataFreshness) -> SignalResult:
    """Ten-session percentage change.

    Sign convention: positive means price is higher than ten sessions
    ago. Votes only beyond +/-5%, because a smaller move is inside the
    ordinary range of most liquid names and would fire constantly.
    """
    if momentum_10d is None:
        return _no_signal("momentum_10d", "needs 11 closes", freshness)

    thresholds = {"threshold_pct": MOMENTUM_THRESHOLD_PCT, "lookback": 10}
    if momentum_10d > MOMENTUM_THRESHOLD_PCT:
        direction = SignalDirection.BUY
        reason = f"price is {momentum_10d:+.1f}% over 10 sessions"
    elif momentum_10d < -MOMENTUM_THRESHOLD_PCT:
        direction = SignalDirection.SELL
        reason = f"price is {momentum_10d:+.1f}% over 10 sessions"
    else:
        direction = SignalDirection.NEUTRAL
        reason = (f"10-session change of {momentum_10d:+.1f}% has not reached "
                  f"the +/-{MOMENTUM_THRESHOLD_PCT:.0f}% threshold")

    return SignalResult(
        indicator="momentum_10d", group=CorrelationGroup.MOMENTUM,
        direction=direction,
        strength=norm.momentum_strength(momentum_10d, MOMENTUM_THRESHOLD_PCT,
                                        realized_vol),
        raw_values={"momentum_10d_pct": momentum_10d},
        thresholds=thresholds, reason=reason, freshness=freshness,
    )


def bollinger_signal(price: Optional[float], bands: Optional[Dict],
                     freshness: DataFreshness) -> SignalResult:
    """Mean reversion, and only at the extremes.

    Being *near* a band is not a signal. The rule is a close outside the
    band; anything inside is a measured neutral, which is a different
    claim from "no reading".
    """
    if not bands or price is None:
        return _no_signal("bollinger", "needs 20 closes", freshness)

    upper, lower, middle = bands.get("upper"), bands.get("lower"), bands.get("middle")
    if upper is None or lower is None or upper <= lower:
        return _no_signal("bollinger", "degenerate bands (zero dispersion)",
                          freshness)

    position = (price - lower) / (upper - lower)
    raw = {"price": price, "upper": upper, "middle": middle, "lower": lower,
           "position_in_band": position}
    thresholds = {"num_std": 2.0, "period": 20,
                  "vote_requires": "close outside the band"}

    if price > upper:
        direction = SignalDirection.SELL
        reason = f"price {price:.2f} closed above the upper band {upper:.2f}"
    elif price < lower:
        direction = SignalDirection.BUY
        reason = f"price {price:.2f} closed below the lower band {lower:.2f}"
    else:
        direction = SignalDirection.NEUTRAL
        where = ("near the upper band" if position >= 0.75 else
                 "near the lower band" if position <= 0.25 else
                 "mid-range")
        reason = (f"price is {where} but inside the bands, which is not a "
                  f"mean-reversion signal")

    return SignalResult(
        indicator="bollinger", group=CorrelationGroup.MEAN_REVERSION,
        direction=direction,
        strength=norm.bollinger_strength(price, upper, lower),
        raw_values=raw, thresholds=thresholds, reason=reason,
        freshness=freshness,
    )


def evaluate_indicators(prices: Sequence[float],
                        freshness: DataFreshness = DataFreshness.UNKNOWN
                        ) -> List[SignalResult]:
    """Every indicator, each independently able to return NO_SIGNAL."""
    values = ind.compute_all(prices)
    price = values["price"]
    vol = values["realized_volatility"]

    return [
        ma_crossover_signal(values["sma_20"], values["sma_50"], price, vol,
                            freshness),
        golden_cross_signal(values["sma_50"], values["sma_200"], price, vol,
                            freshness),
        rsi_signal(values["rsi"], freshness),
        macd_signal(values["macd"], price, vol, freshness),
        momentum_signal(values["momentum_10d"], vol, freshness),
        bollinger_signal(price, values["bollinger"], freshness),
    ]


# --- grouping ------------------------------------------------------------

def group_signals(signals: Sequence[SignalResult]) -> List[SignalGroupResult]:
    """Net each correlation group to ONE opinion.

    Indicators inside a group measure substantially the same thing, so
    however many fire they contribute a single vote between them. This
    is the whole reason groups exist: three momentum readings agreeing is
    one observation seen three times, and counting it as three was how a
    single fact came to be reported as independent agreement.

    Opposing members net against each other rather than both counting,
    and a group whose own members conflict has its influence halved -
    internal disagreement is real information about the quality of that
    group's opinion.
    """
    by_group: Dict[str, List[SignalResult]] = {str(g): [] for g in ALL_GROUPS}
    for sig in signals:
        by_group.setdefault(str(sig.group), []).append(sig)

    out: List[SignalGroupResult] = []
    for group in ALL_GROUPS:
        members = by_group.get(str(group), [])
        directional = [m for m in members if m.direction.is_directional]
        buys = [m for m in directional if m.direction is SignalDirection.BUY]
        sells = [m for m in directional if m.direction is SignalDirection.SELL]
        neutrals = [m for m in members
                    if m.direction is SignalDirection.NEUTRAL]
        absent = [m for m in members
                  if m.direction is SignalDirection.NO_SIGNAL]

        net = sum(m.direction.vote * m.strength for m in directional)
        conflicted = bool(buys) and bool(sells)

        if not members or len(absent) == len(members):
            direction = GroupDirection.NO_SIGNAL
            reason = "no member of this group could be computed"
        elif not directional:
            direction = GroupDirection.NEUTRAL
            reason = "members computed but none crossed a threshold"
        elif net > 1e-9:
            direction = GroupDirection.BUY
            reason = "net of this group's members supports upward direction"
        elif net < -1e-9:
            direction = GroupDirection.SELL
            reason = "net of this group's members supports downward direction"
        else:
            # Opposing members of equal strength: a genuine deadlock, not
            # a neutral reading and not a coin toss to be dressed up.
            direction = GroupDirection.MIXED
            reason = "members oppose each other with equal strength"

        if conflicted and direction.is_directional:
            reason = ("members of this group disagree; its influence is "
                      "halved")

        internal_agreement = 0.5 if conflicted else 1.0

        # The strongest member that supports the group's net direction.
        # A mean would let a weak agreeing member dilute a strong one.
        if direction is GroupDirection.BUY:
            supporting = buys
        elif direction is GroupDirection.SELL:
            supporting = sells
        else:
            supporting = []
        magnitude = max((m.strength for m in supporting), default=0.0)

        out.append(SignalGroupResult(
            group=group, direction=direction,
            weight=magnitude * internal_agreement,
            magnitude=magnitude, net=net,
            internal_agreement=internal_agreement,
            internal_disagreement=conflicted,
            member_count=len(members), opinionated_members=len(directional),
            buy_votes=len(buys), sell_votes=len(sells),
            neutral_votes=len(neutrals), no_signal_votes=len(absent),
            members=list(members), reason=reason,
        ))
    return out


# --- aggregation ---------------------------------------------------------

def _band(direction: SignalDirection, agreement: float, magnitude: float,
          opinionated: int, conflicted: bool) -> StrengthBand:
    if not direction.is_directional:
        return StrengthBand.MIXED if conflicted else StrengthBand.NONE
    if agreement >= STRONG_AGREEMENT and opinionated >= 2 \
            and magnitude >= STRONG_MAGNITUDE:
        return StrengthBand.STRONG
    if agreement >= MODERATE_AGREEMENT and magnitude >= MODERATE_MAGNITUDE:
        return StrengthBand.MODERATE
    return StrengthBand.WEAK


def aggregate(groups: Sequence[SignalGroupResult]) -> Dict:
    """Combine group opinions into a direction, an agreement and a
    magnitude - three numbers, never one.

    `signal_agreement` is purely structural: the share of opinionated
    groups that agreed, scaled by whether those groups' own members
    agreed. It contains no magnitude term, so it cannot rise because a
    reading got bigger.

    `signal_magnitude` is purely about size: the mean magnitude of the
    groups that won. It contains no agreement term, so it cannot rise
    because more groups happened to concur.
    """
    opinionated = [g for g in groups if g.direction.is_directional]
    buys = [g for g in opinionated if g.direction is GroupDirection.BUY]
    sells = [g for g in opinionated if g.direction is GroupDirection.SELL]
    conflicted_groups = [g for g in groups if g.internal_disagreement]

    counts = {
        "buy_groups": len(buys),
        "sell_groups": len(sells),
        "neutral_groups": sum(1 for g in groups
                              if g.direction is GroupDirection.NEUTRAL),
        "no_signal_groups": sum(1 for g in groups
                                if g.direction is GroupDirection.NO_SIGNAL),
        "mixed_groups": sum(1 for g in groups
                            if g.direction is GroupDirection.MIXED),
        "opinionated_groups": len(opinionated),
    }

    if not opinionated:
        return {
            **counts, "direction": SignalDirection.NO_SIGNAL
            if counts["neutral_groups"] == 0 else SignalDirection.NEUTRAL,
            "signal_agreement": 0.0, "signal_magnitude": 0.0,
            "strength_band": StrengthBand.NONE,
            "winning_groups": [],
        }

    if len(buys) > len(sells):
        direction, winning = SignalDirection.BUY, buys
    elif len(sells) > len(buys):
        direction, winning = SignalDirection.SELL, sells
    else:
        # Independent groups split evenly. That is genuine indecision,
        # and reporting a direction here would invent a tiebreak the
        # evidence does not contain.
        return {
            **counts, "direction": SignalDirection.NEUTRAL,
            "signal_agreement": 0.0,
            "signal_magnitude": 0.0,
            "strength_band": StrengthBand.MIXED,
            "winning_groups": [],
        }

    share = len(winning) / len(opinionated)
    internal = sum(g.internal_agreement for g in winning) / len(winning)
    agreement = share * internal
    magnitude = sum(g.magnitude for g in winning) / len(winning)

    return {
        **counts,
        "direction": direction,
        "signal_agreement": max(0.0, min(1.0, agreement)),
        "signal_magnitude": max(0.0, min(1.0, magnitude)),
        "strength_band": _band(direction, agreement, magnitude,
                               len(opinionated), bool(conflicted_groups)),
        "winning_groups": [str(g.group) for g in winning],
    }


# --- regime --------------------------------------------------------------

# How much the broader market discounts a stock's own reading. The regime
# NEVER creates or flips a direction: it scales magnitude only, so a
# hostile market can make evidence count for less but can never make
# evidence appear.
#
# The project is long-only operationally. A SELL reading here is
# analysis - "the indicators lean down" - and must never be read as a
# short instruction.
REGIME_ADJUSTMENT = {
    "BUY": {
        "STRONG_BULLISH": 1.00, "BULLISH": 1.00, "NEUTRAL": 0.95,
        "MIXED": 0.85, "VOLATILE": 0.75, "BEARISH": 0.70,
        "STRONG_BEARISH": 0.55, "UNKNOWN": 0.70,
    },
    "SELL": {
        "STRONG_BEARISH": 1.00, "BEARISH": 1.00, "NEUTRAL": 0.95,
        "MIXED": 0.85, "VOLATILE": 0.85, "BULLISH": 0.70,
        "STRONG_BULLISH": 0.55, "UNKNOWN": 0.70,
    },
}

REGIME_WARNINGS = {
    "STRONG_BEARISH": "market regime is STRONG_BEARISH: long-side evidence "
                      "is heavily discounted",
    "BEARISH": "market regime is BEARISH: long-side evidence is discounted",
    "VOLATILE": "market regime is VOLATILE: readings are less reliable",
    "UNKNOWN": "market regime is UNKNOWN: the reading is not regime-informed",
}


def regime_adjustment(direction: SignalDirection, regime: str) -> float:
    if not direction.is_directional:
        return 1.0
    table = REGIME_ADJUSTMENT.get(str(direction), {})
    return table.get((regime or "UNKNOWN").upper(), table.get("UNKNOWN", 0.70))


# --- top level -----------------------------------------------------------

def evaluate(symbol: str, prices: Sequence[float],
             freshness: DataFreshness = DataFreshness.UNKNOWN,
             market_regime: str = "UNKNOWN",
             regime_confidence: float = 0.0,
             data_quality: Optional[Dict] = None,
             price: Optional[float] = None) -> QuantitativeSignalResult:
    """Full quantitative evaluation for one symbol.

    Returns a result with `analysis_available` False rather than raising
    when history is short: a caller asking about a newly listed security
    should get a stated absence, not an exception.
    """
    from .reasons import build_reasons

    prices = list(prices or [])
    result = QuantitativeSignalResult(
        symbol=symbol.upper(),
        market_regime=(market_regime or "UNKNOWN").upper(),
        regime_confidence=regime_confidence,
        price=price if price is not None else (prices[-1] if prices else None),
        data_quality=dict(data_quality or {}),
    )

    if len(prices) < MIN_HISTORY:
        result.direction = SignalDirection.NO_SIGNAL
        result.strength_band = StrengthBand.NONE
        result.warnings.append(
            f"only {len(prices)} closes available; the engine needs at least "
            f"{MIN_HISTORY}"
        )
        result.reasons.append(
            "Not enough price history to evaluate this security."
        )
        return result

    signals = evaluate_indicators(prices, freshness)
    groups = group_signals(signals)
    agg = aggregate(groups)

    result.indicator_results = signals
    result.group_results = groups
    result.direction = agg["direction"]
    result.signal_agreement = agg["signal_agreement"]
    result.signal_magnitude = agg["signal_magnitude"]
    result.strength_band = agg["strength_band"]
    result.buy_groups = agg["buy_groups"]
    result.sell_groups = agg["sell_groups"]
    result.neutral_groups = agg["neutral_groups"]
    result.no_signal_groups = agg["no_signal_groups"]
    result.opinionated_groups = agg["opinionated_groups"]
    result.indicators_evaluated = sum(
        1 for s in signals if s.direction is not SignalDirection.NO_SIGNAL)
    result.indicators_directional = sum(1 for s in signals if s.counted)

    adjustment = regime_adjustment(result.direction, result.market_regime)
    result.regime_adjustment = adjustment
    result.regime_adjusted_magnitude = result.signal_magnitude * adjustment
    result.regime_note = (
        f"magnitude scaled by {adjustment:.2f} for a "
        f"{result.market_regime} market regime; the regime does not create "
        f"or change direction"
    )
    warning = REGIME_WARNINGS.get(result.market_regime)
    if warning and result.direction.is_directional:
        result.warnings.append(warning)

    if freshness is DataFreshness.STALE:
        result.warnings.append(
            "price data is stale; this reading may not reflect current "
            "conditions"
        )
    elif freshness in (DataFreshness.MISSING, DataFreshness.UNKNOWN):
        result.warnings.append(
            "price data freshness is unknown; treat this reading with caution"
        )

    missing = [s.label for s in signals
               if s.direction is SignalDirection.NO_SIGNAL]
    if missing:
        result.warnings.append(
            "not computed: " + ", ".join(missing)
        )

    result.reasons = build_reasons(result)
    return result
