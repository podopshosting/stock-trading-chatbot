"""
Why the agent did not trade.

A trading agent has to explain inactivity as clearly as activity.
Without this, a quiet day is indistinguishable from a broken one, and
the temptation is to "fix" the strategy when nothing was wrong.

The explanation is a funnel: every symbol that entered the day leaves it
at a named stage, and the stages sum to the number that went in. A
rejection is attributed to the FIRST binding reason in the pipeline
order, because a hypothesis that was too weak was never going to be
sized, and listing both would double-count one decision.

Reasons are grouped into categories that answer different questions:

  MARKET      the opportunity was not there (no signal, no catalyst)
  QUALITY     the instrument was untradeable (spread, liquidity, price)
  DATA        we could not see well enough to act
  BUDGET      the opportunity was there and the room was not
  POLICY      permitted by the market, forbidden by our own rules
  PROTECTION  a protective state was engaged
  TIMING      too late in the session

Only BUDGET and PROTECTION mean "we would have traded otherwise".
Nothing here is a tuning recommendation: a capital ceiling reached is
the system working, not a limit to raise.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

# Pipeline order. A decision is attributed to the first of its reasons
# that appears here, so the explanation names the binding constraint
# rather than every constraint.
STAGE_ORDER = (
    # protective states first: nothing else matters while one is engaged
    "EMERGENCY_STOP", "DAILY_RISK_LOCK", "TRADING_DISABLED",
    "EXECUTION_UNAVAILABLE", "MARKET_CLOSED",
    # could we see?
    "STALE_MARKET_DATA",
    # was there an opportunity at all?
    "HYPOTHESIS_NOT_ACTIONABLE", "HYPOTHESIS_TOO_WEAK",
    "BLOCKING_CONTRADICTION", "IMMINENT_BINARY_EVENT",
    # is the instrument tradeable?
    "SPREAD_TOO_WIDE", "INSUFFICIENT_LIQUIDITY", "PRICE_OUT_OF_RANGE",
    # do our own rules permit it?
    "NOT_LONG_ONLY", "INSTRUMENT_NOT_PERMITTED",
    "AVERAGING_DOWN_PROHIBITED", "ALREADY_HOLDING",
    # is there room?
    "MAX_POSITIONS_REACHED", "MAX_NEW_POSITIONS_REACHED",
    "DAILY_CAPITAL_EXCEEDED", "INSUFFICIENT_CAPITAL",
    "POSITION_SIZE_EXCEEDED", "PER_TRADE_RISK_EXCEEDED",
    # last
    "TOO_LATE_IN_SESSION",
)

CATEGORY = {
    "EMERGENCY_STOP": "PROTECTION", "DAILY_RISK_LOCK": "PROTECTION",
    "TRADING_DISABLED": "PROTECTION", "EXECUTION_UNAVAILABLE": "PROTECTION",
    "MARKET_CLOSED": "TIMING", "TOO_LATE_IN_SESSION": "TIMING",
    "STALE_MARKET_DATA": "DATA",
    "HYPOTHESIS_NOT_ACTIONABLE": "MARKET",
    "HYPOTHESIS_TOO_WEAK": "MARKET",
    "BLOCKING_CONTRADICTION": "MARKET",
    "IMMINENT_BINARY_EVENT": "MARKET",
    "SPREAD_TOO_WIDE": "QUALITY", "INSUFFICIENT_LIQUIDITY": "QUALITY",
    "PRICE_OUT_OF_RANGE": "QUALITY",
    "NOT_LONG_ONLY": "POLICY", "INSTRUMENT_NOT_PERMITTED": "POLICY",
    "AVERAGING_DOWN_PROHIBITED": "POLICY", "ALREADY_HOLDING": "POLICY",
    "MAX_POSITIONS_REACHED": "BUDGET",
    "MAX_NEW_POSITIONS_REACHED": "BUDGET",
    "DAILY_CAPITAL_EXCEEDED": "BUDGET", "INSUFFICIENT_CAPITAL": "BUDGET",
    "POSITION_SIZE_EXCEEDED": "BUDGET",
    "PER_TRADE_RISK_EXCEEDED": "BUDGET",
}

# Plain readings. Each says what happened, not what to change.
MEANING = {
    "HYPOTHESIS_TOO_WEAK": (
        "the signal did not reach the minimum strength; the threshold "
        "exists so that weak readings are not sized as if they were "
        "strong"),
    "HYPOTHESIS_NOT_ACTIONABLE": (
        "no strategy applied to the readings - the indicators did not "
        "agree on a direction"),
    "NOT_LONG_ONLY": (
        "the hypothesis was short, and shorting is not permitted"),
    "INSUFFICIENT_CAPITAL": (
        "the position the risk model wanted was smaller than the minimum "
        "position size, or there was not enough cash for it"),
    "DAILY_CAPITAL_EXCEEDED": (
        "the day's capital ceiling was reached - a ceiling, not a target"),
    "MAX_POSITIONS_REACHED": (
        "already holding the maximum number of concurrent positions"),
    "MAX_NEW_POSITIONS_REACHED": (
        "the day's limit on new positions was reached"),
    "SPREAD_TOO_WIDE": (
        "the quoted spread was wider than the limit, so the entry would "
        "have paid away too much of the expected move"),
    "INSUFFICIENT_LIQUIDITY": (
        "dollar volume was below the floor, or unknown"),
    "STALE_MARKET_DATA": (
        "the market data was too old, or its age was unknown; acting on "
        "a price that may have moved is worse than not acting"),
    "ALREADY_HOLDING": "a position in this symbol was already open",
    "TOO_LATE_IN_SESSION": (
        "too close to the close to open new exposure, since positions "
        "are flattened before the bell"),
    "DAILY_RISK_LOCK": "the daily loss limit was hit and the day is locked",
    "EMERGENCY_STOP": "an emergency stop is engaged and latched",
    "PRICE_OUT_OF_RANGE": "the price was outside the permitted band",
    "PER_TRADE_RISK_EXCEEDED": (
        "the planned risk on the trade exceeded the per-trade limit"),
    "POSITION_SIZE_EXCEEDED": (
        "the position would have been larger than the per-position cap"),
}

# Categories that mean "the opportunity was there, the room was not".
WOULD_HAVE_TRADED = {"BUDGET", "PROTECTION"}


def _first_binding(codes: Iterable[str]) -> Optional[str]:
    present = {c for c in codes if c}
    for code in STAGE_ORDER:
        if code in present:
            return code
    return next(iter(sorted(present)), None)


def explain_inactivity(decisions: Sequence[Dict],
                       scanned: Optional[int] = None,
                       entries: Optional[int] = None) -> Dict:
    """Build the funnel from the stored decision rows.

    `decisions` are the rows the cycle writes, each carrying its outcome
    and, when refused, the Risk Governor's reason codes.
    """
    rows = list(decisions or [])
    entered = [r for r in rows if r.get("outcome") == "ENTERED"]
    refused = [r for r in rows if r.get("outcome") == "REFUSED"]
    not_evaluated = [r for r in rows if r.get("outcome") == "NOT_EVALUATED"]

    by_code: Dict[str, Dict] = {}
    by_category: Dict[str, int] = {}
    for row in refused:
        codes = (row.get("risk") or {}).get("reason_codes") or []
        code = _first_binding(codes) or "UNKNOWN"
        entry = by_code.setdefault(code, {
            "code": code,
            "category": CATEGORY.get(code, "UNKNOWN"),
            "meaning": MEANING.get(code, "no description recorded"),
            "decisions": 0, "symbols": []})
        entry["decisions"] += 1
        symbol = row.get("symbol")
        if symbol and symbol not in entry["symbols"]:
            entry["symbols"].append(symbol)
        cat = entry["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    stages = sorted(by_code.values(),
                    key=lambda e: (-e["decisions"], e["code"]))
    blocked_by_room = sum(v for k, v in by_category.items()
                          if k in WOULD_HAVE_TRADED)

    if entered:
        headline = (f"{len(entered)} position(s) were opened; the rest of "
                    f"the day's candidates were refused for the reasons "
                    f"below.")
    elif not rows:
        headline = ("No candidate reached the Risk Governor. Either the "
                    "scanner returned nothing or no scan was fresh enough "
                    "to use.")
    elif blocked_by_room:
        headline = (f"Opportunities were found and {blocked_by_room} were "
                    f"refused for want of room or because a protective "
                    f"state was engaged - not because the signal was "
                    f"absent.")
    else:
        headline = ("No opportunity met the bar. Nothing was refused for "
                    "want of capital, so the day was quiet because the "
                    "market did not offer a qualifying setup.")

    return {
        "headline": headline,
        "funnel": {
            "symbols_scanned": scanned,
            "decisions_recorded": len(rows),
            "not_evaluated": len(not_evaluated),
            "refused_by_risk": len(refused),
            "entered": len(entered) if entries is None else entries,
        },
        "by_category": by_category,
        "stages": stages,
        "would_have_traded_with_more_room": blocked_by_room,
        "note": ("A reason is attributed to the FIRST binding constraint "
                 "in pipeline order, so the counts name the real blocker "
                 "rather than every box that was ticked. A reached ceiling "
                 "is the system working; it is not a limit to raise."),
    }
