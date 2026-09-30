"""
Structured per-symbol analysis for the review UI.

Exists because the production `/chatbot` response embeds most of the
reasoning in prose. A frontend that wants to show *which* independent
groups agreed would otherwise have to parse English out of an LLM
paragraph, which is not a data contract.

This module adds no analysis of its own. It calls the same
`ml_agent_lite` engine the production handler uses and reshapes the
result into named fields, so the UI consumes structure rather than text.
Nothing here changes the maths.

A deliberate distinction runs through the output: a group that produced
**no opinion** is not the same as a group that is **neutral**. The
Bollinger/mean-reversion group only fires at band extremes, so most of
the time it is absent — and the UI must say "no signal", not "neutral",
because those are different claims.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

# Human-readable names for the engine's internal signal ids.
SIGNAL_LABELS = {
    "rsi": "RSI",
    "macd": "MACD",
    "momentum_10d": "10-day momentum",
    "ma_crossover": "MA crossover (20/50)",
    "golden_cross": "Golden/death cross (50/200)",
    "bollinger": "Bollinger Bands",
}

GROUP_LABELS = {
    "trend": "Trend",
    "momentum": "Momentum",
    "mean_reversion": "Mean Reversion",
}

GROUP_DESCRIPTIONS = {
    "trend": "Where price sits relative to its moving averages",
    "momentum": "Rate and direction of recent price change",
    "mean_reversion": "Whether price is stretched from its recent range",
}

ALL_GROUPS = ("trend", "momentum", "mean_reversion")

# The wording the UI shows instead of a bare percentage. Signal agreement
# is not a probability, and the label has to make that hard to misread.
AGREEMENT_MEANING = (
    "Signal agreement reflects how strongly independent analysis groups "
    "agree with each other, and how strong the signals within them are. "
    "It is NOT the probability of a price move, a forecast, or the chance "
    "of a profitable trade."
)

DISCLAIMER = (
    "This is technical analysis of past price data, not investment advice. "
    "It does not predict future prices. No orders can be placed from this "
    "system."
)


def _strength_label(recommendation: str, agreement: float,
                    supporting: int, opposing: int) -> str:
    """Words first, number second.

    "Strong" / "Mixed" is what a reader can act on; 0.72 is not, and a
    bare percentage next to a direction invites being read as a
    probability.
    """
    if recommendation == "HOLD":
        if supporting == 0 and opposing == 0:
            return "No directional signal"
        return "Mixed" if opposing else "Weak"
    if opposing:
        return "Contested"
    if agreement >= 0.70 and supporting >= 2:
        return "Strong"
    if agreement >= 0.55:
        return "Moderate"
    return "Weak"


def _macd_state(macd: Optional[float], signal: Optional[float],
                histogram: Optional[float]) -> Dict:
    """The crossover IS the indicator, so report it, not just the line."""
    if macd is None or signal is None:
        return {"available": False,
                "note": "needs 34 closes for a 9-period signal line"}
    bullish = histogram is not None and histogram > 0
    return {
        "available": True,
        "macd_line": macd,
        "signal_line": signal,
        "histogram": histogram,
        "relation": "MACD > Signal" if bullish else "MACD < Signal",
        "crossover": "bullish" if bullish else "bearish",
        "interpretation": ("Bullish crossover - MACD above its signal line"
                           if bullish else
                           "Bearish crossover - MACD below its signal line"),
        # Stated because a positive MACD line with a negative histogram is
        # the case the old implementation could never report.
        "note": ("The signal line is a 9-period EMA of the MACD line. A "
                 "positive MACD line can still be a bearish crossover if it "
                 "is falling below that average."),
    }


def _rsi_state(rsi: Optional[float]) -> Dict:
    """Bands as the engine actually uses them: it votes only below 30 or
    above 70, and treats 40-60 as a hold. Describing RSI 60 as a buy
    would misrepresent the engine."""
    if rsi is None:
        return {"available": False}
    if rsi < 30:
        zone, reading, engine = "oversold", "Oversold", "buy"
    elif rsi > 70:
        zone, reading, engine = "overbought", "Overbought", "sell"
    elif 40 <= rsi <= 60:
        zone, reading, engine = "neutral", "Neutral", "hold"
    else:
        zone, reading, engine = "leaning", (
            "Moderately strong" if rsi > 60 else "Moderately weak"), "none"
    return {
        "available": True, "value": rsi, "zone": zone, "reading": reading,
        "engine_vote": engine,
        "note": ("The engine only votes on RSI below 30 or above 70, and "
                 "treats 40-60 as neutral. A reading above 50 is not "
                 "itself a buy signal."),
    }


def _moving_averages(price: Optional[float], sma20: Optional[float],
                     sma50: Optional[float]) -> Dict:
    if not (price and sma20 and sma50):
        return {"available": False}
    ordered = []
    for label, value in (("Price", price), ("SMA 20", sma20),
                         ("SMA 50", sma50)):
        ordered.append((label, value))
    ordered.sort(key=lambda kv: -kv[1])
    structure = " > ".join(label for label, _v in ordered)
    bullish = price > sma20 > sma50
    bearish = price < sma20 < sma50
    return {
        "available": True,
        "price": price, "sma_20": sma20, "sma_50": sma50,
        "structure": structure,
        "reading": ("Bullish structure" if bullish else
                    "Bearish structure" if bearish else "Mixed structure"),
    }


def _bollinger(price: Optional[float], bands: Optional[Dict]) -> Dict:
    if not bands or price is None:
        return {"available": False,
                "note": "Bollinger Bands only produce a vote at the band "
                        "extremes, so most of the time this group is silent."}
    upper, middle, lower = bands.get("upper"), bands.get("middle"), bands.get("lower")
    if not (upper and middle and lower) or upper <= lower:
        return {"available": False}
    position = (price - lower) / (upper - lower)
    if price > upper:
        where, vote = "Above upper band", "sell"
    elif price < lower:
        where, vote = "Below lower band", "buy"
    elif position >= 0.75:
        where, vote = "Near upper band", "none"
    elif position <= 0.25:
        where, vote = "Near lower band", "none"
    else:
        where, vote = "Mid-range", "none"
    return {
        "available": True,
        "upper": upper, "middle": middle, "lower": lower, "price": price,
        "position_pct": round(position * 100, 1),
        "where": where, "engine_vote": vote,
        "note": ("Being near a band is not itself a signal. The engine only "
                 "votes when price closes outside a band."),
    }


def build_group_view(groups: Dict) -> List[Dict]:
    """One row per correlation group, including groups that stayed silent.

    Showing all three, with absent ones marked "no signal", is what stops
    a reader counting six indicators as six independent opinions.
    """
    out = []
    for name in ALL_GROUPS:
        detail = groups.get(name)
        if not detail:
            out.append({
                "group": name,
                "label": GROUP_LABELS[name],
                "description": GROUP_DESCRIPTIONS[name],
                "direction": "NO_SIGNAL",
                "counted": 0,
                "weight": 0.0,
                "internal_disagreement": False,
                "signals_fired": 0,
                "members": [],
                "summary": "No signal from this group",
            })
            continue

        members = [
            {
                "signal": m.get("name"),
                "label": SIGNAL_LABELS.get(m.get("name"), m.get("name")),
                "direction": (m.get("direction") or "hold").upper(),
                "confidence": m.get("confidence"),
            }
            for m in (detail.get("members") or [])
        ]
        directions = {m["direction"] for m in members
                      if m["direction"] in ("BUY", "SELL")}
        internal_disagreement = len(directions) > 1

        direction = (detail.get("direction") or "hold").upper()
        if direction == "HOLD":
            direction = "NEUTRAL"

        if internal_disagreement:
            summary = ("Signals inside this group disagree, which reduces "
                       "its influence")
        elif direction == "BUY":
            summary = "Supports upward direction"
        elif direction == "SELL":
            summary = "Supports downward direction"
        else:
            summary = "No actionable reading"

        out.append({
            "group": name,
            "label": GROUP_LABELS[name],
            "description": GROUP_DESCRIPTIONS[name],
            "direction": direction,
            "counted": detail.get("counted", 0),
            "weight": detail.get("confidence"),
            "net": detail.get("net"),
            "internal_agreement": detail.get("internal_agreement"),
            "internal_disagreement": internal_disagreement,
            "signals_fired": detail.get("signals_fired", 0),
            "members": members,
            "summary": summary,
        })
    return out


def explain(recommendation: str, group_view: List[Dict]) -> Dict:
    """Deterministic explanation of the vote.

    Built from the structured result, not generated by a model: a language
    model has no business explaining arithmetic it did not perform, and an
    explanation that can drift from the numbers is worse than none.
    """
    supporting = [g for g in group_view
                  if g["direction"] == recommendation and g["counted"]]
    opposing = [g for g in group_view
                if g["direction"] in ("BUY", "SELL")
                and g["direction"] != recommendation and g["counted"]]
    silent = [g for g in group_view if g["direction"] == "NO_SIGNAL"]
    conflicted = [g for g in group_view if g["internal_disagreement"]]

    lines: List[str] = []
    if recommendation == "HOLD":
        directional = [g for g in group_view
                       if g["direction"] in ("BUY", "SELL") and g["counted"]]
        if not directional:
            headline = "Why HOLD?"
            lines.append("No independent group produced a directional signal.")
        else:
            headline = "Why HOLD?"
            names = {g["direction"]: g["label"] for g in directional}
            lines.append(
                f"{' and '.join(sorted(g['label'] for g in directional))} "
                f"point in different directions."
            )
            lines.append(
                "Because the independent groups conflict, there is not "
                "enough agreement for a directional call."
            )
    else:
        headline = f"Why {recommendation}?"
        if len(supporting) >= 2:
            lines.append(
                f"{' and '.join(g['label'] for g in supporting)} "
                f"independently support the same direction."
            )
            lines.append(
                "Agreement across independent groups increases signal "
                "strength."
            )
        elif supporting:
            lines.append(
                f"Only {supporting[0]['label']} supports this direction."
            )
            lines.append(
                "A single group is weaker evidence than agreement across "
                "groups."
            )
        if opposing:
            lines.append(
                f"{' and '.join(g['label'] for g in opposing)} "
                f"argue the other way, which lowers signal agreement."
            )

    for group in conflicted:
        lines.append(
            f"Inside {group['label']}, "
            + " and ".join(
                f"{m['label']} is {m['direction'].lower()}"
                for m in group["members"] if m["direction"] in ("BUY", "SELL")
            )
            + " - that internal disagreement halves the group's weight."
        )

    if silent:
        lines.append(
            f"{' and '.join(g['label'] for g in silent)} produced no signal, "
            f"so {'it was' if len(silent) == 1 else 'they were'} not counted."
        )

    return {
        "headline": headline,
        "lines": lines,
        "supporting_groups": [g["label"] for g in supporting],
        "opposing_groups": [g["label"] for g in opposing],
        "silent_groups": [g["label"] for g in silent],
    }


def build_analysis(symbol: str, quote, daily_closes: List[float],
                   ml_result: Dict, bollinger: Optional[Dict] = None,
                   now: Optional[float] = None) -> Dict:
    """Assemble the structured analysis the review UI consumes."""
    now = now if now is not None else time.time()
    indicators = ml_result.get("indicators") or {}
    groups = (ml_result.get("signals") or {}).get("groups") or {}
    group_view = build_group_view(groups)

    recommendation = (ml_result.get("recommendation") or "HOLD").upper()
    agreement = float(ml_result.get("confidence") or 0.0)

    supporting = sum(1 for g in group_view
                     if g["direction"] == recommendation and g["counted"])
    opposing = sum(1 for g in group_view
                   if g["direction"] in ("BUY", "SELL")
                   and g["direction"] != recommendation and g["counted"])
    neutral = sum(1 for g in group_view
                  if g["direction"] in ("NEUTRAL", "NO_SIGNAL"))
    buy_groups = sum(1 for g in group_view
                     if g["direction"] == "BUY" and g["counted"])
    sell_groups = sum(1 for g in group_view
                      if g["direction"] == "SELL" and g["counted"])

    provenance = getattr(quote, "provenance", None)
    age = None
    if provenance is not None and provenance.retrieved_at is not None:
        age = max(0.0, now - provenance.retrieved_at)

    if age is None:
        freshness = "UNKNOWN"
    elif age <= 300:
        freshness = "FRESH"
    elif age <= 1800:
        freshness = "STALE"
    else:
        freshness = "MISSING"

    return {
        "symbol": symbol.upper(),
        "price": getattr(quote, "price", None),
        "change": getattr(quote, "change", None),
        "change_percent": getattr(quote, "change_percent", None),
        "volume": getattr(quote, "volume", None),
        "previous_close": getattr(quote, "previous_close", None),

        "recommendation": recommendation,
        "signal_agreement": round(agreement, 4),
        "signal_strength": _strength_label(recommendation, agreement,
                                           supporting, opposing),
        "agreement_meaning": AGREEMENT_MEANING,

        # Counted by DIRECTION, not by "agrees with the recommendation".
        # On a HOLD, "0 supporting / 2 opposing" is technically true and
        # useless; what a reader needs is "1 group says BUY, 1 says SELL".
        "agreement_summary": {
            "buy_groups": buy_groups,
            "sell_groups": sell_groups,
            "neutral_or_silent": neutral,
            "supporting": supporting,
            "opposing": opposing,
            "groups_with_opinion": (ml_result.get("signals") or {}).get("total", 0),
            "groups_total": len(ALL_GROUPS),
            "raw_signals_fired": (ml_result.get("signals") or {})
                                 .get("signals_fired", 0),
            "verdict": (
                "No strong directional signal"
                if buy_groups == 0 and sell_groups == 0
                else "Signals disagree"
                if buy_groups and sell_groups
                else "Independent groups agree"
                if max(buy_groups, sell_groups) >= 2
                else "Single group only"
            ),
        },

        "groups": group_view,
        "explanation": explain(recommendation, group_view),

        "indicators": {
            "rsi": _rsi_state(indicators.get("rsi")),
            "macd": _macd_state(indicators.get("macd"),
                                indicators.get("macd_signal"),
                                indicators.get("macd_histogram")),
            "moving_averages": _moving_averages(
                getattr(quote, "price", None), indicators.get("sma_20"),
                indicators.get("sma_50")),
            "bollinger": _bollinger(getattr(quote, "price", None), bollinger),
            "momentum_10d": indicators.get("momentum_10d"),
            "volatility_pct": indicators.get("volatility_pct"),
        },

        "risk_level": ml_result.get("risk_level"),

        "data_quality": {
            "price_source": (provenance.provider if provenance else "unknown"),
            "price_as_of": (provenance.as_of if provenance else None),
            "age_seconds": (None if age is None else round(age, 1)),
            "freshness": freshness,
            "is_delayed": (provenance.is_delayed if provenance else None),
            "feed_note": (provenance.note if provenance else ""),
            "history_bars": len(daily_closes),
            # Named separately so the UI never implies one source for
            # everything.
            "sources": {
                "price_and_bars": (provenance.provider if provenance
                                   else "unknown"),
                "fundamentals": "alphavantage (not used in this analysis)",
            },
        },

        "execution_available": False,
        "disclaimer": DISCLAIMER,
    }
