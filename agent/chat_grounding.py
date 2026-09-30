"""
Grounding for the chat layer.

Turns persisted agent state into a factual context block, and answers the
handful of state questions deterministically without involving a model at
all.

The rule this module exists to enforce: **the chat may explain state, and
may not invent it.** If a field is absent, the answer says so. A model
asked "what is the market regime?" with no state available will otherwise
produce a confident, plausible, fabricated answer - which is exactly the
failure this project already hit when an unrecognised ticker produced
invented penny-stock analysis.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

UNAVAILABLE = "I don't have that yet - the agent has not recorded it."

# Questions answerable from stored state alone, with no model call.
_PATTERNS = [
    ("market_open", r"\b(is|are)\b.*\bmarket\b.*\b(open|closed|trading)\b"),
    ("market_open", r"\bmarket\s+(open|closed|status|session)\b"),
    ("regime", r"\b(market\s+)?regime\b"),
    ("regime_why", r"\bwhy\b.*\b(bullish|bearish|neutral|mixed|volatile|regime)\b"),
    ("agent_state", r"\bwhat\b.*\b(are you doing|agent.*doing|state)\b"),
    ("agent_state", r"\bagent\s+(state|status)\b"),
    ("freshness", r"\b(how\s+)?(fresh|stale|old|current)\b.*\bdata\b"),
    ("freshness", r"\bdata\b.*\b(fresh|stale|how old)\b"),
    ("positions", r"\b(open\s+)?positions?\b"),
    ("pnl", r"\b(p&?l|pnl|profit|made|lost)\b"),
    ("trading_enabled", r"\b(trading|are you)\b.*\b(enabled|live|real money|actually trad)\b"),
]


def classify_question(query: str) -> Optional[str]:
    """Which state question, if any, this text is asking."""
    if not query:
        return None
    text = query.lower()
    for topic, pattern in _PATTERNS:
        if re.search(pattern, text):
            return topic
    return None


def _fmt_age(seconds) -> str:
    if seconds is None:
        return "unknown age"
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s old"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m old"
    return f"{seconds / 3600:.1f}h old"


def build_context(session_dict: Optional[Dict]) -> str:
    """A factual block for a prompt. Never speculative.

    Passed to the model as the only permitted source for state claims.
    """
    if not session_dict:
        return (
            "AGENT STATE: unavailable. No session record could be read.\n"
            "Do not guess any of these values. Say they are unavailable."
        )

    detail = session_dict.get("regime_detail") or {}
    lines: List[str] = [
        "AGENT STATE (the only permitted source for these facts):",
        f"- session_date: {session_dict.get('session_date')}",
        f"- agent_state: {session_dict.get('agent_state')}",
        f"- market_session: {session_dict.get('market_status')}",
        f"- trading_enabled: {session_dict.get('trading_enabled')} "
        f"(this system has no order execution at all)",
        f"- emergency_stop: {session_dict.get('emergency_stop')}",
        f"- daily_risk_lock: {session_dict.get('daily_risk_lock')}",
        f"- market_regime: {session_dict.get('market_regime')}",
        f"- regime_score: {session_dict.get('market_regime_score')}",
        f"- regime_confidence: {session_dict.get('market_regime_confidence')} "
        f"(agreement among inputs and data quality, NOT a probability of a "
        f"market direction)",
        f"- risk_posture: {session_dict.get('risk_posture')} "
        f"(a recommendation to a future risk layer, not a decision)",
        f"- regime_updated_at: {session_dict.get('regime_updated_at')}",
        f"- open_positions: {session_dict.get('open_positions')}",
        f"- realized_pnl: {session_dict.get('realized_pnl')}",
        f"- unrealized_pnl: {session_dict.get('unrealized_pnl')}",
        f"- daily_capital_limit: {session_dict.get('daily_capital_limit')}",
        f"- capital_deployed: {session_dict.get('capital_deployed')}",
    ]

    if detail.get("trend"):
        lines.append(
            f"- regime dimensions: trend={detail.get('trend')}, "
            f"risk_mode={detail.get('risk_mode')}, "
            f"volatility={detail.get('volatility')}, "
            f"breadth_proxy={detail.get('breadth_proxy')}"
        )
    for reason in (detail.get("reasons") or [])[:8]:
        lines.append(f"- reason: {reason}")
    for warning in (detail.get("warnings") or [])[:5]:
        lines.append(f"- warning: {warning}")

    inputs = detail.get("inputs") or {}
    for symbol, info in inputs.items():
        lines.append(
            f"- input {symbol}: price={info.get('price')} "
            f"provider={info.get('provider')} "
            f"age={_fmt_age(info.get('age_seconds'))} "
            f"as_of={info.get('as_of')}"
        )

    lines.append(
        "RULES: state only what appears above. If a value is missing, say it "
        "is unavailable rather than estimating it. Never describe this system "
        "as placing trades."
    )
    return "\n".join(lines)


def answer_from_state(query: str, session_dict: Optional[Dict]) -> Optional[str]:
    """Answer a state question directly, or return None to let chat handle it.

    Deterministic by design: these answers are facts the agent recorded,
    so routing them through a model would add a fabrication risk and buy
    nothing.
    """
    topic = classify_question(query)
    if topic is None:
        return None
    if not session_dict:
        return UNAVAILABLE

    s = session_dict
    detail = s.get("regime_detail") or {}

    if topic == "market_open":
        session = s.get("market_status", "UNKNOWN")
        if session == "UNKNOWN":
            return ("I can't confirm the market session right now - the "
                    "broker clock was unreachable, so I'm treating it as "
                    "unknown rather than guessing.")
        readable = {
            "OPEN": "open for regular trading",
            "PRE_MARKET": "in pre-market",
            "AFTER_HOURS": "in after-hours",
            "CLOSED": "closed",
        }.get(session, session)
        return f"The market is {readable} (as recorded at {s.get('updated_at')})."

    if topic == "regime":
        regime = s.get("market_regime", "UNKNOWN")
        if regime == "UNKNOWN":
            return ("The market regime is UNKNOWN - not enough usable data "
                    "to classify it. I won't guess a direction.")
        return (
            f"Regime: {regime} (score {s.get('market_regime_score')}, "
            f"confidence {s.get('market_regime_confidence')}). "
            f"Trend {detail.get('trend', 'unknown')}, "
            f"volatility {detail.get('volatility', 'unknown')}, "
            f"breadth {detail.get('breadth_proxy', 'unknown')}. "
            f"Risk posture {s.get('risk_posture')}. "
            f"Last evaluated {s.get('regime_updated_at')}. "
            f"Confidence measures how well the inputs agree and how fresh "
            f"they are - it is not a probability that the market will rise."
        )

    if topic == "regime_why":
        reasons = detail.get("reasons") or []
        if not reasons:
            return ("I don't have recorded reasons for the current regime - "
                    "it hasn't been evaluated yet.")
        body = "\n".join(f"  - {r}" for r in reasons[:8])
        return (f"Regime {s.get('market_regime')} "
                f"(score {s.get('market_regime_score')}) because:\n{body}")

    if topic == "agent_state":
        extra = ""
        if s.get("emergency_stop"):
            extra = " An emergency stop is active."
        elif s.get("daily_risk_lock"):
            extra = " A daily risk lock is active."
        return (f"Agent state: {s.get('agent_state')}. "
                f"Market session {s.get('market_status')}.{extra} "
                f"Trading is {'enabled' if s.get('trading_enabled') else 'disabled'}, "
                f"and there is no order execution in this build.")

    if topic == "freshness":
        inputs = detail.get("inputs") or {}
        if not inputs:
            return ("No market data inputs are recorded yet, so I can't tell "
                    "you how fresh they are.")
        parts = [f"{sym} {_fmt_age(i.get('age_seconds'))} from "
                 f"{i.get('provider') or 'unknown provider'}"
                 for sym, i in inputs.items()]
        quality = detail.get("data_quality") or {}
        suffix = ""
        if quality:
            suffix = (f" Of {quality.get('instruments_requested')} instruments: "
                      f"{quality.get('fresh')} fresh, {quality.get('stale')} stale, "
                      f"{quality.get('missing')} missing.")
        return "Market data ages: " + "; ".join(parts) + "." + suffix

    if topic == "positions":
        return (f"Open positions: {s.get('open_positions', 0)}. "
                f"This build has no order execution, so there is nothing to "
                f"open a position with.")

    if topic == "pnl":
        return (f"Realized P&L {s.get('realized_pnl', 0)}, "
                f"unrealized {s.get('unrealized_pnl', 0)}, "
                f"capital deployed {s.get('capital_deployed', 0)} of a "
                f"{s.get('daily_capital_limit')} daily limit. "
                f"These are zero because no trading has occurred - the agent "
                f"cannot place orders in this build.")

    if topic == "trading_enabled":
        return (f"Trading is "
                f"{'enabled' if s.get('trading_enabled') else 'disabled'} in "
                f"state, but more importantly this build contains no order "
                f"execution path of any kind - no broker adapter, paper or "
                f"live. Nothing can be bought or sold.")

    return None
