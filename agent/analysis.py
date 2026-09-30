"""
Presentation layer over the canonical quantitative signal engine.

This module holds NO analysis. It has no thresholds, no formulas and no
opinion about what a reading means: every number and every verdict comes
from `agent.signals`, and this file only renames and arranges them for a
UI.

That constraint is the point. An earlier version of this file re-derived
the RSI zones, the Bollinger vote and the MACD crossover with its own
copies of the cutoffs. They agreed with the engine only because the same
person wrote both in the same afternoon; nothing enforced it, and the
first edit to either would have produced a UI that quietly disagreed
with the system it was displaying. Duplicated interpretation is worse
than duplicated arithmetic, because the disagreement is a matter of
meaning and no equality check catches it.

If something here needs a threshold, it belongs in the engine.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .signals import QuantitativeSignalResult
from .signals.models import (
    AGREEMENT_MEANING, ALL_GROUPS, DISCLAIMER, GROUP_DESCRIPTIONS,
    GROUP_LABELS, MAGNITUDE_MEANING, GroupDirection, SignalDirection,
    StrengthBand,
)

# Words a reader can act on. The engine's band is the source of truth;
# this only translates it, and the mapping is one-to-one so a new band
# cannot silently fall through to a friendly-sounding default.
STRENGTH_WORDS = {
    StrengthBand.STRONG: "Strong",
    StrengthBand.MODERATE: "Moderate",
    StrengthBand.WEAK: "Weak",
    StrengthBand.MIXED: "Mixed",
    StrengthBand.NONE: "No directional signal",
}

# The UI says HOLD where the engine says NEUTRAL. Same state, different
# audience: "neutral" describes a measurement, "hold" describes what the
# reader is being told the evidence supports doing about it.
UI_DIRECTION = {
    SignalDirection.BUY: "BUY",
    SignalDirection.SELL: "SELL",
    SignalDirection.NEUTRAL: "HOLD",
    SignalDirection.NO_SIGNAL: "HOLD",
}

GROUP_UI_DIRECTION = {
    GroupDirection.BUY: "BUY",
    GroupDirection.SELL: "SELL",
    GroupDirection.NEUTRAL: "NEUTRAL",
    GroupDirection.MIXED: "MIXED",
    GroupDirection.NO_SIGNAL: "NO_SIGNAL",
}


def _fmt(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


def _group_summary(group) -> str:
    if group.internal_disagreement:
        return "Signals inside this group disagree, which reduces its influence"
    if group.direction is GroupDirection.BUY:
        return "Supports upward direction"
    if group.direction is GroupDirection.SELL:
        return "Supports downward direction"
    if group.direction is GroupDirection.MIXED:
        return "Members oppose each other exactly, so this group casts no vote"
    if group.direction is GroupDirection.NO_SIGNAL:
        return "No signal from this group"
    return "Measured, but nothing crossed a threshold"


def build_group_view(result: QuantitativeSignalResult) -> List[Dict]:
    """One row per correlation group, including the silent ones.

    Showing all three - with the absent ones marked - is what stops a
    reader counting six indicators as six independent opinions.
    """
    out: List[Dict] = []
    for group_enum in ALL_GROUPS:
        group = result.group(group_enum)
        key = str(group_enum)
        if group is None:
            out.append({
                "group": key, "label": GROUP_LABELS[key],
                "description": GROUP_DESCRIPTIONS[key],
                "direction": "NO_SIGNAL", "counted": 0, "weight": 0.0,
                "magnitude": 0.0, "internal_disagreement": False,
                "signals_fired": 0, "members": [],
                "summary": "No signal from this group",
            })
            continue

        out.append({
            "group": key,
            "label": group.label,
            "description": GROUP_DESCRIPTIONS.get(key, ""),
            "direction": GROUP_UI_DIRECTION[group.direction],
            "counted": group.counted,
            "weight": _fmt(group.weight, 4),
            "magnitude": _fmt(group.magnitude, 4),
            "net": _fmt(group.net, 4),
            "internal_agreement": group.internal_agreement,
            "internal_disagreement": group.internal_disagreement,
            "signals_fired": group.opinionated_members,
            "buy_votes": group.buy_votes,
            "sell_votes": group.sell_votes,
            "members": [
                {
                    "signal": m.indicator,
                    "label": m.label,
                    "direction": str(m.direction),
                    "strength": _fmt(m.strength, 4),
                    "reason": m.reason,
                }
                for m in group.members
            ],
            "summary": _group_summary(group),
        })
    return out


def _indicator_block(result: QuantitativeSignalResult, name: str) -> Dict:
    """Generic passthrough of one indicator's own reading.

    Everything shown is what the engine recorded: its direction, its
    strength, the raw values it used and the thresholds it applied.
    """
    sig = result.indicator(name)
    if sig is None:
        return {"available": False}
    available = sig.direction is not SignalDirection.NO_SIGNAL
    return {
        "available": available,
        "direction": str(sig.direction),
        "engine_vote": (str(sig.direction).lower()
                        if sig.direction.is_directional else "none"),
        "strength": _fmt(sig.strength, 4),
        "reason": sig.reason,
        "raw_values": sig.raw_values,
        "thresholds": sig.thresholds,
        "note": "" if available else sig.reason,
    }


def _macd_block(result: QuantitativeSignalResult) -> Dict:
    block = _indicator_block(result, "macd")
    if not block.get("available"):
        return {"available": False,
                "note": block.get("note")
                or "needs 34 closes for a 9-period signal line"}
    raw = block["raw_values"]
    hist = raw.get("histogram")
    bullish = str(block["direction"]) == "BUY"
    return {
        **block,
        "macd_line": _fmt(raw.get("macd"), 4),
        "signal_line": _fmt(raw.get("signal"), 4),
        "histogram": _fmt(hist, 4),
        "relation": ("MACD > Signal" if bullish else
                     "MACD < Signal" if str(block["direction"]) == "SELL"
                     else "MACD ≈ Signal"),
        "crossover": ("bullish" if bullish else
                      "bearish" if str(block["direction"]) == "SELL"
                      else "none"),
        "interpretation": block["reason"],
        "note": ("The signal line is a 9-period EMA of the MACD line. A "
                 "positive MACD line can still be a bearish crossover if it "
                 "is falling below that average."),
    }


def _rsi_block(result: QuantitativeSignalResult) -> Dict:
    block = _indicator_block(result, "rsi")
    if not block.get("available"):
        return {"available": False, "note": block.get("note", "")}
    sig = result.indicator("rsi")
    value = sig.raw_values.get("rsi")
    th = sig.thresholds
    # Zone naming is presentation. The cutoffs come from the engine.
    if value is None:
        zone, reading = "unknown", "Unknown"
    elif value < th.get("oversold", 30):
        zone, reading = "oversold", "Oversold"
    elif value > th.get("overbought", 70):
        zone, reading = "overbought", "Overbought"
    elif th.get("neutral_low", 40) <= value <= th.get("neutral_high", 60):
        zone, reading = "neutral", "Neutral"
    else:
        zone = "leaning"
        reading = ("Moderately strong" if value > th.get("neutral_high", 60)
                   else "Moderately weak")
    return {
        **block,
        "value": _fmt(value, 1),
        "zone": zone,
        "reading": reading,
        "note": (f"The engine only votes on RSI below "
                 f"{th.get('oversold', 30):.0f} or above "
                 f"{th.get('overbought', 70):.0f}, and treats "
                 f"{th.get('neutral_low', 40):.0f}-"
                 f"{th.get('neutral_high', 60):.0f} as neutral. A reading "
                 f"above 50 is not itself a buy signal."),
    }


def _moving_average_block(result: QuantitativeSignalResult) -> Dict:
    block = _indicator_block(result, "ma_crossover")
    if not block.get("available"):
        return {"available": False, "note": block.get("note", "")}
    raw = block["raw_values"]
    price, sma20, sma50 = (raw.get("price"), raw.get("sma_20"),
                           raw.get("sma_50"))
    ordered = sorted((("Price", price), ("SMA 20", sma20), ("SMA 50", sma50)),
                     key=lambda kv: -(kv[1] or 0))
    bullish = price and sma20 and sma50 and price > sma20 > sma50
    bearish = price and sma20 and sma50 and price < sma20 < sma50
    return {
        **block,
        "price": _fmt(price), "sma_20": _fmt(sma20), "sma_50": _fmt(sma50),
        "structure": " > ".join(label for label, _v in ordered),
        "reading": ("Bullish structure" if bullish else
                    "Bearish structure" if bearish else "Mixed structure"),
    }


def _bollinger_block(result: QuantitativeSignalResult) -> Dict:
    block = _indicator_block(result, "bollinger")
    if not block.get("available"):
        return {"available": False,
                "note": block.get("note")
                or ("Bollinger Bands only vote at the band extremes, so this "
                    "group is silent most of the time.")}
    raw = block["raw_values"]
    position = raw.get("position_in_band")
    price, upper, lower = raw.get("price"), raw.get("upper"), raw.get("lower")
    if price is not None and upper is not None and price > upper:
        where = "Above upper band"
    elif price is not None and lower is not None and price < lower:
        where = "Below lower band"
    elif position is not None and position >= 0.75:
        where = "Near upper band"
    elif position is not None and position <= 0.25:
        where = "Near lower band"
    else:
        where = "Mid-range"
    return {
        **block,
        "upper": _fmt(upper), "middle": _fmt(raw.get("middle")),
        "lower": _fmt(lower), "price": _fmt(price),
        "position_pct": None if position is None else round(position * 100, 1),
        "where": where,
        "note": ("Being near a band is not itself a signal. The engine only "
                 "votes when price closes outside a band."),
    }


def build_explanation(result: QuantitativeSignalResult,
                      recommendation: str) -> Dict:
    """The engine's deterministic reasons, with a headline for the UI."""
    groups = result.group_results
    supporting = [g.label for g in groups
                  if g.direction.is_directional
                  and GROUP_UI_DIRECTION[g.direction] == recommendation]
    opposing = [g.label for g in groups
                if g.direction.is_directional
                and GROUP_UI_DIRECTION[g.direction] != recommendation]
    silent = [g.label for g in groups
              if g.direction is GroupDirection.NO_SIGNAL]
    return {
        "headline": f"Why {recommendation}?",
        "lines": list(result.reasons),
        "supporting_groups": supporting,
        "opposing_groups": opposing,
        "silent_groups": silent,
    }


def build_analysis(result: QuantitativeSignalResult,
                   quote=None,
                   market_context: Optional[Dict] = None) -> Dict:
    """Assemble the payload the review UI consumes.

    `quote` is optional and supplies only presentation fields the engine
    has no opinion about - the session change and volume. Nothing
    analytical is taken from it.
    """
    recommendation = UI_DIRECTION[result.direction]
    group_view = build_group_view(result)

    buy_groups = result.buy_groups
    sell_groups = result.sell_groups
    neutral_or_silent = (result.neutral_groups + result.no_signal_groups
                         + sum(1 for g in result.group_results
                               if g.direction is GroupDirection.MIXED))

    if buy_groups == 0 and sell_groups == 0:
        verdict = "No strong directional signal"
    elif buy_groups and sell_groups:
        verdict = "Signals disagree"
    elif max(buy_groups, sell_groups) >= 2:
        verdict = "Independent groups agree"
    else:
        verdict = "Single group only"

    return {
        "symbol": result.symbol,
        "price": _fmt(result.price),
        "change": getattr(quote, "change", None),
        "change_percent": getattr(quote, "change_percent", None),
        "volume": getattr(quote, "volume", None),
        "previous_close": getattr(quote, "previous_close", None),

        "recommendation": recommendation,
        "signal_agreement": _fmt(result.signal_agreement, 4),
        "signal_magnitude": _fmt(result.signal_magnitude, 4),
        "signal_strength": STRENGTH_WORDS[result.strength_band],
        "agreement_meaning": AGREEMENT_MEANING,
        "magnitude_meaning": MAGNITUDE_MEANING,

        "agreement_summary": {
            "buy_groups": buy_groups,
            "sell_groups": sell_groups,
            "neutral_or_silent": neutral_or_silent,
            "groups_with_opinion": result.opinionated_groups,
            "groups_total": result.groups_total,
            "raw_signals_fired": result.indicators_directional,
            "indicators_evaluated": result.indicators_evaluated,
            "verdict": verdict,
        },

        "groups": group_view,
        "explanation": build_explanation(result, recommendation),

        "indicators": {
            "macd": _macd_block(result),
            "rsi": _rsi_block(result),
            "moving_averages": _moving_average_block(result),
            "bollinger": _bollinger_block(result),
            "golden_cross": _indicator_block(result, "golden_cross"),
            "momentum_10d": (result.indicator("momentum_10d").raw_values
                             .get("momentum_10d_pct")
                             if result.indicator("momentum_10d") else None),
            "volatility_pct": None,
        },

        "market_regime": result.market_regime,
        "regime_adjustment": _fmt(result.regime_adjustment, 4),
        "regime_adjusted_magnitude": _fmt(result.regime_adjusted_magnitude, 4),
        "regime_note": result.regime_note,
        "market_context": market_context or {},

        "data_quality": dict(result.data_quality),
        "warnings": list(result.warnings),
        "analysis_available": result.analysis_available,
        "execution_available": False,
        "disclaimer": DISCLAIMER,
    }
