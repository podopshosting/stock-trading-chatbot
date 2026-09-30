#!/usr/bin/env python3
"""
Falsifying controls for the quantitative signal engine.

A test suite that has never been seen to fail is not evidence. Each
mutation below reintroduces a defect this project has actually shipped,
or one the design deliberately rules out, and the suite must turn red.
A mutation that leaves the suite green marks a guard that cannot fail -
which is worse than no guard, because it reads as coverage.

The mutations edit the real source files and are reverted afterwards.
The runner verifies by checksum that every file is byte-identical when
it finishes; anything else is reported as a failure of the harness
itself rather than silently ignored.

Usage:
    python3 scripts/falsifying_controls.py [--verbose]

Exit status:
    0  every mutation was caught
    1  at least one mutation survived
    3  the harness could not run (anchor missing, restore failed)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

REPO = pathlib.Path(__file__).resolve().parent.parent

ENGINE = REPO / "agent" / "signals" / "engine.py"
INDICATORS = REPO / "agent" / "signals" / "indicators.py"

EV_ENGINE = REPO / "agent" / "evidence" / "engine.py"
EV_DEDUP = REPO / "agent" / "evidence" / "dedup.py"
EV_CLASS = REPO / "agent" / "evidence" / "classification.py"
EV_LLM = REPO / "agent" / "evidence" / "llm.py"
EV_SERVICE = REPO / "agent" / "evidence" / "service.py"
EV_MODELS = REPO / "agent" / "evidence" / "models.py"

HYP_ENGINE = REPO / "agent" / "hypothesis" / "engine.py"

RISK_GOV = REPO / "agent" / "risk" / "governor.py"
RISK_MODELS = REPO / "agent" / "risk" / "models.py"

BRK_PAPER = REPO / "agent" / "broker" / "paper.py"
BRK_EXEC = REPO / "agent" / "broker" / "execution.py"
BRK_MODELS = REPO / "agent" / "broker" / "models.py"

POS_MODELS = REPO / "agent" / "positions" / "models.py"
POS_EXITS = REPO / "agent" / "positions" / "exits.py"
POS_MANAGER = REPO / "agent" / "positions" / "manager.py"

JNL_MODELS = REPO / "agent" / "journal" / "models.py"
JNL_METRICS = REPO / "agent" / "journal" / "metrics.py"
JNL_STORE = REPO / "agent" / "journal" / "store.py"
JNL_RECORDER = REPO / "agent" / "journal" / "recorder.py"

RPL_CLOCK = REPO / "agent" / "replay" / "clock.py"
RPL_DATA = REPO / "agent" / "replay" / "data.py"
RPL_BROKER = REPO / "agent" / "replay" / "broker.py"
RPL_ENGINE = REPO / "agent" / "replay" / "engine.py"

ORC_MODELS = REPO / "agent" / "orchestration" / "models.py"
ORC_DAY = REPO / "agent" / "orchestration" / "day.py"
ORC_LOCK = REPO / "agent" / "orchestration" / "lock.py"

SUITES = ["tests.test_signal_engine", "tests.test_signal_statistics",
          "tests.test_signal_equivalence", "tests.test_evidence",
          "tests.test_evidence_service", "tests.test_hypothesis",
          "tests.test_risk", "tests.test_broker",
          "tests.test_positions", "tests.test_journal",
          "tests.test_replay", "tests.test_orchestration"]


@dataclass
class Mutation:
    name: str
    description: str
    path: pathlib.Path
    old: str
    new: str
    # Substrings of test names that MUST fail. Empty means "any failure".
    expect: List[str]


MUTATIONS = [
    Mutation(
        name="macd-fixed-multiplier",
        description="restore `signal = macd * 0.9` (the original defect)",
        path=INDICATORS,
        old='    signal_series = ema_series(macd_line, signal_period)\n'
            '    if not signal_series:\n'
            '        return None\n'
            '\n'
            '    line = macd_line[-1]\n'
            '    sig = signal_series[-1]\n',
        new='    signal_series = ema_series(macd_line, signal_period)\n'
            '    if not signal_series:\n'
            '        return None\n'
            '\n'
            '    line = macd_line[-1]\n'
            '    sig = line * 0.9  # MUTATION\n',
        expect=["macd", "signal_line", "crossover", "equivalence"],
    ),
    Mutation(
        name="ignore-correlation-groups",
        description="count all six indicators as independent opinions",
        path=ENGINE,
        old="    by_group: Dict[str, List[SignalResult]] = "
            "{str(g): [] for g in ALL_GROUPS}\n"
            "    for sig in signals:\n"
            "        by_group.setdefault(str(sig.group), []).append(sig)\n",
        new="    by_group: Dict[str, List[SignalResult]] = "
            "{str(g): [] for g in ALL_GROUPS}\n"
            "    for sig in signals:\n"
            "        # MUTATION: every indicator gets its own group\n"
            "        by_group.setdefault(str(sig.group), []).append(sig)\n"
            "    for g in list(by_group):\n"
            "        if len(by_group[g]) > 1:\n"
            "            first = by_group[g][0]\n"
            "            by_group[g] = [first]\n",
        expect=["one_opinion", "three_agreeing", "group"],
    ),
    Mutation(
        name="no-internal-disagreement-penalty",
        description="stop halving the weight of a self-contradicting group",
        path=ENGINE,
        old="        internal_agreement = 0.5 if conflicted else 1.0",
        new="        internal_agreement = 1.0  # MUTATION",
        expect=["internal_disagreement", "halves", "lowers_agreement"],
    ),
    Mutation(
        name="rsi-above-fifty-is-a-buy",
        description="treat relative strength as a directional vote",
        path=ENGINE,
        old="    elif RSI_NEUTRAL_LOW <= rsi_value <= RSI_NEUTRAL_HIGH:\n"
            "        direction, zone = SignalDirection.NEUTRAL, \"neutral\"\n",
        new="    elif rsi_value > 50:  # MUTATION\n"
            "        direction, zone = SignalDirection.BUY, \"neutral\"\n",
        expect=["rsi", "sixty", "neutral"],
    ),
    Mutation(
        name="near-band-is-directional",
        description="make proximity to a Bollinger band a signal",
        path=ENGINE,
        old="    else:\n"
            "        direction = SignalDirection.NEUTRAL\n"
            "        where = (\"near the upper band\" if position >= 0.75 else\n"
            "                 \"near the lower band\" if position <= 0.25 else\n"
            "                 \"mid-range\")\n",
        new="    elif position >= 0.75:  # MUTATION\n"
            "        direction = SignalDirection.SELL\n"
            "        where = \"near the upper band\"\n"
            "    elif position <= 0.25:  # MUTATION\n"
            "        direction = SignalDirection.BUY\n"
            "        where = \"near the lower band\"\n"
            "    else:\n"
            "        direction = SignalDirection.NEUTRAL\n"
            "        where = \"mid-range\"\n",
        expect=["near_but_not", "bollinger", "mean_reversion"],
    ),
    Mutation(
        name="regime-creates-direction",
        description="let a bullish regime turn a neutral result into a BUY",
        path=ENGINE,
        old="    adjustment = regime_adjustment(result.direction, "
            "result.market_regime)",
        new="    if not result.direction.is_directional and \\\n"
            "            result.market_regime in (\"BULLISH\", "
            "\"STRONG_BULLISH\"):\n"
            "        result.direction = SignalDirection.BUY  # MUTATION\n"
            "    adjustment = regime_adjustment(result.direction, "
            "result.market_regime)",
        expect=["regime_never_manufactures", "frozen_price"],
    ),
    Mutation(
        name="rsi-flat-series-is-overbought",
        description="restore RSI 100 for a series that never moved",
        path=INDICATORS,
        old="        return 100.0 if avg_gain > 0 else 50.0",
        new="        return 100.0  # MUTATION",
        expect=["flat", "frozen"],
    ),
    # --- Milestone 6: evidence & catalysts -------------------------------
    Mutation(
        name="count-syndication-independently",
        description="treat every republication as an independent source",
        path=EV_DEDUP,
        old="    seen = set()\n"
            "    for item in group:\n"
            "        seen.add((str(item.source.source_class), "
            "item.source.provider))\n"
            "    return len(seen)",
        new="    seen = set()\n"
            "    for item in group:\n"
            "        seen.add(item.evidence_id)  # MUTATION\n"
            "    return len(seen)",
        expect=["independent_source", "syndication"],
    ),
    Mutation(
        name="never-group-duplicates",
        description="stop collapsing syndicated retellings into one event",
        path=EV_DEDUP,
        old="    # 2. The same canonical URL.",
        new="    return False, 'MUTATION: grouping disabled'\n"
            "    # 2. The same canonical URL.",
        expect=["syndication", "collapse", "duplicate", "one_event"],
    ),
    Mutation(
        name="shelf-registration-is-dilution",
        description="classify every S-3 as active dilution",
        path=EV_CLASS,
        old='    "S-3": (EvidenceType.SHELF_REGISTRATION, Direction.UNCERTAIN, '
            '0.35,\n'
            "            FinancingStage.ABILITY_TO_ISSUE,",
        new='    "S-3": (EvidenceType.DILUTION, Direction.NEGATIVE, 0.75,\n'
            "            FinancingStage.ACTUAL_OFFERING,  # MUTATION",
        expect=["shelf", "capacity", "financing"],
    ),
    Mutation(
        name="every-insider-sale-is-bearish",
        description="treat tax-withholding dispositions as bearish sales",
        path=EV_CLASS,
        old='    "F": (EvidenceType.OTHER, Direction.NEUTRAL, 0.05,',
        new='    "F": (EvidenceType.INSIDER_SELL, Direction.NEGATIVE, 0.45,  '
            '# MUTATION',
        expect=["tax_withholding", "insider", "bearish"],
    ),
    Mutation(
        name="beat-collapses-to-positive",
        description="report a beat with cut guidance as positive",
        path=EV_CLASS,
        old="    if positives and negatives:\n"
            "        return Direction.MIXED, 0.90, reason",
        new="    if positives and negatives:\n"
            "        return Direction.POSITIVE, 0.90, reason  # MUTATION",
        expect=["mixed", "guidance", "earnings"],
    ),
    Mutation(
        name="drop-source-provenance",
        description="flatten every source to the same reliability",
        path=EV_MODELS,
        old="SOURCE_RELIABILITY = {\n"
            "    SourceClass.PRIMARY: 1.00,",
        new="SOURCE_RELIABILITY = {\n"
            "    SourceClass.PRIMARY: 0.50,  # MUTATION",
        expect=["reliability", "primary", "tier"],
    ),
    Mutation(
        name="stale-evidence-keeps-full-weight",
        description="remove temporal decay",
        path=EV_ENGINE,
        old="    return math.pow(0.5, age_hours / half_life)",
        new="    return 1.0  # MUTATION",
        expect=["decay", "stale", "newer", "outranks"],
    ),
    Mutation(
        name="llm-output-becomes-evidence-unchecked",
        description="accept model facts without checking the quote exists",
        path=EV_LLM,
        old="        if haystack and _normalise(quote)[:120] not in haystack:",
        new="        if False:  # MUTATION",
        expect=["quote", "fabricat", "discard"],
    ),
    Mutation(
        name="provider-failure-erases-everything",
        description="abort collection when any provider fails",
        path=EV_SERVICE,
        # Placed AFTER the loop: clearing inside it did nothing when the
        # failing provider ran first, so the mutation was inert and the
        # harness could not tell a working guard from an untested one.
        old="        company_name = self._company_name(symbol)",
        new="        if failed:\n"
            "            items = []  # MUTATION\n"
            "        company_name = self._company_name(symbol)",
        expect=["provider", "erase", "failing", "survives"],
    ),
    # --- Milestone 7: hypotheses -----------------------------------------
    Mutation(
        name="buy-into-material-negative-news",
        description="stop blocking a long when material negative evidence "
                    "is published",
        path=HYP_ENGINE,
        old='            "MATERIAL_NEGATIVE_EVIDENCE", '
            "ContradictionSeverity.BLOCKING,",
        new='            "MATERIAL_NEGATIVE_EVIDENCE", '
            "ContradictionSeverity.MINOR,  # MUTATION",
        expect=["material_negative", "contradiction"],
    ),
    Mutation(
        name="catalyst-alone-becomes-a-trade",
        description="let a positive catalyst produce a long without price "
                    "confirmation",
        path=HYP_ENGINE,
        old="        return Strategy.NO_VALID_STRATEGY, reasons\n"
            "\n"
            "    return Strategy.NO_VALID_STRATEGY, [",
        new="        return Strategy.MOMENTUM_CATALYST, reasons  # MUTATION\n"
            "\n"
            "    return Strategy.NO_VALID_STRATEGY, [",
        expect=["catalyst_only", "confirm", "not_a_trade"],
    ),
    Mutation(
        name="trade-on-stale-prices",
        description="stop blocking hypotheses built on stale price data",
        path=HYP_ENGINE,
        old="BLOCKING_FRESHNESS = frozenset({\"STALE\", \"MISSING\", "
            "\"UNKNOWN\"})",
        new="BLOCKING_FRESHNESS = frozenset()  # MUTATION",
        expect=["stale"],
    ),
    Mutation(
        name="ignore-hostile-regime",
        description="propose new long exposure into a strongly bearish market",
        path=HYP_ENGINE,
        old='HOSTILE_REGIMES = frozenset({"STRONG_BEARISH"})',
        new="HOSTILE_REGIMES = frozenset()  # MUTATION",
        expect=["hostile", "regime"],
    ),
    Mutation(
        name="hide-the-strength-components",
        description="return an opaque score with no decomposition",
        path=HYP_ENGINE,
        old="    strength = max(0.0, min(1.0, base - min(penalty, 0.9)))\n"
            "    return strength, components",
        new="    strength = max(0.0, min(1.0, base - min(penalty, 0.9)))\n"
            "    return strength, {}  # MUTATION",
        expect=["component", "decompos", "transparen"],
    ),
    # --- Milestone 8: risk governor --------------------------------------
    Mutation(
        name="remove-daily-capital-ceiling",
        description="stop rejecting a trade that exceeds the day's capital",
        path=RISK_GOV,
        old="    if value > remaining + 1e-9:",
        new="    if False:  # MUTATION",
        expect=["capital", "ceiling", "exhausted"],
    ),
    Mutation(
        name="ignore-daily-loss-lock",
        description="allow new exposure after the daily loss limit",
        path=RISK_GOV,
        old="    if context.daily_risk_lock:\n"
            "        rej.add(RejectionCode.DAILY_RISK_LOCK,",
        new="    if False:  # MUTATION\n"
            "        rej.add(RejectionCode.DAILY_RISK_LOCK,",
        expect=["risk_lock", "daily"],
    ),
    Mutation(
        name="accept-stale-prices",
        description="treat an unknown or stale quote as usable",
        path=RISK_GOV,
        old="    if context.quote_age_seconds is None:\n"
            "        rej.add(RejectionCode.STALE_MARKET_DATA,",
        new="    if False:  # MUTATION\n"
            "        rej.add(RejectionCode.STALE_MARKET_DATA,",
        expect=["stale", "quote_age", "fail_closed"],
    ),
    Mutation(
        name="ignore-emergency-stop",
        description="proceed despite a global halt",
        path=RISK_GOV,
        old="    if halt is not None and halt.halted:",
        new="    if False:  # MUTATION",
        expect=["halt", "emergency", "rollover"],
    ),
    Mutation(
        name="remove-max-positions",
        description="allow unlimited concurrent positions",
        path=RISK_GOV,
        old="    if context.open_positions >= limits.max_concurrent_positions:",
        new="    if False:  # MUTATION",
        expect=["max_concurrent", "positions"],
    ),
    Mutation(
        name="remove-spread-rule",
        description="accept any spread",
        path=RISK_GOV,
        old="    elif context.spread_pct > limits.max_spread_pct:",
        new="    elif False:  # MUTATION",
        expect=["spread"],
    ),
    Mutation(
        name="allow-averaging-down",
        description="permit adding to an existing position",
        path=RISK_GOV,
        old="        rej.add(RejectionCode.ALREADY_HOLDING,\n"
            '                f"already holding {symbol}")',
        new="        pass  # MUTATION",
        expect=["averaging", "already_holding"],
    ),
    Mutation(
        name="size-by-conviction-not-risk",
        description="scale the position with hypothesis strength",
        path=RISK_GOV,
        old="    value = min(risk_sized, limits.max_position_value, remaining)",
        new="    value = min(risk_sized * (1 + getattr(hypothesis, "
            "'hypothesis_strength', 0.0)), limits.max_position_value, "
            "remaining)  # MUTATION",
        expect=["conviction", "per_trade", "risk"],
    ),
    Mutation(
        name="agreement-includes-magnitude",
        description="multiply agreement by magnitude, recombining the two "
                    "concepts this milestone separated",
        path=ENGINE,
        old="    agreement = share * internal",
        new="    agreement = share * internal * magnitude  # MUTATION",
        expect=["structural", "agreement"],
    ),
    # --- Milestone 9: paper broker ---------------------------------------
    Mutation(
        name="fill-at-the-mid",
        description="ignore the spread and fill both sides at the midpoint",
        path=BRK_PAPER,
        old="        if side is OrderSide.BUY:\n"
            "            return quote.ask if quote.ask is not None else quote.last\n"
            "        return quote.bid if quote.bid is not None else quote.last",
        new="        return quote.mid  # MUTATION",
        expect=["mid", "spread", "flat", "round"],
    ),
    Mutation(
        name="slippage-in-your-favour",
        description="apply slippage as an improvement rather than a cost",
        path=BRK_PAPER,
        old="        return price + drift if side is OrderSide.BUY else price - drift",
        new="        return price - drift if side is OrderSide.BUY else price + drift  # MUTATION",
        expect=["slippage", "against", "ask", "bid"],
    ),
    Mutation(
        name="no-slippage-at-all",
        description="model a market with no price impact",
        path=BRK_PAPER,
        old="        drift = price * (self.config.slippage_bps / 10_000.0)",
        new="        drift = 0.0  # MUTATION",
        expect=["slippage"],
    ),
    Mutation(
        name="duplicate-client-id-places-second-order",
        description="let a retried submission double the position",
        path=BRK_PAPER,
        old="        if client_order_id in self._client_ids:",
        new="        if False:  # MUTATION",
        expect=["duplicate", "double", "retr", "idempot"],
    ),
    Mutation(
        name="permit-shorting",
        description="allow selling a name that is not held",
        path=BRK_PAPER,
        old="            if held <= 0:",
        new="            if False:  # MUTATION",
        expect=["short"],
    ),
    Mutation(
        name="ignore-buying-power",
        description="fill an order larger than the cash available",
        path=BRK_PAPER,
        old="            if required > self._account.buying_power + 1e-9:",
        new="            if False:  # MUTATION",
        expect=["buying_power", "INSUFFICIENT_BUYING_POWER", "buying power"],
    ),
    Mutation(
        name="stop-reserving-cash-for-working-orders",
        description="let two working orders be sized against the same dollar",
        path=BRK_PAPER,
        old="        self._account.reserved_cash += (",
        new="        self._account.reserved_cash += 0.0 * (  # MUTATION",
        expect=["reserv", "buying_power"],
    ),
    Mutation(
        name="execute-without-approval",
        description="submit an order from an unapproved risk decision",
        path=BRK_EXEC,
        old="    if not getattr(decision, \"approved\", False):",
        new="    if False:  # MUTATION",
        expect=["approv", "refus"],
    ),
    Mutation(
        name="execute-while-execution-unavailable",
        description="ignore the execution_available kill switch",
        path=BRK_EXEC,
        old="    if not execution_available:",
        new="    if False:  # MUTATION",
        expect=["execution_available", "refus", "unavail"],
    ),
    Mutation(
        name="accept-a-mismatched-hypothesis",
        description="reuse one approval to execute a different hypothesis",
        path=BRK_EXEC,
        old="    if hypothesis is not None and decision.hypothesis_id != getattr(\n            hypothesis, \"hypothesis_id\", None):",
        new="    if False:  # MUTATION",
        expect=["mismatch", "hypothes"],
    ),
    Mutation(
        name="random-client-order-id",
        description="derive the client id from chance rather than the decision",
        path=BRK_EXEC,
        old="        f\"{decision.decision_id}:{intent}\".encode()).hexdigest()[:20]",
        new="        repr(id(decision)).encode()).hexdigest()[:20]  # MUTATION",
        expect=["client_order_id", "derived", "retr", "double"],
    ),
    Mutation(
        name="reintroduce-market-orders",
        description="offer an order type that accepts any price",
        path=BRK_MODELS,
        old='    LIMIT = "LIMIT"',
        new='    MARKET = "MARKET"  # MUTATION\n    LIMIT = "LIMIT"',
        expect=["MARKET", "order_types"],
    ),
    # --- Milestone 10: positions and exits -------------------------------
    Mutation(
        name="allow-widening-a-stop",
        description="let a stop be moved away from price to avoid a loss",
        path=POS_MODELS,
        old="        if new_stop < self.plan.stop_price:",
        new="        if False:  # MUTATION",
        expect=["widen", "stop"],
    ),
    Mutation(
        name="hold-when-price-is-unknown",
        description="hold an open position that cannot be priced",
        path=POS_EXITS,
        old="    if price is None or stale:",
        new="    if False:  # MUTATION",
        expect=["price", "stale", "exit", "hold"],
    ),
    Mutation(
        name="trust-a-stale-quote",
        description="evaluate a stop against a quote of any age",
        path=POS_EXITS,
        old="    stale = (context.quote_age_seconds is not None\n"
            "             and context.quote_age_seconds > MAX_QUOTE_AGE_SECONDS)",
        new="    stale = False  # MUTATION",
        expect=["stale", "quote"],
    ),
    Mutation(
        name="let-the-high-water-mark-retreat",
        description="track the latest price instead of the best",
        path=POS_EXITS,
        old="    if position.high_water_price is None or price > position.high_water_price:",
        new="    if True:  # MUTATION",
        expect=["high_water", "retreat", "trail"],
    ),
    Mutation(
        name="trail-from-the-first-tick",
        description="remove the trailing activation threshold",
        path=POS_EXITS,
        old="    if gain_pct < TRAILING_ACTIVATION_PCT:",
        new="    if False:  # MUTATION",
        expect=["trail", "inactive", "earned", "activat"],
    ),
    Mutation(
        name="let-the-trail-loosen-a-tighter-stop",
        description="apply the trailing level even when it is wider",
        path=POS_EXITS,
        old="    if level is None or level <= position.plan.stop_price:",
        new="    if level is None:  # MUTATION",
        expect=["loosen", "tighter", "widen", "trail"],
    ),
    Mutation(
        name="ignore-a-global-halt-for-open-positions",
        description="keep positions open through a global halt",
        path=POS_EXITS,
        old="    if context.global_halt:",
        new="    if False:  # MUTATION",
        expect=["halt"],
    ),
    Mutation(
        name="assume-not-halted-when-unreadable",
        description="treat an unreadable halt state as safe",
        path=POS_EXITS,
        old="    elif not context.halt_state_readable:",
        new="    elif False:  # MUTATION",
        expect=["halt", "unreadable", "readable"],
    ),
    Mutation(
        name="skip-the-end-of-day-flatten",
        description="carry positions overnight",
        path=POS_EXITS,
        old="    if (flatten_at is not None and context.minutes_to_close is not None",
        new="    if (False and flatten_at is not None  # MUTATION",
        expect=["close", "END_OF_DAY", "overnight"],
    ),
    Mutation(
        name="pick-an-arbitrary-primary-reason",
        description="return the first reason found rather than the most protective",
        path=POS_EXITS,
        old="    for reason in EXIT_PRIORITY:\n"
            "        if reason in reasons:\n"
            "            return reason",
        new="    return reasons[0]  # MUTATION",
        expect=["primary", "protective", "priority"],
    ),
    Mutation(
        name="reconcile-optimistically",
        description="report a match whenever the broker can be read",
        path=POS_MANAGER,
        old="        matched = not (agent_only or broker_only or mismatches)",
        new="        matched = True  # MUTATION",
        expect=["match", "reconcil", "halt", "diverg"],
    ),
    Mutation(
        name="leave-the-entry-remainder-working",
        description="manage a partial entry without cancelling the rest",
        path=POS_MANAGER,
        old='        remaining = order.get("remaining_quantity") or 0.0',
        new="        remaining = 0.0  # MUTATION",
        expect=["remainder", "cancel", "partial"],
    ),
    Mutation(
        name="submit-exits-with-execution-unavailable",
        description="ignore the kill switch when closing a position",
        path=POS_MANAGER,
        old="        if not self.execution_available:",
        new="        if False:  # MUTATION",
        expect=["execution_available", "risk", "unavail"],
    ),
    Mutation(
        name="manage-a-position-with-a-stop-above-entry",
        description="accept a stop that is not below the entry price",
        path=POS_MANAGER,
        old="        if plan.stop_price >= price:",
        new="        if False:  # MUTATION",
        expect=["stop", "entry", "below"],
    ),
    # --- Milestone 11: journal and metrics -------------------------------
    Mutation(
        name="count-scratches-as-wins",
        description="inflate the win rate by treating noise as a result",
        path=JNL_MODELS,
        old="        if abs(r) < SCRATCH_THRESHOLD_R:\n"
            "            return TradeOutcome.SCRATCH",
        new="        pass  # MUTATION",
        expect=["scratch", "win_rate", "noise"],
    ),
    Mutation(
        name="include-scratches-in-the-win-rate",
        description="dilute losses by counting scratches in the denominator",
        path=JNL_METRICS,
        old="    return [t for t in _counted(trades)\n"
            "            if t.outcome in (TradeOutcome.WIN, TradeOutcome.LOSS)]",
        new="    return _counted(trades)  # MUTATION",
        expect=["scratch", "denominator", "win_rate"],
    ),
    Mutation(
        name="count-unknown-outcomes-as-neutral",
        description="treat a trade whose result is unknown as data",
        path=JNL_METRICS,
        old="    return [t for t in trades if t.outcome is not TradeOutcome.UNKNOWN]",
        new="    return _all_with_money(trades)  # MUTATION",
        expect=["unknown", "denominator", "exclud"],
    ),
    Mutation(
        name="declare-evidence-without-a-sample",
        description="call a metric evidence regardless of sample size",
        path=JNL_METRICS,
        old="        return (self.adequacy is Adequacy.DEMONSTRATED\n"
            "                and self.excludes_null is True)",
        new="        return True  # MUTATION",
        expect=["evidence", "sample", "demonstrat", "edge"],
    ),
    Mutation(
        name="ignore-the-confidence-interval",
        description="call a metric evidence even when the interval spans the null",
        path=JNL_METRICS,
        old="        return self.ci_low > self.null_value or self.ci_high < self.null_value",
        new="        return True  # MUTATION",
        expect=["null", "interval", "evidence"],
    ),
    Mutation(
        name="lower-the-claim-threshold-to-nothing",
        description="let a handful of trades demonstrate an edge",
        path=JNL_METRICS,
        old="MIN_SAMPLE_FOR_CLAIM = 100",
        new="MIN_SAMPLE_FOR_CLAIM = 1  # MUTATION",
        expect=["sample", "threshold", "demonstrat", "edge"],
    ),
    Mutation(
        name="use-the-normal-approximation",
        description="replace the Wilson interval with the normal one",
        path=JNL_METRICS,
        old="    denom = 1.0 + z * z / n",
        new="    denom = 1.0  # MUTATION",
        expect=["wilson", "interval", "published"],
    ),
    Mutation(
        name="compare-groups-of-any-size",
        description="present a three-trade group as comparable",
        path=JNL_METRICS,
        old='            "comparable": n >= MIN_SAMPLE_PER_GROUP,',
        new='            "comparable": True,  # MUTATION',
        expect=["comparable", "group"],
    ),
    Mutation(
        name="report-infinite-profit-factor",
        description="divide by zero losses and call it quality",
        path=JNL_METRICS,
        old="    value = None if losses <= 0 else wins / losses",
        new="    value = float('inf') if losses <= 0 else wins / losses  # MUTATION",
        expect=["profit_factor", "losses", "sample"],
    ),
    Mutation(
        name="measure-r-against-the-trailed-stop",
        description="rewrite history so trailing exits look like scratches",
        path=JNL_RECORDER,
        old="        if move.get(\"from\") is not None:\n"
            "            planned_stop = move[\"from\"]\n"
            "            break",
        new="        pass  # MUTATION",
        expect=["planned_stop", "entry", "trail"],
    ),
    Mutation(
        name="journal-an-exit-with-no-fill-price",
        description="record a trade whose result is unknown",
        path=JNL_RECORDER,
        old='    if not exit_order or not exit_order.get("average_fill_price"):',
        new="    if False:  # MUTATION",
        expect=["fill price", "unknown", "journal"],
    ),
    Mutation(
        name="allow-the-journal-to-be-rewritten",
        description="let a trade record be overwritten",
        path=JNL_STORE,
        old="        if trade.trade_id in self._trades:",
        new="        if False:  # MUTATION",
        expect=["append", "already", "revis"],
    ),
    Mutation(
        name="trust-derived-values-from-storage",
        description="restore a stored r_multiple instead of recomputing it",
        path=JNL_STORE,
        old="        planned_stop=data[\"planned_stop\"],",
        new="        planned_stop=data.get(\"planned_stop_OVERRIDDEN\", 0.0),  # MUTATION",
        expect=["derived", "recompute", "round trip", "r_multiple"],
    ),
    Mutation(
        name="hide-stop-breaches",
        description="stop flagging losses worse than the planned risk",
        path=JNL_MODELS,
        old="        return r < -1.0",
        new="        return False  # MUTATION",
        expect=["breach", "stop", "planned risk", "1R"],
    ),
    # --- Milestone 12: replay and lookahead ------------------------------
    Mutation(
        name="let-the-clock-go-backwards",
        description="allow a replay to rewind and re-decide",
        path=RPL_CLOCK,
        old="        if index <= self._index:",
        new="        if False:  # MUTATION",
        expect=["forward", "rewind", "clock"],
    ),
    Mutation(
        name="allow-reading-future-bars",
        description="drop the index-based lookahead guard",
        path=RPL_CLOCK,
        old="        if index > self._index:",
        new="        if False:  # MUTATION",
        expect=["future", "lookahead", "visible"],
    ),
    Mutation(
        name="allow-reading-future-timestamps",
        description="drop the time-based lookahead guard",
        path=RPL_CLOCK,
        old="        if timestamp > self._timestamp:",
        new="        if False:  # MUTATION",
        expect=["future", "lookahead", "dated"],
    ),
    Mutation(
        name="treat-undated-evidence-as-visible",
        description="show evidence whose publication time is unknown",
        path=RPL_CLOCK,
        old="        if timestamp is None:\n            return False",
        new="        if timestamp is None:\n            return True  # MUTATION",
        expect=["undated", "visible", "publication"],
    ),
    Mutation(
        name="include-the-next-bar-in-the-history",
        description="let the signal engine see one bar into the future",
        path=RPL_DATA,
        old="        end = min(self._clock.index + 1, len(self._bars))\n"
            "        window = self._bars[:end]\n"
            "        if lookback is not None:\n"
            "            window = window[-lookback:]\n"
            "        return [b.close for b in window]",
        new="        window = self._bars[:self._clock.index + 2]  # MUTATION\n"
            "        if lookback is not None:\n"
            "            window = window[-lookback:]\n"
            "        return [b.close for b in window]",
        expect=["closes", "current bar", "future"],
    ),
    Mutation(
        name="accept-unsorted-bars",
        description="skip the chronological ordering check",
        path=RPL_DATA,
        old="            if later.timestamp <= earlier.timestamp:",
        new="            if False:  # MUTATION",
        expect=["order", "increasing", "chronolog"],
    ),
    Mutation(
        name="invent-a-fill-on-the-final-bar",
        description="fill an order that had no next bar to trade against",
        path=RPL_DATA,
        old="        nxt = self._clock.index + 1\n"
            "        if nxt >= len(self._bars):\n"
            "            return None\n"
            "        return self._bars[nxt].open",
        new="        nxt = min(self._clock.index + 1, len(self._bars) - 1)  # MUTATION\n"
            "        return self._bars[nxt].open",
        expect=["final bar", "NO_NEXT_BAR", "invent", "unfillable"],
    ),
    Mutation(
        name="fill-entries-at-the-deciding-close",
        description="use the price the decision was made on as the fill price",
        path=RPL_BROKER,
        old="        fill_price = series.next_open()",
        new="        fill_price = series.current().close  # MUTATION",
        expect=["next open", "close", "fill"],
    ),
    Mutation(
        name="fill-every-stop-exactly-at-its-level",
        description="ignore gaps so losses are never worse than planned",
        path=RPL_BROKER,
        old="                fill_price = min(stop_price, nxt[\"open\"])",
        new="                fill_price = stop_price  # MUTATION",
        expect=["gap", "stop", "worse"],
    ),
    Mutation(
        name="decide-during-the-warmup",
        description="take trades before the indicators have history",
        path=RPL_ENGINE,
        old="            if index < config.warmup_bars:",
        new="            if False:  # MUTATION",
        expect=["warmup", "decision"],
    ),
    Mutation(
        name="silently-default-a-permissive-regime",
        description="manufacture trades by relaxing the risk posture",
        path=RPL_ENGINE,
        old="    if regime_for is None:",
        new="    if False:  # MUTATION",
        expect=["regime", "failed closed", "warn"],
    ),
    Mutation(
        name="report-a-lookahead-run-as-valid",
        description="return a contaminated result without marking it void",
        path=RPL_ENGINE,
        old="        return not self.lookahead_detected",
        new="        return True  # MUTATION",
        expect=["valid", "void", "lookahead"],
    ),
    Mutation(
        name="drop-the-end-of-data-flatten",
        description="leave open positions out of the results",
        path=RPL_ENGINE,
        old="        _flatten_at_end(manager, series, journal, entry_orders, config, stats)",
        new="        pass  # MUTATION",
        expect=["end of data", "survive", "exits_filled", "open"],
    ),
    # --- Milestone 13: orchestration -------------------------------------
    Mutation(
        name="enter-outside-intraday",
        description="open positions in the opening minutes and pre-close",
        path=ORC_MODELS,
        old="        return self is CyclePhase.INTRADAY",
        new="        return self is not CyclePhase.CLOSED  # MUTATION",
        expect=["INTRADAY", "exposure", "phase"],
    ),
    Mutation(
        name="block-exits-when-halted",
        description="stop closing positions while a halt is in force",
        path=ORC_MODELS,
        old="        return self in (CyclePhase.OPENING, CyclePhase.INTRADAY,\n"
            "                        CyclePhase.PRE_CLOSE)",
        new="        return self is CyclePhase.INTRADAY  # MUTATION",
        expect=["exit", "halt", "pre_close", "PRE_CLOSE"],
    ),
    Mutation(
        name="permit-exposure-despite-halts",
        description="ignore the halt list when deciding on new exposure",
        path=ORC_MODELS,
        old="        return self.phase.permits_new_exposure and not self.halt_reasons",
        new="        return self.phase.permits_new_exposure  # MUTATION",
        expect=["halt", "exposure", "entries"],
    ),
    Mutation(
        name="treat-unknown-market-status-as-open",
        description="assume the market is trading when the provider is silent",
        path=ORC_DAY,
        old='    if status != "OPEN":\n        return CyclePhase.UNKNOWN',
        new='    if False:  # MUTATION\n        return CyclePhase.UNKNOWN',
        expect=["UNKNOWN", "open", "assume"],
    ),
    Mutation(
        name="enter-without-knowing-the-time-to-close",
        description="allow entries when the flatten window cannot be respected",
        path=ORC_DAY,
        old="    if minutes_to_close is None:\n"
            "        # Open but we cannot tell how long is left. Exits are safe;\n"
            "        # entries are not, because the flatten window cannot be\n"
            "        # respected.\n"
            "        return CyclePhase.PRE_CLOSE",
        new="    if minutes_to_close is None:\n"
            "        return CyclePhase.INTRADAY  # MUTATION",
        expect=["to_close", "flatten", "entries"],
    ),
    Mutation(
        name="run-entries-before-exits",
        description="add risk before managing the risk already held",
        path=ORC_DAY,
        old="            self._manage_exits(result, phase, quote_for, minutes_to_close,\n"
            "                               session_date)",
        new="            pass  # MUTATION",
        expect=["exit", "order", "manage"],
    ),
    Mutation(
        name="skip-reconciliation",
        description="act on a position set that may disagree with the broker",
        path=ORC_DAY,
        old="            self._reconcile(result)",
        new="            pass  # MUTATION",
        expect=["reconcil", "diverg", "fiction"],
    ),
    Mutation(
        name="ignore-the-trading-switch",
        description="trade with trading_enabled false",
        path=ORC_DAY,
        old="        if not self.trading_enabled:",
        new="        if False:  # MUTATION",
        expect=["trading_enabled", "TRADING_DISABLED"],
    ),
    Mutation(
        name="ignore-the-execution-switch",
        description="trade with execution_available false",
        path=ORC_DAY,
        old="        if not self.execution_available:\n"
            "            result.halt(HaltReason.EXECUTION_UNAVAILABLE,",
        new="        if False:  # MUTATION\n"
            "            result.halt(HaltReason.EXECUTION_UNAVAILABLE,",
        expect=["execution_available", "EXECUTION_UNAVAILABLE"],
    ),
    Mutation(
        name="assume-not-halted-when-the-store-fails",
        description="treat an unreadable halt state as permission to trade",
        path=ORC_DAY,
        old="            result.halt(HaltReason.HALT_STATE_UNREADABLE, str(exc))",
        new="            pass  # MUTATION",
        expect=["HALT_STATE_UNREADABLE", "unreadable"],
    ),
    Mutation(
        name="proceed-without-the-cycle-lock",
        description="run a second cycle alongside the first",
        path=ORC_DAY,
        old="        if not self._acquire_lock(result):",
        new="        if False:  # MUTATION",
        expect=["duplicate", "concurrent", "lock"],
    ),
    Mutation(
        name="continue-when-the-lock-is-unreadable",
        description="assume no other cycle is running when the lock cannot be read",
        path=ORC_DAY,
        old="            result.add_step(\"acquire_lock\", ok=False,\n"
            "                            detail=f\"lock unreadable: {exc}\")\n"
            "            return False",
        new="            return True  # MUTATION",
        expect=["lock", "duplicate", "concurrent"],
    ),
    Mutation(
        name="let-an-impostor-release-the-lock",
        description="allow a cycle to release a lock it does not hold",
        path=ORC_LOCK,
        old="        if self._holder == cycle_id:",
        new="        if True:  # MUTATION",
        expect=["holder", "release", "impostor"],
    ),
    Mutation(
        name="let-a-cycle-raise-into-the-scheduler",
        description="propagate an unhandled error so the retry reruns everything",
        path=ORC_DAY,
        old="        except Exception as exc:                          # noqa: BLE001\n"
            "            # An unhandled error must not leave the agent believing it\n"
            "            # may trade. Exits above have already run.\n"
            "            result.halt(HaltReason.UNHANDLED_ERROR, str(exc))",
        new="        except Exception as exc:  # MUTATION\n"
            "            raise\n"
            "            result.halt(HaltReason.UNHANDLED_ERROR, str(exc))",
        expect=["abort", "unhandled", "raise"],
    ),
    Mutation(
        name="let-a-provider-error-abort-the-cycle",
        description="stop the exits when a data provider raises",
        path=ORC_DAY,
        old="    try:\n        return fn(*args)\n"
            "    except Exception:                                     # noqa: BLE001\n"
            "        return None",
        new="    return fn(*args)  # MUTATION",
        expect=["provider", "exit", "abort"],
    ),
    Mutation(
        name="skip-the-pre-close-flatten",
        description="carry positions overnight when no exit rule fires",
        path=ORC_DAY,
        old="        if phase.requires_flatten and not intents:",
        new="        if False:  # MUTATION",
        expect=["flatten", "pre_close", "overnight", "PRE_CLOSE"],
    ),
    Mutation(
        name="count-a-journal-failure-as-an-exit-failure",
        description="report a closed position as still at risk",
        path=ORC_DAY,
        old="            result.errors.append(\n"
            "                f\"journal failed for {position.symbol} (the position is \"\n"
            "                f\"closed; only the record is missing): {exc}\")",
        new="            result.exits_failed += 1  # MUTATION",
        expect=["record is missing", "journal"],
    ),
]


def checksum(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _purge_bytecode() -> None:
    """Remove every __pycache__ under the repo.

    CPython validates a .pyc against the source's (mtime, size). One
    mutation here - S-3 -> DILUTION - happens to produce a file of
    EXACTLY the same length, and the write/run/restore cycle completes
    within one second, so both fields matched and the subprocess
    imported the UNMUTATED bytecode. The mutation reached disk and never
    executed, and the harness reported the guard as unable to fail when
    in fact it works.

    A harness that can silently not-apply a mutation is worse than no
    harness: it produces confident, wrong statements about coverage in
    both directions.
    """
    for cache in REPO.rglob("__pycache__"):
        for child in cache.glob("*.pyc"):
            try:
                child.unlink()
            except OSError:
                pass


def run_suites() -> Dict:
    """Run the signal and evidence suites, returning pass/fail."""
    _purge_bytecode()
    env = dict(os.environ)
    # Belt and braces: don't write new bytecode either.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", *SUITES],
        cwd=REPO, capture_output=True, text=True, timeout=900, env=env,
    )
    failing = []
    for line in proc.stderr.splitlines():
        if line.startswith(("FAIL: ", "ERROR: ")):
            failing.append(line.split(" ", 1)[1].strip())
    return {"passed": proc.returncode == 0, "failing": failing,
            "returncode": proc.returncode}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    originals = {p: p.read_text() for p in {m.path for m in MUTATIONS}}
    original_sums = {p: checksum(p) for p in originals}

    def restore() -> None:
        for path, text in originals.items():
            path.write_text(text)

    print("=" * 74)
    print("FALSIFYING CONTROLS - each mutation must turn the suite RED")
    print("=" * 74)

    baseline = run_suites()
    if not baseline["passed"]:
        print("\nHARNESS ABORTED: the suite is already failing before any "
              "mutation.")
        for name in baseline["failing"][:10]:
            print(f"   {name}")
        return 3
    print(f"\nbaseline: suite is GREEN\n")

    results = []
    try:
        for mutation in MUTATIONS:
            source = originals[mutation.path]
            if mutation.old not in source:
                print(f"  {mutation.name:34} HARNESS ERROR: anchor not found")
                results.append((mutation, None, "anchor-missing"))
                continue

            mutated = source.replace(mutation.old, mutation.new, 1)
            mutation.path.write_text(mutated)

            # Prove the mutation is actually on disk. A harness that
            # reports on a mutation it failed to apply is reporting on
            # nothing.
            on_disk = mutation.path.read_text()
            if mutation.new not in on_disk or on_disk == source:
                print(f"  {mutation.name:34} HARNESS ERROR: mutation did "
                      f"not apply")
                restore()
                results.append((mutation, None, "did-not-apply"))
                continue

            outcome = run_suites()
            restore()

            caught = not outcome["passed"]
            matched = []
            if caught and mutation.expect:
                lowered = " ".join(outcome["failing"]).lower()
                matched = [e for e in mutation.expect if e.lower() in lowered]

            status = "CAUGHT" if caught else "SURVIVED  <== GUARD CANNOT FAIL"
            print(f"  {mutation.name:34} {status}")
            print(f"    {mutation.description}")
            if caught:
                print(f"    {len(outcome['failing'])} test(s) failed"
                      + (f"; matched expectations: {matched}" if matched else ""))
                if args.verbose:
                    for name in outcome["failing"][:6]:
                        print(f"       - {name}")
            print()
            results.append((mutation, caught, outcome))
    finally:
        restore()

    # The harness must leave the tree exactly as it found it.
    drift = [str(p.relative_to(REPO)) for p in originals
             if checksum(p) != original_sums[p]]
    if drift:
        print(f"HARNESS ERROR: files not restored: {drift}")
        return 3

    final = run_suites()
    survived = [m.name for m, caught, _ in results if caught is False]
    broken = [m.name for m, caught, _ in results if caught is None]

    print("=" * 74)
    print(f"restored cleanly; suite is "
          f"{'GREEN' if final['passed'] else 'RED'} again")
    print(f"{len(results) - len(survived) - len(broken)}/{len(results)} "
          f"mutations caught")
    if survived:
        print(f"SURVIVED (guards that cannot fail): {survived}")
    if broken:
        print(f"HARNESS ERRORS (anchor missing): {broken}")
    print("=" * 74)

    if broken or not final["passed"]:
        return 3
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
