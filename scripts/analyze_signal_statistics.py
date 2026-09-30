#!/usr/bin/env python3
"""
Statistical controls for the quantitative signal engine.

Hand-written examples only test what the author already suspected. This
harness runs the engine over a large synthetic population and looks for
the failure modes that unit tests structurally cannot see:

  accidental duplicates     two indicators whose votes are near-perfectly
                            correlated are one indicator wearing two hats,
                            and counting them separately inflates apparent
                            agreement
  dead indicators           one that never fires, or always fires the same
                            way, contributes nothing but looks like
                            corroboration
  dominance                 if the final direction always equals one
                            indicator's vote, the other five are decoration
  impossible states         a directional call with no opinionated group,
                            agreement outside [0,1], magnitude on a
                            non-directional result
  sign errors               an indicator that systematically votes against
                            the drift that produced the series

This is the harness that exposed the original MACD defect: the histogram
sign matched the MACD line's sign 300 times out of 300, which no unit
test had noticed because each individual case looked reasonable.

The goal is NOT to make the numbers look good. A high HOLD rate or a
weak indicator may both be correct. The goal is to make the relationships
visible so a broken one cannot hide.

Usage:
    python3 scripts/analyze_signal_statistics.py [--samples 900] [--seed 42]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.signals import engine                          # noqa: E402
from agent.signals.models import (                        # noqa: E402
    DataFreshness, GroupDirection, SignalDirection,
)

INDICATORS = ("ma_crossover", "golden_cross", "rsi", "macd",
              "momentum_10d", "bollinger")

REGIMES = ("STRONG_BULLISH", "BULLISH", "NEUTRAL", "MIXED", "VOLATILE",
           "BEARISH", "STRONG_BEARISH", "UNKNOWN")


# --- population ----------------------------------------------------------

def make_series(rng: random.Random, n: int = 260) -> Dict:
    """One synthetic security, with a deliberately wide spread of shapes.

    Regimes, price levels and volatilities are varied so the statistics
    are not dominated by one kind of market - a population of only calm
    large caps would hide a normalisation that breaks on volatile names.
    """
    shape = rng.choice(["trend_up", "trend_down", "flat", "choppy",
                        "rally_then_roll", "selloff_then_bounce",
                        "accelerating", "decelerating"])
    start = rng.uniform(5.0, 1500.0)
    vol = rng.uniform(0.004, 0.055)

    segments = {
        "trend_up":            [(n, rng.uniform(0.001, 0.006), vol)],
        "trend_down":          [(n, -rng.uniform(0.001, 0.006), vol)],
        "flat":                [(n, 0.0, vol)],
        "choppy":              [(n, 0.0, vol * 2.0)],
        "rally_then_roll":     [(int(n * 0.85), rng.uniform(0.003, 0.009), vol),
                                (n - int(n * 0.85), -rng.uniform(0.008, 0.020), vol)],
        "selloff_then_bounce": [(int(n * 0.85), -rng.uniform(0.003, 0.009), vol),
                                (n - int(n * 0.85), rng.uniform(0.008, 0.020), vol)],
        "accelerating":        [(n // 2, rng.uniform(0.0, 0.002), vol),
                                (n - n // 2, rng.uniform(0.004, 0.010), vol)],
        "decelerating":        [(n // 2, rng.uniform(0.004, 0.010), vol),
                                (n - n // 2, rng.uniform(0.0, 0.002), vol)],
    }[shape]

    prices = [start]
    for count, daily, seg_vol in segments:
        for _ in range(count):
            prices.append(max(0.01,
                              prices[-1] * (1 + daily + rng.gauss(0, seg_vol))))

    net_drift = (prices[-1] - prices[0]) / prices[0]
    return {"prices": prices, "shape": shape, "net_drift": net_drift,
            "volatility": vol, "start": start}


# --- statistics ----------------------------------------------------------

def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        # One of the series never varied: an indicator that always votes
        # the same way. Undefined correlation, and itself a finding.
        return float("nan")
    return num / (dx * dy)


def analyse(samples: int, seed: int) -> Dict:
    rng = random.Random(seed)

    votes: Dict[str, List[int]] = {name: [] for name in INDICATORS}
    strengths: Dict[str, List[float]] = {name: [] for name in INDICATORS}
    fired: Counter = Counter()
    absent: Counter = Counter()
    neutral: Counter = Counter()
    directions: Counter = Counter()
    bands: Counter = Counter()
    group_directions: Dict[str, Counter] = defaultdict(Counter)
    internal_conflicts: Counter = Counter()
    cross_group_conflict = 0
    agreements: List[float] = []
    magnitudes: List[float] = []
    final_votes: List[int] = []
    drifts: List[float] = []
    impossible: List[str] = []

    for i in range(samples):
        sample = make_series(rng)
        regime = rng.choice(REGIMES)
        result = engine.evaluate(f"S{i}", sample["prices"],
                                 DataFreshness.FRESH, regime)
        if not result.analysis_available:
            continue

        directions[str(result.direction)] += 1
        bands[str(result.strength_band)] += 1
        agreements.append(result.signal_agreement)
        magnitudes.append(result.signal_magnitude)
        final_votes.append(result.direction.vote)
        drifts.append(sample["net_drift"])

        for sig in result.indicator_results:
            votes[sig.indicator].append(sig.direction.vote)
            if sig.direction is SignalDirection.NO_SIGNAL:
                absent[sig.indicator] += 1
            elif sig.direction is SignalDirection.NEUTRAL:
                neutral[sig.indicator] += 1
            else:
                fired[sig.indicator] += 1
                strengths[sig.indicator].append(sig.strength)

        for group in result.group_results:
            group_directions[str(group.group)][str(group.direction)] += 1
            if group.internal_disagreement:
                internal_conflicts[str(group.group)] += 1

        if result.buy_groups and result.sell_groups:
            cross_group_conflict += 1

        # --- invariants that must never be violated ---
        if result.direction.is_directional and result.opinionated_groups == 0:
            impossible.append(f"S{i}: directional with no opinionated group")
        if not result.direction.is_directional and result.signal_magnitude > 0:
            impossible.append(f"S{i}: magnitude on a non-directional result")
        if not (0.0 <= result.signal_agreement <= 1.0):
            impossible.append(f"S{i}: agreement {result.signal_agreement}")
        if not (0.0 <= result.signal_magnitude <= 1.0):
            impossible.append(f"S{i}: magnitude {result.signal_magnitude}")
        if result.buy_groups and result.sell_groups \
                and result.direction.is_directional \
                and result.buy_groups == result.sell_groups:
            impossible.append(f"S{i}: directional on an even group split")

    evaluated = sum(directions.values())

    # Pairwise correlation of directional votes.
    correlations = {}
    for a_idx, a in enumerate(INDICATORS):
        for b in INDICATORS[a_idx + 1:]:
            xs, ys = votes[a], votes[b]
            if len(xs) == len(ys) and xs:
                correlations[f"{a} vs {b}"] = pearson(xs, ys)

    # How often does the overall direction simply equal one indicator's?
    dominance = {}
    for name in INDICATORS:
        agree = sum(1 for v, f in zip(votes[name], final_votes)
                    if v == f and f != 0)
        directional_finals = sum(1 for f in final_votes if f != 0)
        dominance[name] = (agree / directional_finals
                           if directional_finals else float("nan"))

    # Does each indicator point the way the generating drift did?
    drift_alignment = {}
    for name in INDICATORS:
        pairs = [(v, d) for v, d in zip(votes[name], drifts) if v != 0]
        if pairs:
            drift_alignment[name] = sum(
                1 for v, d in pairs if (v > 0) == (d > 0)) / len(pairs)
        else:
            drift_alignment[name] = float("nan")

    return {
        "samples": samples,
        "evaluated": evaluated,
        "direction_distribution": dict(directions),
        "strength_bands": dict(bands),
        "hold_rate": (directions.get("NEUTRAL", 0)
                      + directions.get("NO_SIGNAL", 0)) / evaluated
        if evaluated else 0.0,
        "cross_group_conflict_rate": cross_group_conflict / evaluated
        if evaluated else 0.0,
        "indicator_fire_rate": {n: fired[n] / evaluated if evaluated else 0.0
                                for n in INDICATORS},
        "indicator_neutral_rate": {n: neutral[n] / evaluated if evaluated else 0.0
                                   for n in INDICATORS},
        "indicator_absent_rate": {n: absent[n] / evaluated if evaluated else 0.0
                                  for n in INDICATORS},
        "indicator_mean_strength": {
            n: (sum(strengths[n]) / len(strengths[n])) if strengths[n]
            else float("nan") for n in INDICATORS},
        "indicator_strength_spread": {
            n: (max(strengths[n]) - min(strengths[n])) if len(strengths[n]) > 1
            else 0.0 for n in INDICATORS},
        "pairwise_vote_correlation": correlations,
        "dominance": dominance,
        "drift_alignment": drift_alignment,
        "group_direction_distribution": {k: dict(v) for k, v
                                         in group_directions.items()},
        "internal_conflict_rate": {k: internal_conflicts[k] / evaluated
                                   if evaluated else 0.0
                                   for k in group_directions},
        "mean_agreement": sum(agreements) / len(agreements) if agreements else 0.0,
        "mean_magnitude": sum(magnitudes) / len(magnitudes) if magnitudes else 0.0,
        "impossible_states": impossible,
    }


def report(stats: Dict) -> None:
    print(f"\n{'=' * 70}")
    print(f"SIGNAL ENGINE STATISTICAL CONTROLS  "
          f"({stats['evaluated']} of {stats['samples']} evaluated)")
    print("=" * 70)

    print("\n-- overall direction --")
    for key, count in sorted(stats["direction_distribution"].items(),
                             key=lambda kv: -kv[1]):
        print(f"   {key:12} {count:5}  {count / stats['evaluated']:6.1%}")
    print(f"   {'no call rate':12} {stats['hold_rate']:>11.1%}")
    print(f"   cross-group conflict {stats['cross_group_conflict_rate']:.1%}")
    print(f"   mean agreement {stats['mean_agreement']:.3f}   "
          f"mean magnitude {stats['mean_magnitude']:.3f}")

    print("\n-- per indicator --")
    print(f"   {'indicator':14} {'fires':>7} {'neutral':>8} {'absent':>7} "
          f"{'mean str':>9} {'spread':>7} {'drift align':>12}")
    for name in INDICATORS:
        print(f"   {name:14} {stats['indicator_fire_rate'][name]:>6.1%} "
              f"{stats['indicator_neutral_rate'][name]:>7.1%} "
              f"{stats['indicator_absent_rate'][name]:>6.1%} "
              f"{stats['indicator_mean_strength'][name]:>9.3f} "
              f"{stats['indicator_strength_spread'][name]:>7.3f} "
              f"{stats['drift_alignment'][name]:>11.1%}")

    print("\n-- pairwise vote correlation (|r| > 0.80 = likely duplicate) --")
    for pair, value in sorted(stats["pairwise_vote_correlation"].items(),
                              key=lambda kv: -abs(kv[1] if kv[1] == kv[1] else 0)):
        flag = ""
        if value == value and abs(value) > 0.80:
            flag = "  <== DUPLICATE?"
        elif value != value:
            flag = "  <== constant vote"
        print(f"   {pair:34} r = {value:+.3f}{flag}")

    print("\n-- dominance: overall direction equals this indicator's vote --")
    for name, value in sorted(stats["dominance"].items(), key=lambda kv: -kv[1]):
        flag = "  <== DOMINATES" if value > 0.95 else ""
        print(f"   {name:14} {value:6.1%}{flag}")

    print("\n-- groups --")
    for group, dist in stats["group_direction_distribution"].items():
        total = sum(dist.values())
        parts = "  ".join(f"{k}={v / total:.0%}" for k, v in sorted(dist.items()))
        print(f"   {group:16} {parts}")
        print(f"   {'':16} internal conflict "
              f"{stats['internal_conflict_rate'][group]:.1%}")

    print("\n-- invariants --")
    if stats["impossible_states"]:
        print(f"   {len(stats['impossible_states'])} IMPOSSIBLE STATES:")
        for line in stats["impossible_states"][:10]:
            print(f"      {line}")
    else:
        print("   no impossible states observed")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=900)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    stats = analyse(args.samples, args.seed)
    if args.json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        report(stats)
    return 1 if stats["impossible_states"] else 0


if __name__ == "__main__":
    sys.exit(main())
