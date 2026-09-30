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
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

REPO = pathlib.Path(__file__).resolve().parent.parent

ENGINE = REPO / "agent" / "signals" / "engine.py"
INDICATORS = REPO / "agent" / "signals" / "indicators.py"

SUITES = ["tests.test_signal_engine", "tests.test_signal_statistics",
          "tests.test_signal_equivalence"]


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
    Mutation(
        name="agreement-includes-magnitude",
        description="multiply agreement by magnitude, recombining the two "
                    "concepts this milestone separated",
        path=ENGINE,
        old="    agreement = share * internal",
        new="    agreement = share * internal * magnitude  # MUTATION",
        expect=["structural", "agreement"],
    ),
]


def checksum(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_suites() -> Dict:
    """Run the signal suites, returning pass/fail and the failing names."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", *SUITES],
        cwd=REPO, capture_output=True, text=True, timeout=600,
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

            mutation.path.write_text(source.replace(mutation.old,
                                                    mutation.new, 1))
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
