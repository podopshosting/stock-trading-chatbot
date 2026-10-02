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

# Every mutation carries this marker, which is what makes a
# leaked mutation detectable on the next run.
MUTATION_MARKER = "MUTATION"

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
BRK_LEDGER = REPO / "agent" / "broker" / "order_ledger.py"
BRK_PROV = REPO / "agent" / "broker" / "provenance.py"
BRK_POLLER = REPO / "agent" / "broker" / "order_poller.py"
BRK_EXEC = REPO / "agent" / "broker" / "execution.py"
BRK_MODELS = REPO / "agent" / "broker" / "models.py"

POS_MODELS = REPO / "agent" / "positions" / "models.py"
POS_ADOPT = REPO / "agent" / "positions" / "adoption.py"

CO_EARNINGS = REPO / "agent" / "company" / "earnings.py"
CO_FUND = REPO / "agent" / "company" / "fundamentals.py"
CO_PEERS = REPO / "agent" / "company" / "peers.py"
CO_DIVS = REPO / "agent" / "company" / "dividends.py"
CO_SPLITS = REPO / "agent" / "company" / "splits.py"
CO_MODELS = REPO / "agent" / "company" / "models.py"
CO_EVIDENCE = REPO / "agent" / "autonomy" / "evidence_class.py"
PROV_BASE = REPO / "agent" / "providers" / "base.py"
RISK_GOV2 = REPO / "agent" / "risk" / "governor.py"
BRK_ALPACA = REPO / "agent" / "broker" / "alpaca_paper.py"
POS_EXITS = REPO / "agent" / "positions" / "exits.py"
POS_MANAGER = REPO / "agent" / "positions" / "manager.py"
AUT_ORDERVIEW = REPO / "agent" / "autonomy" / "order_view.py"

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

API_HANDLER = REPO / "lambda-micro" / "agent-api" / "handler.py"
DASHBOARD = REPO / "web" / "agent" / "index.html"

BRK_STORE = REPO / "agent" / "broker" / "store.py"
POS_STORE = REPO / "agent" / "positions" / "store.py"

EVAL_CAL = REPO / "agent" / "evaluation" / "calibration.py"
EVAL_SWEEP = REPO / "agent" / "evaluation" / "sweep.py"

READINESS = REPO / "agent" / "readiness.py"
LIVE_CONTRACT = REPO / "agent" / "broker" / "live_contract.py"

# Every test module under tests/, discovered - NOT a hand-maintained list.
#
# It was a hand-maintained list of 18 modules, and on 2026-10-01 that let
# 15 new mutations "survive" while the tests that catch them sat in files
# the harness never ran: the company-intelligence and autonomy suites were
# absent, so no mutation in that code could ever fail. A harness reporting
# coverage it does not have is worse than no harness, because the report
# reads like evidence. Discovery means a new test file participates the
# moment it exists.
SUITES = ["discover", "-s", "tests", "-t", "."]


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
        old="    if context.source_age_seconds is None:\n"
            "        rej.add(RejectionCode.STALE_MARKET_DATA,",
        new="    if False:  # MUTATION\n"
            "        rej.add(RejectionCode.STALE_MARKET_DATA,",
        expect=["stale", "unknown", "data age", "fail_closed"],
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
            "                               session_date, realized_pnl_today)",
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
            "            self._raise(result, Condition.CYCLE_LOCK_FAILURE, "
            "str(exc)[:160])\n"
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
    # --- Milestone 14: dashboard and read API ----------------------------
    Mutation(
        name="claim-the-api-can-place-orders",
        description="report an execution capability that does not exist",
        path=API_HANDLER,
        old='        "can_place_orders": False,\n'
            '        "can_place_orders_detail": (',
        new='        "can_place_orders": True,  # MUTATION\n'
            '        "can_place_orders_detail": (',
        expect=["can_place_orders", "paper"],
    ),
    Mutation(
        name="default-the-switches-to-on",
        description="treat a missing environment variable as permission",
        path=API_HANDLER,
        old='def _switch(name: str, default: str = "false") -> bool:',
        new='def _switch(name: str, default: str = "true") -> bool:  # MUTATION',
        expect=["switch", "default", "permission"],
    ),
    Mutation(
        name="report-an-unreadable-halt-as-clear",
        description="show an unknown halt state as safe",
        path=API_HANDLER,
        old="        halt = True\n"
            "        halt_detail = f\"halt state unreadable, assuming halted: {exc}\"",
        new="        halt = False  # MUTATION\n"
            "        halt_detail = \"unknown\"",
        expect=["halt", "unreadable"],
    ),
    Mutation(
        name="add-a-composite-score",
        description="blend the pipeline stages into one number",
        path=API_HANDLER,
        old='        "composite_score": None,',
        new='        "composite_score": 0.5,  # MUTATION',
        expect=["composite", "score"],
    ),
    Mutation(
        name="report-zero-open-risk-when-unknown",
        description="claim there is no risk when the total is unknown",
        path=API_HANDLER,
        # Anchor repaired 2026-10-01: the previous one
        # ('        "total_open_risk": None,') stopped matching when the
        # endpoint began reading the real position store, so this
        # mutation had been silently NOT RUN - providing no coverage
        # while appearing in the list as though it did.
        old='        "total_open_risk": round(sum(risks), 2) if risks else None,',
        new='        "total_open_risk": round(sum(risks), 2) if risks else 0.0,  # MUTATION',
        expect=["open_risk", "None", "zero", "unknown"],
    ),
    Mutation(
        # The same property, lost a second way. Found while repairing the
        # anchor above: nothing asserted it on the unreadable-store path,
        # so this mutation would have survived.
        name="report-zero-open-risk-when-unreadable",
        description="claim zero risk when the position store cannot be read",
        path=API_HANDLER,
        old='            "positions": [], "open_count": None, "total_open_risk": None,',
        new='            "positions": [], "open_count": None, "total_open_risk": 0.0,  # MUTATION',
        expect=["unreadable", "open_risk", "zero"],
    ),
    # --- the read API's own view of the order ledger. A SECOND
    # --- implementation, because the API may not import agent/broker at
    # --- all, so each of these also guards against drift from the
    # --- writer rather than only against a local mistake.
    Mutation(
        name="view-reads-the-wrong-partition",
        description="look in a partition the ledger never writes, so "
                    "the API reports zero outstanding orders",
        path=AUT_ORDERVIEW,
        old='PARTITION_PREFIX = "EXTORDERS#"',
        new='PARTITION_PREFIX = "EXTORDER#"  # MUTATION',
        expect=["partition", "writer", "drift"],
    ),
    Mutation(
        name="view-forgets-never-placed-is-terminal",
        description="let the API and the cycle disagree about which "
                    "statuses are finished",
        path=AUT_ORDERVIEW,
        old='TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED",\n'
            '                      "NEVER_PLACED"})',
        new='TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", '
            '"EXPIRED"})  # MUTATION',
        expect=["terminal", "identical", "drift"],
    ),
    Mutation(
        name="view-treats-every-status-as-terminal",
        description="report every order as finished, so nothing reads "
                    "as outstanding",
        path=AUT_ORDERVIEW,
        old='    return str(row.get("status") or "") in TERMINAL',
        new='    return True  # MUTATION',
        expect=["terminal", "outstanding", "agrees"],
    ),
    Mutation(
        name="view-reports-unestablished-exposure-as-zero",
        description="an order that cannot be sized reserves nothing",
        path=AUT_ORDERVIEW,
        old="    return None\n\n\ndef _age_seconds",
        new="    return 0.0  # MUTATION\n\n\ndef _age_seconds",
        expect=["unsizeable", "unknown", "exposure", "agrees"],
    ),
    Mutation(
        name="view-renders-unknown-exposure-as-a-number",
        description="emit a number for an exposure that could not be "
                    "established, which is the one value a reader acts on",
        path=AUT_ORDERVIEW,
        old='        "committed_exposure": round(total, 6) if known else None,',
        new='        "committed_exposure": round(total, 6),  # MUTATION',
        expect=["unknown", "null", "render"],
    ),
    Mutation(
        name="view-skips-a-malformed-ledger-row",
        description="read past a row it cannot parse, so a partial "
                    "ledger reads as a complete one",
        path=AUT_ORDERVIEW,
        old='                raise OrderViewError(\n'
            '                    "an order ledger row has no payload; a partial read "\n'
            '                    "cannot establish exposure")',
        new='                continue  # MUTATION',
        expect=["malformed", "payload", "skipped", "partial"],
    ),
    # --- exits are as durable as entries. They were not: an exit went
    # --- through DELETE /v2/positions, which accepts no client order id,
    # --- so a lost response left no handle to ask the venue about.
    Mutation(
        name="allow-an-external-exit-with-no-ledger",
        description="send an exit to a network venue with no durable "
                    "record of it anywhere",
        path=BRK_EXEC,
        old='    if external and ledger is None:\n'
            '        raise ExecutionRefused(\n'
            '            "this broker submits to an external venue and no order ledger "\n'
            '            "was supplied; an exit order that is not durably recorded "\n'
            '            "before it is sent cannot be recovered")',
        new='    if False:  # MUTATION\n'
            '        raise ExecutionRefused("x")',
        expect=["exit", "ledger", "refused"],
    ),
    Mutation(
        name="randomise-the-exit-client-id",
        description="give each exit attempt a fresh random id, so a "
                    "retry cannot be suppressed as a duplicate",
        path=BRK_EXEC,
        old='    intent = exit_intent_name(attempt)\n'
            '    digest = hashlib.sha256(\n'
            '        f"{risk_decision_id}:{intent}".encode()).hexdigest()[:20]',
        new='    import uuid  # MUTATION\n'
            '    digest = uuid.uuid4().hex[:20]',
        expect=["deterministic", "exit", "reconstruction", "retry"],
    ),
    Mutation(
        name="reuse-the-first-exit-id-for-a-remainder",
        description="close a remainder under the id the venue has "
                    "already seen, so the order is suppressed and the "
                    "remainder stays open",
        path=BRK_EXEC,
        old='def exit_intent_name(attempt: int = 1) -> str:\n'
            '    return EXIT_INTENT if attempt <= 1 else f"{EXIT_INTENT}#{attempt}"',
        new='def exit_intent_name(attempt: int = 1) -> str:\n'
            '    return EXIT_INTENT  # MUTATION',
        expect=["attempt", "remainder", "collide", "distinguishable"],
    ),
    Mutation(
        name="make-the-exit-limit-reach-the-wrong-way",
        description="price the exit above the market so it does not "
                    "fill and the position stays live",
        path=BRK_EXEC,
        old='    limit = reference_price * (1 - MARKETABLE_LIMIT_BUFFER_PCT / 100.0)',
        new='    limit = reference_price * (1 + MARKETABLE_LIMIT_BUFFER_PCT '
            '/ 100.0)  # MUTATION',
        expect=["limit", "spread", "down"],
    ),
    Mutation(
        name="drop-the-suffixed-exit-intents",
        description="leave later exit attempts out of the provenance "
                    "intents, so a position closed on a second attempt "
                    "cannot be proved to be the agent's own",
        path=BRK_PROV,
        old='DEFAULT_INTENTS = ("ENTRY", "EXIT", "EXIT#2", "EXIT#3")',
        new='DEFAULT_INTENTS = ("ENTRY", "EXIT")  # MUTATION',
        expect=["intent", "provenance", "recognised"],
    ),
    Mutation(
        name="send-every-exit-through-close-position",
        description="keep exits on the endpoint that accepts no client "
                    "order id and so cannot be made idempotent",
        path=POS_MANAGER,
        old='        if (not external or self.order_ledger is None\n'
            '                or self.exit_submitter is None):',
        new='        if True:  # MUTATION',
        expect=["exit", "intent", "recorded", "submit"],
    ),
    Mutation(
        name="advance-the-exit-attempt-on-a-suppressed-retry",
        description="advance the exit attempt on a suppressed retry, so "
                    "the next try mints a new id and defeats the "
                    "venue's duplicate suppression",
        path=POS_MANAGER,
        old='        if cli and cli not in position.exit_client_order_ids:',
        new='        if cli:  # MUTATION',
        expect=["attempt", "retry", "suppress"],
    ),
    # --- adoption: taking back a position the agent provably created.
    # --- The DRAM position on 2026-10-02 was the agent's own and could
    # --- not be closed, because it was not in the store the exit path
    # --- iterates.
    Mutation(
        name="adopt-on-a-prefix-match",
        description="adopt a position on the strength of a client-id "
                    "prefix, which any other client could also choose",
        path=POS_ADOPT,
        old='if verdict["origin"] == ORIGIN_UNKNOWN or not verdict.get("proven"):',
        new='if verdict["origin"] == ORIGIN_UNKNOWN and not '
            'verdict.get("adoptable"):  # MUTATION',
        expect=["prefix", "proven", "layer"],
    ),
    Mutation(
        name="drop-the-second-evidence-check",
        description="rely on the proven flag alone, which is defined in "
                    "another module and could change meaning",
        path=POS_ADOPT,
        old='        if verdict["evidence"] not in (EVIDENCE_LEDGER,\n'
            '                                       EVIDENCE_RECONSTRUCTED_ID):',
        new='        if False:  # MUTATION',
        expect=["evidence", "weak", "layer"],
    ),
    Mutation(
        name="adopt-the-same-position-twice",
        description="let repeated discovery create a second managed "
                    "record for one position",
        path=POS_ADOPT,
        old="        if symbol in managed:",
        new="        if False:  # MUTATION",
        expect=["idempotent", "twice", "already"],
    ),
    Mutation(
        name="let-an-unknown-origin-position-permit-entries",
        description="open new exposure beside a position the agent "
                    "cannot explain",
        path=POS_ADOPT,
        old='            out["blocks_new_exposure"] = True\n'
            '            log_event("position_unknown_origin"',
        new='            log_event("position_unknown_origin"  # MUTATION',
        expect=["unknown", "exposure", "block"],
    ),
    Mutation(
        name="treat-unreadable-broker-positions-as-none-held",
        description="an unreadable position list reads as an empty one",
        path=POS_ADOPT,
        old='        out["integrity"] = INTEGRITY_UNKNOWN\n'
            '        out["blocks_new_exposure"] = True\n'
            '        out["errors"].append(f"broker positions unreadable: {exc}")',
        new='        out["errors"].append(  # MUTATION\n'
            '            f"broker positions unreadable: {exc}")',
        expect=["unreadable", "positions", "block"],
    ),
    Mutation(
        name="adopt-a-position-with-no-viable-stop",
        description="adopt under a stop that does not bound the loss to "
                    "the configured per-trade risk limit",
        path=POS_ADOPT,
        old="    if stop <= 0:",
        new="    if False:  # MUTATION",
        expect=["stop", "refused", "viable"],
    ),
    Mutation(
        name="close-a-preexisting-external-position",
        description="treat somebody else's position as ours to flatten",
        path=POS_ADOPT,
        old='        if verdict["origin"] == ORIGIN_PREEXISTING_EXTERNAL:',
        new='        if False:  # MUTATION',
        expect=["preexisting", "closed", "adopted"],
    ),
    Mutation(
        name="reconcile-before-adopting",
        description="latch an emergency stop over a position the agent "
                    "can prove it opened, before recording it",
        path=ORC_DAY,
        old="            self._adopt_external_positions(result, session_date)\n"
            "            self._reconcile(result)",
        new="            self._reconcile(result)  # MUTATION\n"
            "            self._adopt_external_positions(result, session_date)",
        expect=["reconcil", "adopt", "agree"],
    ),
    Mutation(
        name="let-an-unattributable-position-permit-entries",
        description="ignore the adoption block in the entry path",
        path=ORC_DAY,
        old="        if result.exposure_blocked_by_adoption:",
        new="        if False:  # MUTATION",
        expect=["attributed", "adoption", "entries"],
    ),
    Mutation(
        name="prefer-a-reconstructed-stop-over-the-recorded-one",
        description="ignore the stop distance the position was actually "
                    "opened under",
        path=POS_ADOPT,
        old="    if stop_distance_pct:",
        new="    if False:  # MUTATION",
        expect=["recovered", "stored", "preferred", "original"],
    ),
    # --- following up orders whose outcome is not yet known, and
    # --- counting them as exposure before they fill.
    Mutation(
        name="let-unknown-exposure-permit-entries",
        description="open new exposure while the reserved amount from "
                    "outstanding orders could not be established",
        path=ORC_DAY,
        old="        if self.order_ledger is not None and not "
            "result.committed_exposure_known:",
        new="        if False:  # MUTATION",
        expect=["unknown", "exposure", "entries"],
    ),
    Mutation(
        name="stop-counting-pending-orders-as-exposure",
        description="size a new entry as though an accepted-but-unfilled "
                    "order were not live",
        path=ORC_DAY,
        old="        deployed = capital_deployed + "
            "(result.committed_exposure or 0.0)",
        new="        deployed = capital_deployed  # MUTATION",
        expect=["pending", "ceiling", "capital", "exposure"],
    ),
    Mutation(
        name="never-poll-outstanding-orders",
        description="leave every non-terminal order unfollowed, so an "
                    "order's outcome is never learned",
        path=ORC_DAY,
        old="            self._poll_external_orders(result, session_date)",
        new="            pass  # MUTATION",
        expect=["poll", "exposure", "integrity"],
    ),
    Mutation(
        name="treat-a-failed-lookup-as-a-confirmed-absence",
        description="conclude an order was never placed because the venue "
                    "could not be asked",
        path=BRK_POLLER,
        old="        if not confirmed:",
        new="        if False:  # MUTATION",
        expect=["absence", "silence", "lookup", "vanished"],
    ),
    Mutation(
        name="drop-the-absence-grace-period",
        description="conclude an order was never placed the instant the "
                    "venue's list does not show it yet",
        path=BRK_POLLER,
        old="ABSENCE_GRACE_SECONDS = 60.0",
        new="ABSENCE_GRACE_SECONDS = 0.0  # MUTATION",
        expect=["grace", "absence", "inside"],
    ),
    Mutation(
        name="mark-a-previously-seen-order-as-never-placed",
        description="let an order we have watched at the venue be "
                    "concluded never to have existed",
        path=BRK_POLLER,
        old="        if row.submission_outcome_known:",
        new="        if False:  # MUTATION",
        expect=["seen", "never_placed", "vanished"],
    ),
    Mutation(
        name="report-a-bounded-poll-as-complete",
        description="say the sweep was complete while a backlog of "
                    "outstanding orders was not polled",
        path=BRK_POLLER,
        old='        result["integrity"] = INTEGRITY_PARTIAL\n        result["errors"].append(\n            f"{len(truncated)} order(s) not polled this pass (bound of "',
        new='        result["errors"].append(  # MUTATION\n            f"{len(truncated)} order(s) not polled this pass (bound of "',
        expect=["bound", "partial", "complete"],
    ),
    Mutation(
        name="report-an-unreadable-ledger-as-zero-exposure",
        description="an unreadable ledger reports no outstanding orders "
                    "rather than unknown exposure",
        path=BRK_POLLER,
        old='        result["integrity"] = INTEGRITY_UNKNOWN\n        result["errors"].append(f"ledger unreadable: {exc}")',
        new='        result["exposure_known"] = True  # MUTATION\n        result["errors"].append(f"ledger unreadable: {exc}")',
        expect=["unreadable", "unknown", "exposure"],
    ),
    Mutation(
        name="poll-with-no-lookup-and-call-it-complete",
        description="a broker that cannot be asked about orders reports a "
                    "clean sweep of zero",
        path=BRK_POLLER,
        old='        result["integrity"] = INTEGRITY_UNKNOWN\n        result["errors"].append(\n            "this broker offers no order lookup',
        new='        result["errors"].append(  # MUTATION\n            "this broker offers no order lookup',
        expect=["lookup", "unknown", "sweep"],
    ),
    Mutation(
        name="let-a-filled-order-be-marked-never-placed",
        description="release the reservation on an order that has a "
                    "recorded fill",
        path=BRK_LEDGER,
        old="    if (row.filled_quantity or 0.0) > 0:",
        new="    if False:  # MUTATION",
        expect=["filled", "never", "placed"],
    ),
    # --- the deterministic client order id is derived in TWO modules
    # --- that cannot import each other. One mutation per side, because
    # --- a guard that only catches drift in one direction is half a
    # --- guard.
    Mutation(
        name="drift-the-execution-side-client-id",
        description="change the id execution.py derives so it no longer "
                    "matches the one provenance.py reconstructs",
        path=BRK_EXEC,
        old='        f"{decision.decision_id}:{intent}".encode()).hexdigest()[:20]',
        new='        f"{decision.decision_id}:{intent}:x".encode()'
            ').hexdigest()[:20]  # MUTATION',
        expect=["drift", "derivations", "byte_for_byte", "agree"],
    ),
    Mutation(
        name="drift-the-provenance-side-client-id",
        description="change the id provenance.py reconstructs so it no "
                    "longer matches the one execution.py builds",
        path=BRK_PROV,
        old='        f"{risk_decision_id}:{intent}".encode()'
            ').hexdigest()[:DIGEST_LENGTH]',
        new='        f"{risk_decision_id}|{intent}".encode()'
            ').hexdigest()[:DIGEST_LENGTH]  # MUTATION',
        expect=["drift", "derivations", "byte_for_byte", "dram"],
    ),
    Mutation(
        name="let-the-reference-price-enter-the-client-id",
        description="make a retry at a different price produce a "
                    "different id, defeating the venue's own dedupe",
        path=BRK_EXEC,
        old='        f"{decision.decision_id}:{intent}".encode()).hexdigest()[:20]',
        new='        f"{decision.decision_id}:{intent}:{reference_price}"'
            '.encode()).hexdigest()[:20]  # MUTATION',
        expect=["reference_price", "price", "drift", "agree"],
    ),
    # --- intent before submit. The orphan on 2026-10-02 was a filled
    # --- position that nothing in this system had recorded asking for.
    # --- These seven are the window that produced it.
    Mutation(
        name="submit-first-record-intent-after",
        description="send the order and write the durable intent "
                    "afterwards, recreating the crash window exactly",
        path=BRK_EXEC,
        old="""    stamp = now or _utcnow()
    if ledger is not None:
        already = _recover_or_record_intent(""",
        new="""    stamp = now or _utcnow()
    if False:  # MUTATION
        already = _recover_or_record_intent(""",
        expect=["intent", "before", "recorded", "sent"],
    ),
    Mutation(
        name="swallow-a-failed-intent-write",
        description="let an order go to the venue even though its intent "
                    "could not be recorded",
        path=BRK_EXEC,
        old="""        raise ExecutionRefused(
            f"the order intent for {proposal.client_order_id} could not "
            f"be recorded, so the order was NOT submitted: {exc}") from exc""",
        new="        pass  # MUTATION",
        expect=["intent", "prevents", "submission", "failed"],
    ),
    Mutation(
        name="read-an-unreadable-ledger-as-empty",
        description="treat a ledger that could not be read as proof that "
                    "no such order exists",
        path=BRK_EXEC,
        old="""        raise ExecutionRefused(
            f"the order ledger could not be read for "
            f"{proposal.client_order_id}, so it cannot be established "
            f"whether this order has already been sent: {exc}") from exc""",
        new="        existing = None  # MUTATION",
        expect=["unreadable", "ledger", "prevents"],
    ),
    Mutation(
        name="submit-when-the-venue-cannot-be-asked",
        description="an intent exists and the venue is unreachable, so "
                    "assume the order was never placed and send it",
        path=BRK_EXEC,
        old="""                raise ExecutionRefused(
                    f"an intent already exists for "
                    f"{proposal.client_order_id} and the venue could not "
                    f"be queried to find out whether it was placed: "
                    f"{exc}") from exc""",
        new="                found = None  # MUTATION",
        expect=["unanswerable", "lookup", "refuses", "double"],
    ),
    Mutation(
        name="resubmit-an-order-the-venue-already-has",
        description="recover the existing order and then place a second "
                    "one anyway",
        path=BRK_EXEC,
        old="""                _record_submission(ledger, proposal, found, stamp,
                                   required=False)
                return found""",
        new="""                _record_submission(ledger, proposal, found, stamp,
                                   required=False)  # MUTATION""",
        expect=["venue", "already", "sent", "again"],
    ),
    Mutation(
        name="make-the-ledger-optional-for-an-external-venue",
        description="allow a network submission with no durable record "
                    "of it anywhere",
        path=BRK_EXEC,
        old="    if external and ledger is None:",
        new="    if False:  # MUTATION",
        expect=["external", "ledger", "refuses"],
    ),
    Mutation(
        name="make-a-lost-observation-unsend-the-order",
        description="raise when the observation cannot be stored, "
                    "reporting a failure for an order that exists",
        path=BRK_EXEC,
        old="        _record_submission(ledger, proposal, order, stamp)",
        new="        _record_submission(ledger, proposal, order, stamp,"
            " required=True)  # MUTATION",
        expect=["observation", "unsend", "lost"],
    ),
    # --- external order ledger: every guard here protects against a
    # --- defect that actually happened on 2026-10-02.
    Mutation(
        name="treat-an-unknown-status-as-terminal",
        description="let an order the venue described in words we do not "
                    "recognise count as finished",
        path=BRK_LEDGER,
        old='        return (self.status or "") in TERMINAL',
        new='        return True  # MUTATION',
        expect=["terminal", "nonterminal", "unknown", "live"],
    ),
    Mutation(
        name="let-filled-quantity-decrease",
        description="allow a stale observation to un-fill a position",
        path=BRK_LEDGER,
        old="    row.filled_quantity = max(row.filled_quantity or 0.0, seen)",
        new="    row.filled_quantity = seen  # MUTATION",
        expect=["unfill", "decrease", "out_of_order", "stale"],
    ),
    Mutation(
        name="let-a-repeated-intent-overwrite-observations",
        description="overwrite a record that already carries observed "
                    "broker state",
        path=BRK_LEDGER,
        old="""        existing = self._rows.get(record.client_order_id)
        if existing is not None:""",
        new="""        existing = self._rows.get(record.client_order_id)
        if False:  # MUTATION""",
        expect=["overwrite", "intent", "observation"],
    ),
    Mutation(
        name="accept-an-observation-without-an-intent",
        description="invent a ledger record for an order nobody recorded "
                    "asking for",
        path=BRK_LEDGER,
        old="""        row = self._rows.get(client_order_id)
        if row is None:
            raise OrderLedgerError(""",
        new="""        row = self._rows.get(client_order_id)
        if row is None and False:
            raise OrderLedgerError(""",
        expect=["intent", "attribut", "refus"],
    ),
    Mutation(
        name="treat-unsizeable-exposure-as-zero",
        description="report a total that silently omits an order whose "
                    "size could not be established",
        path=BRK_LEDGER,
        old='    return {"reserved": round(total, 6),',
        new='    unknown = []  # MUTATION\n    return {"reserved": round(total, 6),',
        expect=["unknown", "known", "unestablish"],
    ),
    Mutation(
        name="let-an-accepted-unfilled-order-reserve-nothing",
        description="stop reserving the notional of an order the venue "
                    "has accepted but not yet filled",
        path=BRK_LEDGER,
        old="""        if self.is_terminal and self.submission_outcome_known:
            return 0.0
        if self.requested_notional is not None:""",
        new="""        if True:  # MUTATION
            return 0.0
        if self.requested_notional is not None:""",
        expect=["reserve", "exposure", "notional", "unfilled"],
    ),
    Mutation(
        name="skip-a-malformed-order-row",
        description="drop an unparseable order row and report the rest as "
                    "though the ledger were complete",
        path=BRK_LEDGER,
        old="""            except Exception:                             # noqa: BLE001
                # One unreadable row must not hide the rest, and must not
                # vanish silently either.
                raise OrderLedgerError(""",
        new="""            except Exception:                             # noqa: BLE001
                continue  # MUTATION
                raise OrderLedgerError(""",
        expect=["malformed", "partial", "unreadable"],
    ),
    # --- provenance
    Mutation(
        name="call-an-unreadable-history-preexisting",
        description="decide a position belongs to somebody else because "
                    "the order history could not be read",
        path=BRK_PROV,
        old="""    if broker_orders is None:
        return _result(symbol, ORIGIN_UNKNOWN, EVIDENCE_UNREADABLE,""",
        new="""    if broker_orders is None:
        return _result(symbol, ORIGIN_PREEXISTING_EXTERNAL, EVIDENCE_UNREADABLE,  # MUTATION""",
        expect=["unknown", "preexist", "unreadable"],
    ),
    Mutation(
        name="call-a-prefix-match-proven",
        description="treat a client-id prefix anyone could choose as "
                    "proof the agent created the position",
        path=BRK_PROV,
        old="""           "proven": evidence in (EVIDENCE_LEDGER,
                                  EVIDENCE_RECONSTRUCTED_ID)}""",
        new="""           "proven": True}  # MUTATION""",
        expect=["proven", "prefix", "weaker"],
    ),
    Mutation(
        name="drop-the-paper-banner",
        description="remove the notice that nothing here is real money",
        path=DASHBOARD,
        old="  PAPER ONLY — NO EXECUTION PATH",
        new="  Agent status",
        expect=["PAPER", "paper"],
    ),
    Mutation(
        name="hide-the-evidence-column",
        description="show a metric without saying whether it is evidence",
        path=DASHBOARD,
        old="        ev.appendChild(pill(m.is_evidence ? \"YES\" : \"no\",",
        new="        ev.appendChild(pill(\"-\",  // MUTATION",
        expect=["is_evidence", "evidence"],
    ),
    Mutation(
        name="hide-the-sample-size",
        description="present a metric without its sample size",
        path=DASHBOARD,
        old='        tr.appendChild(el("td", "num", m.sample_size));',
        new='        tr.appendChild(el("td", "num", ""));  // MUTATION',
        expect=["sample_size", "sample size"],
    ),
    Mutation(
        name="stop-disclosing-the-polled-stop-gap",
        description="show a stop price without saying what it does not promise",
        path=DASHBOARD,
        old="        \"An ENGINE_POLLED stop is only checked when a cycle runs. If the \" +\n"
            "        \"agent is not running, that stop does not exist.\"));",
        new="        \"Stops are active.\"));  // MUTATION",
        expect=["ENGINE_POLLED", "does not exist"],
    ),
    # --- Milestone 15: persistence and the pilot -------------------------
    Mutation(
        name="persist-only-open-orders",
        description="lose terminal orders so a retry after restart doubles up",
        path=BRK_STORE,
        old='        "orders": [_order_dict(o) for o in broker._orders.values()],',
        new='        "orders": [_order_dict(o) for o in broker._orders.values()\n'
            '                   if o.is_open],  # MUTATION',
        expect=["terminal", "orders", "idempot", "persist"],
    ),
    Mutation(
        name="drop-the-client-order-id-map",
        description="lose idempotency across a cold start",
        path=BRK_STORE,
        old='        "client_order_ids": dict(broker._client_ids),',
        new='        "client_order_ids": {},  # MUTATION',
        expect=["client", "idempot", "duplicate"],
    ),
    Mutation(
        name="reset-cash-on-restore",
        description="restore the starting cash instead of the actual balance",
        path=BRK_STORE,
        old='        cash=float(acct.get("cash", 0.0)),',
        new='        cash=float(acct.get("starting_cash", 0.0)),  # MUTATION',
        expect=["cash", "reset", "starting"],
    ),
    Mutation(
        name="drop-reserved-cash-on-restore",
        description="release working-order reservations across a restart",
        path=BRK_STORE,
        old='        reserved_cash=float(acct.get("reserved_cash", 0.0)),',
        new="        reserved_cash=0.0,  # MUTATION",
        expect=["reserved", "cash"],
    ),
    Mutation(
        name="treat-an-unreadable-account-as-empty",
        description="return a fresh broker when state cannot be read",
        path=BRK_STORE,
        old="            raise BrokerStateError(\n"
            "                f\"could not read broker state for {account_id}: {exc}\"\n"
            "            ) from exc",
        new="            return None, 0  # MUTATION",
        expect=["unreadable", "raise", "empty"],
    ),
    Mutation(
        name="ignore-the-revision-guard",
        description="let a stale writer overwrite newer state",
        path=BRK_STORE,
        old="        if expected_revision is not None and expected_revision != current:",
        new="        if False:  # MUTATION",
        expect=["revision", "stale", "concurrent"],
    ),
    Mutation(
        name="default-a-missing-stop-on-restore",
        description="reconstruct a position with a stop nobody chose",
        path=POS_STORE,
        old='    if "stop_price" not in plan_data:',
        new="    if False:  # MUTATION",
        expect=["stop", "plan", "unbounded"],
    ),
    Mutation(
        name="allow-a-loosened-stop-on-restore",
        description="restore a stop wider than the position had reached",
        path=POS_STORE,
        old="        if drift > STOP_COMPARISON_TOLERANCE:",
        new="        if False:  # MUTATION",
        expect=["loosen", "wider", "stop"],
    ),
    Mutation(
        name="compare-stops-exactly",
        description="reject legitimate positions over floating point dust",
        path=POS_STORE,
        old="STOP_COMPARISON_TOLERANCE = 1e-4",
        new="STOP_COMPARISON_TOLERANCE = 0.0  # MUTATION",
        expect=["rounding", "dust", "round trip"],
    ),
    Mutation(
        name="lose-the-high-water-mark",
        description="reset the trailing stop ratchet on every restart",
        path=POS_STORE,
        old='        "high_water_price": position.high_water_price,',
        new='        "high_water_price": None,  # MUTATION',
        expect=["high_water", "ratchet", "trail"],
    ),
    Mutation(
        name="treat-unreadable-positions-as-none-held",
        description="believe nothing is held when the store cannot be read",
        path=POS_STORE,
        old="            raise PositionStoreError(\n"
            "                f\"could not read positions for {session_date}: {exc}\"\n"
            "            ) from exc",
        new="            return []  # MUTATION",
        expect=["unreadable", "raise", "positions"],
    ),
    # --- Milestone 16: evaluation and calibration ------------------------
    Mutation(
        name="compare-tiny-strength-bands",
        description="read a trend from bands of two or three trades",
        path=EVAL_CAL,
        old="MIN_BUCKET_SIZE = 15",
        new="MIN_BUCKET_SIZE = 1  # MUTATION",
        expect=["INSUFFICIENT", "band", "comparable"],
    ),
    Mutation(
        name="ignore-interval-overlap-in-calibration",
        description="call an ordering significant when the intervals overlap",
        path=EVAL_CAL,
        old="        significant = bool(monotonic and overlap is False)",
        new="        significant = bool(monotonic)  # MUTATION",
        expect=["significant", "overlap", "chance"],
    ),
    Mutation(
        name="bin-unknown-strength-at-zero",
        description="fabricate a strength reading the system never made",
        path=EVAL_CAL,
        old="            if t.hypothesis_strength is not None",
        new="            if True  # MUTATION",
        expect=["strength", "exclud", "zero"],
    ),
    Mutation(
        name="report-a-correlation-without-an-interval",
        description="quote a coefficient from a handful of points",
        path=EVAL_CAL,
        old="        ci_low = math.tanh(lo)\n        ci_high = math.tanh(hi)",
        new="        ci_low = ci_high = None  # MUTATION",
        expect=["interval", "correlation", "ci_low"],
    ),
    Mutation(
        name="treat-no-variation-as-zero-correlation",
        description="claim no relationship when the question is undefined",
        path=EVAL_CAL,
        old="    if var_x <= 0 or var_y <= 0:",
        new="    if False:  # MUTATION",
        expect=["variation", "undefined", "not zero"],
    ),
    Mutation(
        name="ignore-the-selection-noise-floor",
        description="report the best of many arms as an improvement",
        path=EVAL_SWEEP,
        old="        if gap <= noise_floor:",
        new="        if False:  # MUTATION",
        expect=["noise", "identical", "selection"],
    ),
    Mutation(
        name="pretend-selection-has-no-cost",
        description="set the expected best-of-n gap to zero",
        path=EVAL_SWEEP,
        old="    return spread * max(0.0, root - correction)",
        new="    return 0.0  # MUTATION",
        expect=["noise", "arms", "selection", "floor"],
    ),
    Mutation(
        name="accept-a-winner-without-a-holdout",
        description="confirm a result on the data that selected it",
        path=EVAL_SWEEP,
        old="        elif holdout_data is None:",
        new="        elif False:  # MUTATION",
        expect=["holdout", "selected it"],
    ),
    Mutation(
        name="accept-a-winner-that-failed-out-of-sample",
        description="ignore a negative holdout result",
        path=EVAL_SWEEP,
        old="            elif winner.holdout_expectancy_r <= 0:",
        new="            elif False:  # MUTATION",
        expect=["out of sample", "holdout", "overfit", "fitted"],
    ),
    Mutation(
        name="compare-arms-below-the-minimum",
        description="rank sampling variation across tiny arms",
        path=EVAL_SWEEP,
        old="MIN_TRADES_PER_ARM = 30",
        new="MIN_TRADES_PER_ARM = 1  # MUTATION",
        expect=["INSUFFICIENT", "arm", "comparable"],
    ),
    Mutation(
        name="let-a-sweep-authorise-a-change",
        description="treat any recommendation as permission",
        path=EVAL_SWEEP,
        old="        return self is Recommendation.CANDIDATE_FOR_CHANGE",
        new="        return True  # MUTATION",
        expect=["permits_change", "recommendation"],
    ),
    Mutation(
        name="split-the-holdout-randomly",
        description="contaminate the holdout with the selection regime",
        path=EVAL_SWEEP,
        old="    cut = int(len(items) * (1.0 - holdout))\n"
            "    return list(items[:cut]), list(items[cut:])",
        new="    import random  # MUTATION\n"
            "    shuffled = list(items)\n"
            "    random.Random(0).shuffle(shuffled)\n"
            "    cut = int(len(shuffled) * (1.0 - holdout))\n"
            "    return shuffled[:cut], shuffled[cut:]",
        expect=["chronolog", "random", "split"],
    ),
    # --- Milestones 17-18: live contract and readiness gate --------------
    Mutation(
        name="let-a-paper-adapter-be-live-ready",
        description="drop the paper disqualification",
        path=LIVE_CONTRACT,
        old="        return not self.unmet and not self.is_paper",
        new="        return not self.unmet  # MUTATION",
        expect=["paper", "ready_for_real_money"],
    ),
    Mutation(
        name="trust-a-declared-capability",
        description="count an adapter's own claim as verification",
        path=LIVE_CONTRACT,
        old="        assessment.notes.append(\n"
            "            \"Requirements marked satisfied here are DECLARED by the \"",
        new="        pass  # MUTATION\n"
            "        _unused = (\n"
            "            \"Requirements marked satisfied here are DECLARED by the \"",
        expect=["declared", "verified"],
    ),
    Mutation(
        name="treat-an-opaque-adapter-as-live",
        description="assume a non-describing adapter is not paper",
        path=LIVE_CONTRACT,
        old='    is_paper = bool(capabilities.get("is_paper", True))',
        new='    is_paper = bool(capabilities.get("is_paper", False))  # MUTATION',
        expect=["paper", "capabilities", "opaque"],
    ),
    Mutation(
        name="pass-the-gate-on-a-majority",
        description="treat gates as a score rather than prerequisites",
        path=READINESS,
        old="        return bool(self.gates) and not self.unmet",
        new="        return bool(self.gates) and len(self.unmet) <= len(self.gates) // 2  # MUTATION",
        expect=["unmet", "disqualif", "ready"],
    ),
    Mutation(
        name="treat-an-unknown-gate-as-met",
        description="let an unchecked prerequisite count as satisfied",
        path=READINESS,
        old="        return self is GateStatus.MET",
        new="        return self is not GateStatus.UNMET  # MUTATION",
        expect=["unknown", "unmet", "ready"],
    ),
    Mutation(
        name="pass-an-empty-gate-list",
        description="let a report with no gates read as ready",
        path=READINESS,
        old="        return bool(self.gates) and not self.unmet",
        new="        return not self.unmet  # MUTATION",
        expect=["empty", "ready", "trivial"],
    ),
    Mutation(
        name="count-zero-assessed-trades-as-stops-holding",
        description="read a 0% breach rate over no trades as evidence",
        path=READINESS,
        old="    if breach_rate is None or assessed == 0:",
        new="    if breach_rate is None:  # MUTATION",
        expect=["assessed", "unknown", "stops_hold"],
    ),
    Mutation(
        name="unblock-the-venue-gate",
        description="report an execution venue that does not exist",
        path=READINESS,
        old='        "execution_venue_available", GateCategory.EXECUTION,\n'
            "        GateStatus.BLOCKED,",
        new='        "execution_venue_available", GateCategory.EXECUTION,\n'
            "        GateStatus.MET,  # MUTATION",
        expect=["BLOCKED", "venue", "blocked"],
    ),
    # --- company intelligence and evidence class ----------------------
    Mutation(
        name='swap-eps-estimate-and-actual',
        description='read reportedEPS as the estimate and estimatedEPS as the actual',
        path=CO_EARNINGS,
        old='            report_timing=timing, eps_actual=num(row.get("reportedEPS")),\n            eps_estimate=num(row.get("estimatedEPS")), provenance=prov))',
        new='            report_timing=timing, eps_actual=num(row.get("estimatedEPS")),  # MUTATION\n            eps_estimate=num(row.get("reportedEPS")), provenance=prov))',
        expect=['swap', 'surprise', 'actual'],
    ),
    Mutation(
        name='invent-a-missing-estimate',
        description='treat a missing consensus estimate as zero, so every quarter beats',
        path=CO_EARNINGS,
        old='            return None if v in (None, "", "None") else float(v)',
        new='            return 0.0 if v in (None, "", "None") else float(v)  # MUTATION',
        expect=['missing', 'none', 'estimate', 'invent'],
    ),
    Mutation(
        name='compare-mismatched-fiscal-periods',
        description='call any two periods aligned, comparing unrelated fiscal quarters',
        path=CO_FUND,
        old='    return abs((_d(a.period_end) - _d(b.period_end)).days) <= ALIGN_DAYS \\\n        and kind(a) == kind(b)',
        new='    return True  # MUTATION',
        expect=['mismatch', 'align', 'period'],
    ),
    Mutation(
        name='ttm-skips-the-sequence-checks',
        description='accept four quarters as a TTM without checking that '
                    'they are sequential and non-overlapping',
        path=CO_FUND,
        old='    for a, b in zip(last4, last4[1:]):',
        new='    for a, b in []:  # MUTATION',
        expect=['ttm', 'quarter', 'overlap', 'annual'],
    ),
    Mutation(
        name='stale-fundamentals-read-as-current',
        description='ignore the period age so a three-year-old statement reads CURRENT',
        path=CO_FUND,
        old='    age = (today - _d(period_end)).days',
        new='    age = 0  # MUTATION',
        expect=['stale', 'fresh', 'aging'],
    ),
    Mutation(
        name='accept-an-unrelated-peer',
        description='accept a candidate on sector or market cap alone',
        path=CO_PEERS,
        old='    if not (set(reasons) & INDUSTRY_REASONS):\n        if subject.sector and c.sector and subject.sector != c.sector:\n            return None, ["UNRELATED_BUSINESS", "DIFFERENT_INDUSTRY"]\n        return None, ["DIFFERENT_INDUSTRY"]',
        new='    if not (set(reasons) & INDUSTRY_REASONS):\n        return reasons or ["SAME_SECTOR"], None  # MUTATION',
        expect=['unrelated', 'peer', 'industry', 'sector'],
    ),
    Mutation(
        name='persist-an-llm-invented-peer',
        description="let a language model's suggestion become a stored peer",
        path=CO_PEERS,
        old='        if sym in structured:\n            if s.get("note"):\n                ps.llm_notes[sym] = str(s["note"])[:300]\n        else:',
        new='        if True:  # MUTATION\n            ps.peers.append({"symbol": sym,\n                             "reasons": ["SAME_INDUSTRY"]})\n        else:',
        expect=['llm', 'invent', 'structured'],
    ),
    Mutation(
        name='peer-validation-accepts-any-reason',
        description='let a peer through on any reason string at all',
        path=CO_PEERS,
        old='        if not reasons <= STRUCTURED_REASONS or not (reasons & INDUSTRY_REASONS):',
        new='        if False:  # MUTATION',
        expect=['unstructured', 'invent', 'peer'],
    ),
    Mutation(
        name='one-payment-establishes-a-dividend',
        description='call a single observed payment an ACTIVE dividend',
        path=CO_DIVS,
        old='    if len(regular) < 3:',
        new='    if False:  # MUTATION',
        expect=['one payment', 'single', 'pattern', 'establish'],
    ),
    Mutation(
        name='special-dividends-count-as-regular',
        description='fold special distributions into the regular series',
        path=CO_DIVS,
        old='    regular = sorted((e for e in events if e.kind == "REGULAR"),\n                     key=lambda e: _d(e.ex_date))',
        new='    regular = sorted(events, key=lambda e: _d(e.ex_date))  # MUTATION',
        expect=['special', 'regular', 'irregular'],
    ),
    Mutation(
        name='empty-history-means-no-dividend',
        description='treat an empty provider result as proof the company pays nothing',
        path=CO_DIVS,
        old='        if history_complete:',
        new='        if True:  # MUTATION',
        expect=['unknown', 'partial', 'complete', 'absence'],
    ),
    Mutation(
        name='any-spacing-is-a-regular-dividend',
        description='skip the cadence check, so erratic payments read ACTIVE',
        path=CO_DIVS,
        old='    if near < CADENCE_MAJORITY * len(gaps):',
        new='    if False:  # MUTATION',
        expect=['irregular', 'cadence', 'erratic', 'spacing'],
    ),
    Mutation(
        name='reverse-split-read-as-forward',
        description='report every split as a forward split',
        path=CO_MODELS,
        old='        return SplitType.REVERSE_SPLIT if self.ratio < 1 \\\n            else SplitType.FORWARD_SPLIT',
        new='        return SplitType.FORWARD_SPLIT  # MUTATION',
        expect=['reverse', 'forward', 'split', 'direction'],
    ),
    Mutation(
        name='delayed-data-counts-as-real-time',
        description='let delayed-feed paper fills satisfy a strategy performance gate',
        path=CO_EVIDENCE,
        old='    if quality is DataQuality.REAL_TIME:',
        new='    if True:  # MUTATION',
        expect=['delayed', 'real_time', 'strategy', 'operational'],
    ),
    Mutation(
        name='redeploy-spanning-session-is-countable',
        description='count a session that ran under two code SHAs as one measurement',
        path=CO_EVIDENCE,
        old='    if len(shas) > 1:',
        new='    if False:  # MUTATION',
        expect=['redeploy', 'void', 'span', 'runtime'],
    ),
    # --- real-time data path and paper-only enforcement -----------
    Mutation(
        name='freshness-from-fetch-not-from-the-quote',
        description='fall back to the fetch age when the provider gave no timestamp',
        path=PROV_BASE,
        old='        if not self.as_of:\n            return None',
        new='        if not self.as_of:\n            return self.age_seconds(now)  # MUTATION',
        expect=['unknown', 'fallback', 'fetch', 'freshness', 'source'],
    ),
    Mutation(
        name='governor-checks-the-fetch-age',
        description='check how long ago we downloaded the quote, not how old it is',
        path=RISK_GOV2,
        old='    if context.source_age_seconds is None:',
        new='    if context.quote_age_seconds is None:  # MUTATION',
        expect=['stale', 'data age', 'fetch', 'old'],
    ),
    Mutation(
        name='delayed-sip-labelled-realtime-sip',
        description='classify the 15-minute delayed tape as real-time SIP',
        path=CO_EVIDENCE,
        old='    if name == "delayed_sip":\n        return FeedQuality.DELAYED_SIP',
        new='    if name == "delayed_sip":\n        return FeedQuality.REALTIME_SIP  # MUTATION',
        expect=['delayed', 'realtime_sip', 'strategy', 'operational'],
    ),
    Mutation(
        name='iex-counted-as-the-consolidated-tape',
        description='treat IEX, about 2.5% of volume, as equivalent to full SIP',
        path=CO_EVIDENCE,
        old='    if name == "iex":\n        return FeedQuality.REALTIME_IEX',
        new='    if name == "iex":\n        return FeedQuality.REALTIME_SIP  # MUTATION',
        expect=['iex', 'consolidated', 'realtime_sip', 'strategy'],
    ),
    Mutation(
        name='a-stale-realtime-feed-is-accepted',
        description='skip the data-age check so a halted symbol reads as current',
        path=CO_EVIDENCE,
        old='    if source_age_seconds > max_age_seconds:',
        new='    if False:  # MUTATION',
        expect=['stale', 'old data', 'halted'],
    ),
    Mutation(
        name='no-timestamp-promoted-to-real-time',
        description='treat a missing market-data timestamp as if the data were current',
        path=CO_EVIDENCE,
        old='    if source_age_seconds is None:\n        return FeedQuality.UNKNOWN',
        new='    if source_age_seconds is None:\n        return FeedQuality.REALTIME_SIP  # MUTATION',
        expect=['unknown', 'timestamp', 'absent', 'real-time'],
    ),
    Mutation(
        name='paper-adapter-accepts-the-live-domain',
        description='let the paper adapter be pointed at the live trading domain',
        path=BRK_ALPACA,
        old='def _assert_paper(url: str) -> None:',
        new='def _assert_paper(url: str) -> None:\n    return None  # MUTATION',
        expect=['paper', 'live', 'domain', 'base_url', 'refus'],
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
    """Run the whole discovered test suite, returning pass/fail."""
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
    ap.add_argument("--only", action="append", default=None, metavar="SUBSTR",
                    help="run only mutations whose name contains SUBSTR. "
                         "For iterating on a NEW control; a filtered run is "
                         "not a gate and says so in its own output.")
    args = ap.parse_args()

    mutations = list(MUTATIONS)
    filtered = False
    if args.only:
        wanted = [w.lower() for w in args.only]
        mutations = [m for m in mutations
                     if any(w in m.name.lower() for w in wanted)]
        filtered = True
        if not mutations:
            print(f"no mutation name matches {args.only}")
            return 3

    targets = {m.path for m in mutations}

    # --- pre-flight: refuse to snapshot an already-mutated tree -------
    #
    # This check exists because its absence caused a real incident. The
    # snapshot below is taken from the WORKING TREE, so a mutation left
    # behind by an interrupted run gets recorded as the "original",
    # faithfully restored afterwards, and certified by the checksum as
    # "restored cleanly" - forever. The checksum only ever proved
    # "unchanged since this run started", which is not the same as
    # "matches the real source", and the difference is invisible.
    #
    # Worse, the corrupted file then ships: a disabled spread check
    # reached a deployed Lambda that way.
    #
    # Every mutation writes the marker below, so its presence before any
    # mutation has been applied means a previous run did not clean up.
    contaminated = [p for p in sorted(targets)
                    if MUTATION_MARKER in p.read_text()]
    if contaminated:
        print("=" * 74)
        print("HARNESS ABORTED: source files still contain mutations from a "
              "previous run.")
        print("Snapshotting these would record the mutation as the original "
              "and report it as clean.")
        for path in contaminated:
            print(f"   {path.relative_to(REPO)}")
        print("\nRestore them before running, e.g.:")
        print("   git checkout -- " + " ".join(
            str(p.relative_to(REPO)) for p in contaminated))
        print("=" * 74)
        return 3

    # --- pre-flight: refuse to run against a dirty working tree ------
    #
    # Also from a real incident. This harness rewrites source files in
    # place and restores them from an in-memory snapshot, so anything
    # uncommitted in a mutated file is destroyed by the first restore -
    # and, worse, an edit made DURING a run silently replaces the
    # mutation, so the suite is then run against something that is
    # neither the original nor the mutation and the verdict is
    # meaningless. On 2026-10-01 a concurrent edit produced exactly
    # that: an unrelated test failure attributed to a mutation, and a
    # `pass  # MUTATION` left in agent/journal/models.py.
    #
    # A committed tree makes both impossible to lose and trivial to
    # detect.
    # The WHOLE tree, not just the files this harness rewrites.
    #
    # The narrower check let a real contamination through on
    # 2026-10-02: an edit to lambda-micro/agent-cycle/handler.py landed
    # mid-run, handler.py is not a mutation target, so the check passed
    # - and the post-run baseline then came back RED and every verdict
    # in that run had to be discarded. The harness runs the FULL suite
    # for each mutation, so any file the suite imports can change what
    # a verdict means, not only the files being mutated.
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO, capture_output=True, text=True).stdout.strip()
    if dirty:
        print("=" * 74)
        print("HARNESS ABORTED: the working tree has uncommitted changes.")
        print("The first restore would destroy them, and an edit made while "
              "the run is in")
        print("progress replaces the mutation, making every later verdict "
              "meaningless.")
        for line in dirty.splitlines():
            print(f"   {line}")
        print("\nCommit or stash them, then run this alone and do not edit "
              "the tree until it")
        print("finishes.")
        print("=" * 74)
        return 3

    originals = {p: p.read_text() for p in targets}
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
    if filtered:
        print(f"FILTERED RUN: {len(mutations)} of {len(MUTATIONS)} mutations "
              f"selected by --only.")
        print("This is NOT a gate result. Only an unfiltered run certifies "
              "the suite.\n")

    results = []
    try:
        for mutation in mutations:
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
            # The mutation must still be the thing on disk. If something
            # else wrote to the file while the suite ran, the suite
            # measured neither the original nor the mutation, and the
            # verdict - either verdict - means nothing.
            after = mutation.path.read_text()
            restore()
            if after != mutated:
                print(f"  {mutation.name:34} HARNESS ERROR: the file "
                      f"changed while the suite was running")
                print(f"    something else wrote to "
                      f"{mutation.path.relative_to(REPO)}; this verdict is "
                      f"discarded rather than reported")
                results.append((mutation, None, "concurrent-write"))
                continue

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
    leaked = [p for p in sorted(originals)
              if MUTATION_MARKER in p.read_text()]
    if leaked:
        # Independent of the checksum comparison, which can only detect
        # drift from a snapshot that might itself be contaminated.
        print("\nRESTORE FAILED: mutation markers remain in:")
        for path in leaked:
            print(f"   {path.relative_to(REPO)}")
        print("Run: git checkout -- " + " ".join(
            str(p.relative_to(REPO)) for p in leaked))
        return 3

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
