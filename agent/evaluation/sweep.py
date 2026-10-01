"""
Parameter sweeps, and why their results are usually worthless.

A sweep runs the same strategy over the same data with different
parameters and reports which did best. It is the most reliable way to
produce a number that looks like an improvement and is not one.

The reason is simple arithmetic. Try twenty parameter settings on one
dataset and the best of them will look good *even if every setting is
identical in truth*, because you are reporting the maximum of twenty
noisy measurements. The expected maximum of twenty draws from a
zero-mean distribution is comfortably positive. That apparent edge is
the selection, not the parameter.

So this module does three things:

1. Runs the sweep and reports every result, not just the winner.
2. States how large an apparent improvement the selection alone would
   be expected to produce, so the winner can be compared against it.
3. Refuses to recommend a change unless the winner survives out-of-
   sample data it was not selected on.

`recommendation` is derived and cannot be set to something more
encouraging than the evidence supports.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..journal.metrics import MIN_SAMPLE_FOR_CLAIM, Adequacy, adequacy_for

CONFIG_VERSION = "sweep-v1.0.0"

# Below this many trades per arm, a sweep result is not reportable as a
# comparison at all. Sweeps are the most overfitting-prone thing in the
# project, so the bar is higher than for a single metric.
MIN_TRADES_PER_ARM = 30

# Fraction of the data held back. The holdout is never used for
# selection, only for confirmation.
DEFAULT_HOLDOUT = 0.3


class Recommendation(str, enum.Enum):
    NO_CHANGE_INSUFFICIENT_DATA = "NO_CHANGE_INSUFFICIENT_DATA"
    NO_CHANGE_WITHIN_NOISE = "NO_CHANGE_WITHIN_NOISE"
    NO_CHANGE_FAILED_HOLDOUT = "NO_CHANGE_FAILED_HOLDOUT"
    CANDIDATE_FOR_CHANGE = "CANDIDATE_FOR_CHANGE"

    def __str__(self) -> str:
        return self.value

    @property
    def permits_change(self) -> bool:
        """Only one value does, and it says CANDIDATE, not APPROVED.

        A sweep cannot authorise a parameter change on its own; it can
        only produce something worth testing deliberately.
        """
        return self is Recommendation.CANDIDATE_FOR_CHANGE


@dataclass
class Arm:
    """One parameter setting and how it did."""
    label: str
    params: Dict
    trades: int
    expectancy_r: Optional[float]
    net_pnl: Optional[float] = None
    win_rate: Optional[float] = None
    holdout_expectancy_r: Optional[float] = None
    comparable: bool = False

    def as_dict(self) -> Dict:
        return {
            "label": self.label,
            "params": self.params,
            "trades": self.trades,
            "expectancy_r": (None if self.expectancy_r is None
                             else round(self.expectancy_r, 4)),
            "holdout_expectancy_r": (
                None if self.holdout_expectancy_r is None
                else round(self.holdout_expectancy_r, 4)),
            "net_pnl": (None if self.net_pnl is None
                        else round(self.net_pnl, 4)),
            "win_rate": (None if self.win_rate is None
                         else round(self.win_rate, 4)),
            "comparable": self.comparable,
        }


def expected_best_of_n(n_arms: int, spread: float) -> float:
    """How much an apparent edge the SELECTION alone would produce.

    Approximates the expected maximum of `n_arms` independent draws from
    a zero-mean normal with the given standard deviation. This is the
    number a sweep winner has to beat to mean anything: if the best arm
    beats the others by less than this, the ranking is explained by
    having looked at several arms.

    The approximation is the standard one for the expected value of the
    maximum of n standard normals, which is accurate enough for the
    purpose - deciding whether an observed gap is obviously within noise
    or obviously outside it.
    """
    if n_arms <= 1 or spread <= 0:
        return 0.0
    # E[max of n standard normals] ~ sqrt(2 ln n) - (ln ln n + ln 4pi) /
    # (2 sqrt(2 ln n)) for n >= 3; fall back to a cruder bound below.
    if n_arms < 3:
        return spread * 0.5642 * (n_arms - 1)
    root = math.sqrt(2.0 * math.log(n_arms))
    correction = (math.log(math.log(n_arms)) + math.log(4.0 * math.pi)) / (
        2.0 * root)
    return spread * max(0.0, root - correction)


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def split_holdout(items: Sequence, holdout: float = DEFAULT_HOLDOUT
                  ) -> Tuple[List, List]:
    """Chronological split: earlier data selects, later data confirms.

    Deliberately NOT random. A random split lets the selection see data
    interleaved with the confirmation period, and market conditions
    cluster in time - so a randomly split holdout is contaminated by the
    same regime the selection was fitted to.
    """
    if not 0.0 < holdout < 1.0:
        raise ValueError("holdout must be between 0 and 1")
    cut = int(len(items) * (1.0 - holdout))
    return list(items[:cut]), list(items[cut:])


def run(arms: Sequence[Dict],
        evaluate: Callable[[Dict, Sequence], Dict],
        selection_data: Sequence,
        holdout_data: Optional[Sequence] = None) -> Dict:
    """Evaluate every arm, then judge whether anything was learned.

    `evaluate(params, data)` returns a dict with at least
    `expectancy_r` and `trades`. The caller owns the strategy; this
    module owns the scepticism.
    """
    if not arms:
        raise ValueError("a sweep needs at least one arm")

    results: List[Arm] = []
    for index, params in enumerate(arms):
        outcome = evaluate(params, selection_data) or {}
        trades = int(outcome.get("trades") or 0)
        results.append(Arm(
            label=outcome.get("label") or f"arm_{index}",
            params=dict(params),
            trades=trades,
            expectancy_r=outcome.get("expectancy_r"),
            net_pnl=outcome.get("net_pnl"),
            win_rate=outcome.get("win_rate"),
            comparable=trades >= MIN_TRADES_PER_ARM,
        ))

    comparable = [a for a in results if a.comparable
                  and a.expectancy_r is not None]
    ranked = sorted(comparable, key=lambda a: a.expectancy_r, reverse=True)

    noise_floor = None
    gap = None
    recommendation = Recommendation.NO_CHANGE_INSUFFICIENT_DATA
    detail = (
        f"{len(comparable)} of {len(results)} arms reached "
        f"{MIN_TRADES_PER_ARM} trades. A sweep over arms smaller than that "
        "ranks sampling variation. Nothing has been learned about the "
        "parameters.")

    if len(ranked) >= 2:
        values = [a.expectancy_r for a in ranked]
        spread = _stdev(values)
        noise_floor = expected_best_of_n(len(ranked), spread)
        gap = ranked[0].expectancy_r - ranked[1].expectancy_r

        if gap <= noise_floor:
            recommendation = Recommendation.NO_CHANGE_WITHIN_NOISE
            detail = (
                f"the best arm beats the second by {gap:.4f}R, but simply "
                f"picking the best of {len(ranked)} arms would be expected "
                f"to produce a gap of about {noise_floor:.4f}R even if "
                "every arm were identical. The ranking is explained by "
                "having looked at several arms.")
        elif holdout_data is None:
            recommendation = Recommendation.NO_CHANGE_FAILED_HOLDOUT
            detail = (
                f"the best arm beats the second by {gap:.4f}R against a "
                f"selection-noise floor of {noise_floor:.4f}R, but no "
                "holdout was supplied. A result confirmed only on the data "
                "that selected it is not a result.")
        else:
            # Confirm the winner on data it was not selected on.
            for arm in ranked[:1]:
                confirm = evaluate(arm.params, holdout_data) or {}
                arm.holdout_expectancy_r = confirm.get("expectancy_r")
                holdout_trades = int(confirm.get("trades") or 0)

            winner = ranked[0]
            if (winner.holdout_expectancy_r is None
                    or holdout_trades < MIN_TRADES_PER_ARM):
                recommendation = Recommendation.NO_CHANGE_FAILED_HOLDOUT
                detail = (
                    f"the holdout produced {holdout_trades} trades, below "
                    f"{MIN_TRADES_PER_ARM}; it cannot confirm anything.")
            elif winner.holdout_expectancy_r <= 0:
                recommendation = Recommendation.NO_CHANGE_FAILED_HOLDOUT
                detail = (
                    "the best arm did not hold up out of sample "
                    f"({winner.holdout_expectancy_r:.4f}R), which is the "
                    "signature of a parameter fitted to the selection "
                    "period rather than a real effect.")
            else:
                recommendation = Recommendation.CANDIDATE_FOR_CHANGE
                detail = (
                    f"the best arm beats the second by {gap:.4f}R against a "
                    f"{noise_floor:.4f}R selection-noise floor and held up "
                    f"out of sample at {winner.holdout_expectancy_r:.4f}R. "
                    "This is a candidate worth testing deliberately, not an "
                    "approved change.")

    return {
        "config_version": CONFIG_VERSION,
        "recommendation": str(recommendation),
        "permits_change": recommendation.permits_change,
        "detail": detail,
        "arms_total": len(results),
        "arms_comparable": len(comparable),
        "min_trades_per_arm": MIN_TRADES_PER_ARM,
        "selection_noise_floor_r": (None if noise_floor is None
                                    else round(noise_floor, 4)),
        "winner_gap_r": None if gap is None else round(gap, 4),
        "arms": [a.as_dict() for a in results],
        "winner": ranked[0].as_dict() if ranked else None,
        "note": (
            "Every arm is reported, not only the winner. Reporting the "
            "maximum of several noisy measurements as an improvement is "
            "the most reliable way to manufacture an edge that is not "
            "there."),
    }
