#!/usr/bin/env python3
"""
Run the strategy against history, or against a named scenario.

    # what does it do when price gaps through the stop?
    python3 scripts/replay.py --scenario gap_through_stop

    # every scenario, as a table
    python3 scripts/replay.py --all-scenarios

    # real history for one symbol
    AWS_PROFILE=mypodops python3 scripts/replay.py --symbol AAPL --days 120

Nothing here touches a broker, an evidence store, the journal tables or
production. It is entirely offline: the replay engine drives the same
signal, hypothesis, risk, position and journal modules the live cycle
uses, over bars supplied from history or invented by a scenario.

Two things this output is NOT:

  * A scenario result is not evidence of edge. The bars are invented.
  * A historical result is not evidence of edge either, until the
    sample-adequacy gates pass. One symbol over 120 days is a sketch.

A run that peeked at future data is reported VOID rather than returned
with a caveat, because a contaminated backtest looks exactly like a
good one.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent.replay import Bar, ReplayConfig, run                # noqa: E402
from agent.replay import scenarios                             # noqa: E402
from agent.risk import RiskLimits                              # noqa: E402


def _permissive_regime(symbol, clock):                         # noqa: ARG001
    return {"regime": "BULLISH", "regime_confidence": 0.8,
            "risk_posture": "NORMAL", "market_session": "OPEN"}


def _load_history(symbol: str, days: int, timeframe: str):
    """Real bars from the provider the live agent uses.

    Returns (bars, note). Raises nothing: a provider failure is returned
    as a note so the caller reports it rather than printing a traceback
    that looks like a code fault.
    """
    try:
        from agent.providers import AlpacaProvider
    except Exception as exc:                              # noqa: BLE001
        return None, f"provider unavailable: {exc}"
    try:
        provider = AlpacaProvider()
        raw = provider.get_bars(symbol, timeframe=timeframe, limit=days)
    except Exception as exc:                              # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"

    bars = []
    for row in raw or []:
        try:
            bars.append(Bar(
                timestamp=str(row.get("timestamp") or row.get("t")),
                open=float(row.get("open", row.get("o"))),
                high=float(row.get("high", row.get("h"))),
                low=float(row.get("low", row.get("l"))),
                close=float(row.get("close", row.get("c"))),
                volume=float(row.get("volume", row.get("v")) or 0.0)))
        except (TypeError, ValueError, KeyError) as exc:
            return None, f"a bar could not be parsed: {exc}"
    if not bars:
        return None, "the provider returned no bars"
    return bars, ""


def _report(label: str, result, extra=None) -> None:
    d = result.as_dict()
    print("=" * 72)
    print(label)
    print("=" * 72)
    if extra:
        for key, value in extra.items():
            print(f"  {key}: {value}")
    print(f"  valid               : {d['valid']}")
    if not d["valid"]:
        print(f"  VOID                : {d['lookahead_detail']}")
    print(f"  bars processed      : {d.get('bars_processed')}")
    print(f"  decisions evaluated : {d.get('decisions_evaluated')}")
    print(f"  entries attempted   : {d.get('entries_attempted')}")
    print(f"  entries filled      : {d.get('entries_filled')}")
    print(f"  exits filled        : {d.get('exits_filled')}")
    print(f"  unfillable orders   : {d.get('unfillable_orders')}")
    print(f"  open at end         : {d.get('positions_open_at_end')}")
    rejections = d.get("rejections") or {}
    if rejections:
        print("  refusals:")
        for code, count in sorted(rejections.items(),
                                  key=lambda kv: -kv[1]):
            print(f"      {code:32} {count}")
    perf = d.get("performance") or {}
    print(f"  trades counted      : {perf.get('trades_counted')}")
    print(f"  net P&L             : {perf.get('total_net_pnl')}")
    print(f"  verdict             : {perf.get('verdict')}")

    # Lead with stop integrity. It is the one number that says whether
    # the risk model held, and it is the question a scenario exists to
    # ask - an aggregate expectancy hides a single catastrophic trade
    # inside an average.
    stops = perf.get("stop_integrity") or {}
    if stops.get("trades_assessed"):
        worst = stops.get("worst_r")
        breaches = stops.get("stop_breaches")
        print("  --- stop integrity ---")
        print(f"  trades assessed     : {stops.get('trades_assessed')}")
        print(f"  stop breaches       : {breaches}")
        print(f"  worst loss (R)      : {worst}")
        if worst is not None:
            if worst < -1.0:
                print(f"  >>> the worst trade lost {abs(worst):.2f}x its "
                      f"PLANNED risk. A stop is an instruction to the "
                      f"market, not a promise from it.")
            else:
                print("  >>> every loss stayed within its planned risk "
                      "in this run")
        if stops.get("breached_symbols"):
            print(f"  breached            : {stops['breached_symbols']}")

    metrics = perf.get("metrics") or {}
    for name, metric in sorted(metrics.items()):
        if not isinstance(metric, dict):
            continue
        print(f"  {name:19} : {metric.get('value')} "
              f"(n={metric.get('sample_size')}, "
              f"{metric.get('adequacy')}, "
              f"evidence={metric.get('is_evidence')})")
    for warning in d.get("warnings") or []:
        print(f"  WARNING             : {warning}")


def _run_scenario(name: str, limits: RiskLimits, verbose: bool) -> int:
    scenario = scenarios.build(name)
    config = ReplayConfig(warmup_bars=scenarios.WARMUP,
                          risk_limits=limits,
                          starting_cash=max(limits.daily_capital_limit * 2,
                                            100.0))
    kwargs = {}
    if scenario.spread_pct is not None:
        kwargs["spread_pct"] = scenario.spread_pct
    result = run(scenario.bars, config,
                 regime_for=scenarios.regime_for(scenario), **kwargs)
    _report(f"SCENARIO: {scenario.name}", result, extra={
        "question": scenario.question,
        "expectation": scenario.expectation,
        **({"note": scenario.note} if scenario.note else {}),
    })
    print(f"\n  {scenarios.SCENARIO_NOT_EVIDENCE}")
    if verbose:
        print(json.dumps(result.as_dict(), indent=2, default=str))
    return 0 if result.valid else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replay the strategy over history or a scenario.")
    ap.add_argument("--scenario")
    ap.add_argument("--all-scenarios", action="store_true")
    ap.add_argument("--list-scenarios", action="store_true")
    ap.add_argument("--symbol")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--timeframe", default="1day")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    limits = RiskLimits()

    if args.list_scenarios:
        for name in sorted(scenarios.ALL):
            scenario = scenarios.build(name)
            print(f"{name:20} {scenario.question}")
        return 0

    if args.all_scenarios:
        failures = []
        for name in sorted(scenarios.ALL):
            if _run_scenario(name, limits, False) != 0:
                failures.append(name)
            print()
        if failures:
            print(f"VOID runs: {failures}")
            return 1
        return 0

    if args.scenario:
        try:
            return _run_scenario(args.scenario, limits, args.verbose)
        except KeyError as exc:
            print(exc)
            return 2

    if args.symbol:
        bars, note = _load_history(args.symbol, args.days, args.timeframe)
        if bars is None:
            print(f"could not load history for {args.symbol}: {note}")
            print("The replay engine is fine; the DATA is missing. These "
                  "are different problems and the output should not "
                  "blur them.")
            return 1
        config = ReplayConfig(warmup_bars=min(200, max(20, len(bars) // 3)),
                              risk_limits=limits)
        result = run({args.symbol: bars}, config,
                     regime_for=_permissive_regime)
        _report(f"HISTORY: {args.symbol} ({len(bars)} {args.timeframe} bars)",
                result, extra={"source": "AlpacaProvider.get_bars"})
        print("\n  NOT EVIDENCE OF EDGE: one symbol is a sketch. The "
              "sample-adequacy gates in /agent/readiness decide when a "
              "record may be claimed as a result.")
        if args.verbose:
            print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0 if result.valid else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
