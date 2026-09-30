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

SUITES = ["tests.test_signal_engine", "tests.test_signal_statistics",
          "tests.test_signal_equivalence", "tests.test_evidence",
          "tests.test_evidence_service"]


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
