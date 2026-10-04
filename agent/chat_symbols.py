"""Recognising a stock question, and what it is asking for.

THIS MODULE PARSES. IT DOES NOT CALCULATE.

"Analyze AAPL" must produce the same answer as pressing Analyze in the
UI, which means routing to the SAME service rather than computing a
second opinion here. A chat path with its own arithmetic is a second
system that will disagree with the first, and the disagreement will be
discovered by a user rather than a test.

So this returns an INTENT and a SYMBOL. The caller dispatches.

WHY SYMBOL EXTRACTION IS FUSSY

English is full of three-letter uppercase words. "RUN THE GAP SCENARIO
ON NVDA" contains RUN, THE, GAP and ON. A naive "any 1-5 capitals"
rule picks the first one and analyses a stock called RUN. The stopword
list is therefore load-bearing, and a question with no recognisable
symbol returns None so the caller can ask rather than guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

# Intents, each mapping to a service the UI already uses.
ANALYZE = "ANALYZE_SYMBOL"
WHY_WOULD = "WHY_WOULD_TRADE"
WHY_WOULD_NOT = "WHY_WOULD_NOT_TRADE"
SIGNALS = "SIGNALS_FOR_SYMBOL"
PREDICTION = "PREDICTION_FOR_SYMBOL"
SCENARIO = "RUN_SCENARIO"
REPLAY = "RUN_REPLAY"

# Words that look like tickers and are not. Without this, "RUN THE GAP
# SCENARIO ON NVDA" analyses a stock called RUN.
STOPWORDS = {
    "A", "AN", "AND", "ANY", "ARE", "AS", "AT", "BE", "BUT", "BY",
    "CAN", "DID", "DO", "DOES", "FOR", "GET", "HAS", "HOW", "I", "IF",
    "IN", "IS", "IT", "ITS", "ME", "MY", "NO", "NOT", "OF", "ON", "OR",
    "OUR", "OUT", "RUN", "SO", "THE", "TO", "UP", "US", "WAS", "WE",
    "WHAT", "WHEN", "WHY", "WILL", "WITH", "YOU", "YOUR", "SAY", "SAYS",
    "GAP", "STOP", "ALL", "NEW", "OLD", "NOW", "ONE", "TWO", "SIX",
    "TEN", "DAY", "DAYS", "WEEK", "MONTH", "YEAR", "LAST", "NEXT",
    "FROM", "OVER", "PAST", "THAT", "THIS", "THEM", "THEN", "ABOUT",
    "AGENT", "TRADE", "TRADED", "MODEL", "MODELS", "DATA", "RISK",
    "LONG", "SHORT", "HOLD", "SELL", "BUY", "CASH", "PNL", "EOD",
    "AM", "PM", "ET", "UTC", "USD",
}


@dataclass(frozen=True)
class SymbolIntent:
    intent: Optional[str]
    symbol: Optional[str]
    scenario: Optional[str] = None
    months: Optional[int] = None
    note: Optional[str] = None

    @property
    def actionable(self) -> bool:
        return self.intent is not None and self.symbol is not None


def extract_symbol(query: str) -> Optional[str]:
    """The ticker in a question, or None.

    Prefers an explicitly cased ticker over a word that merely looks
    like one, and returns None rather than guessing - the caller can
    ask, which is better than analysing the wrong stock confidently.
    """
    if not query:
        return None
    # $AAPL is unambiguous and wins.
    dollar = re.search(r"\$([A-Za-z]{1,5}(?:\.[A-Za-z]{1,2})?)\b", query)
    if dollar:
        return dollar.group(1).upper()
    candidates = [w for w in re.findall(r"\b[A-Z]{1,5}(?:\.[A-Z]{1,2})?\b",
                                        query)
                  if w not in STOPWORDS]
    if candidates:
        return candidates[0]
    # Nothing uppercase: try lowercase words against nothing, which
    # means give up. Guessing "apple" -> AAPL would be inventing a
    # mapping this system does not have.
    return None


def classify(query: str) -> SymbolIntent:
    """What a stock question is asking for.

    Order matters: "why wouldn't it trade X" must be tested before
    "why would it trade X", because the second is a substring of the
    first and matching it would invert the question.
    """
    if not query:
        return SymbolIntent(None, None, note="empty question")
    q = query.lower()
    symbol = extract_symbol(query)

    # Scenario and replay first: they name a symbol AND an action, and
    # "run the gap scenario on NVDA" also contains "trade"-adjacent
    # words that the generic branches would match.
    if "scenario" in q:
        scenario = None
        for name in ("gap_through_stop", "gap-through-stop",
                     "gap through stop", "gap_through_target",
                     "trading_halt", "trading halt", "flash_crash",
                     "flash crash", "spread_blowout", "spread blowout",
                     "low_liquidity", "low liquidity", "volatile_chop",
                     "volatile chop", "slow_bleed", "slow bleed",
                     "false_breakout", "false breakout",
                     "momentum_reversal", "momentum reversal",
                     "consecutive_losses", "consecutive losses",
                     "bear_regime", "bear regime", "late_in_session",
                     "late in session", "partial_fills",
                     "partial fills", "grind_up", "grind up",
                     "downtrend", "chop"):
            if name in q:
                scenario = name.replace(" ", "_").replace("-", "_")
                break
        return SymbolIntent(SCENARIO, symbol, scenario=scenario,
                            note=(None if scenario else
                                  "no scenario named; the caller should "
                                  "list the canonical library"))
    if "replay" in q:
        months = None
        m = re.search(r"\b(\d{1,2})\s*month", q)
        if m:
            months = int(m.group(1))
        elif "six month" in q or "6 month" in q:
            months = 6
        elif "year" in q:
            months = 12
        return SymbolIntent(REPLAY, symbol, months=months)

    if "predict" in q or "prediction" in q or "forecast" in q:
        return SymbolIntent(PREDICTION, symbol)
    if "signal" in q:
        return SymbolIntent(SIGNALS, symbol)

    # Negation BEFORE affirmation.
    if re.search(r"why\s+(would\s*n[o']?t|wouldn't|not)", q) \
            or "why isn't" in q or "why is not" in q \
            or "why didn't" in q and symbol:
        return SymbolIntent(WHY_WOULD_NOT, symbol)
    if re.search(r"why\s+would", q):
        return SymbolIntent(WHY_WOULD, symbol)

    if "analyz" in q or "analys" in q:
        return SymbolIntent(ANALYZE, symbol)

    return SymbolIntent(None, symbol, note="not a stock question")


def looks_like_a_symbol_question(query: str) -> bool:
    """Whether to route this to the analysis services at all.

    Requires BOTH a recognised intent and a symbol. An intent with no
    symbol is a question the caller should answer by asking which
    stock, not by analysing one at random.
    """
    return classify(query).actionable
