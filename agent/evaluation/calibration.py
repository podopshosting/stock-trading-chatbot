"""
Is the hypothesis strength actually predictive?

A strength score is a claim: higher numbers should be followed by better
outcomes. That claim is testable, and until it is tested the score is
decoration.

This module tests it and — far more often — reports that the sample
cannot test it. That second job is the important one. With a few dozen
trades split into strength buckets, every bucket holds a handful of
trades and the differences between them are noise. A calibration curve
drawn from that looks like insight and is a Rorschach blot.

The specific trap being avoided: seeing that high-strength trades did
better than low-strength ones over thirty trades, concluding the score
works, and then raising position size on high-strength signals. The
observed difference between two 15-trade buckets is almost entirely
sampling variation.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..journal.metrics import (
    Adequacy, adequacy_for, expectancy_r, mean_interval, win_rate,
)
from ..journal.models import TradeOutcome, TradeRecord

CONFIG_VERSION = "calibration-v1.0.0"

# A bucket below this cannot be compared with another bucket. Chosen to
# match MIN_SAMPLE_PER_GROUP in the metrics module: the same reasoning
# applies, and two different thresholds for the same judgement would be
# a way to pick whichever answer was wanted.
MIN_BUCKET_SIZE = 15

# Buckets over hypothesis strength. Deliberately few: more buckets means
# fewer trades in each, and the temptation to read a trend from six
# points of two trades apiece.
DEFAULT_BUCKETS: Tuple[Tuple[float, float], ...] = (
    (0.0, 0.45),
    (0.45, 0.60),
    (0.60, 1.01),
)


class MonotonicityVerdict(str, enum.Enum):
    """Whether outcomes improve with strength."""
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_MONOTONIC = "NOT_MONOTONIC"
    MONOTONIC_BUT_NOT_SIGNIFICANT = "MONOTONIC_BUT_NOT_SIGNIFICANT"
    MONOTONIC_AND_SIGNIFICANT = "MONOTONIC_AND_SIGNIFICANT"

    def __str__(self) -> str:
        return self.value


@dataclass
class Bucket:
    """One strength band and what happened in it."""
    low: float
    high: float
    count: int
    expectancy_r: Optional[float] = None
    win_rate: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    comparable: bool = False

    @property
    def label(self) -> str:
        return f"{self.low:.2f}-{self.high:.2f}"

    def as_dict(self) -> Dict:
        return {
            "band": self.label,
            "low": self.low,
            "high": self.high,
            "count": self.count,
            "expectancy_r": (None if self.expectancy_r is None
                             else round(self.expectancy_r, 4)),
            "win_rate": (None if self.win_rate is None
                         else round(self.win_rate, 4)),
            "ci_low": None if self.ci_low is None else round(self.ci_low, 4),
            "ci_high": (None if self.ci_high is None
                        else round(self.ci_high, 4)),
            "comparable": self.comparable,
        }


def bucket_by_strength(trades: Sequence[TradeRecord],
                       buckets: Sequence[Tuple[float, float]] = DEFAULT_BUCKETS
                       ) -> List[Bucket]:
    """Group trades by the strength their hypothesis carried.

    Trades with no recorded strength are excluded rather than binned
    into the lowest bucket: an unknown strength is missing data, and
    placing it at zero would fabricate a reading the system never made.
    """
    out: List[Bucket] = []
    for low, high in buckets:
        members = [
            t for t in trades
            if t.hypothesis_strength is not None
            and low <= t.hypothesis_strength < high
            and t.outcome is not TradeOutcome.UNKNOWN
        ]
        values = [t.r_multiple for t in members if t.r_multiple is not None]
        mean, ci_low, ci_high = mean_interval(values)
        bucket = Bucket(
            low=low, high=high, count=len(members),
            expectancy_r=mean, ci_low=ci_low, ci_high=ci_high,
            comparable=len(members) >= MIN_BUCKET_SIZE,
        )
        if members:
            bucket.win_rate = win_rate(members).value
        out.append(bucket)
    return out


def intervals_overlap(a: Bucket, b: Bucket) -> Optional[bool]:
    """Do two buckets' confidence intervals overlap?

    None when either interval is unavailable. Overlapping intervals mean
    the difference between the buckets is consistent with chance, which
    is the usual answer at these sample sizes.
    """
    if None in (a.ci_low, a.ci_high, b.ci_low, b.ci_high):
        return None
    return not (a.ci_high < b.ci_low or b.ci_high < a.ci_low)


def assess(trades: Sequence[TradeRecord],
           buckets: Sequence[Tuple[float, float]] = DEFAULT_BUCKETS) -> Dict:
    """Is strength predictive? Usually: cannot tell yet.

    `verdict` is the field to read and it cannot be set to something
    more encouraging than the buckets support.
    """
    rows = bucket_by_strength(trades, buckets)
    scored = [b for b in rows if b.count > 0]
    comparable = [b for b in rows if b.comparable]

    with_strength = [t for t in trades if t.hypothesis_strength is not None]
    without_strength = [t for t in trades if t.hypothesis_strength is None]

    if len(comparable) < 2:
        verdict = MonotonicityVerdict.INSUFFICIENT_DATA
        detail = (
            f"{len(comparable)} of {len(rows)} strength bands have at least "
            f"{MIN_BUCKET_SIZE} trades. Comparing bands smaller than that "
            "measures sampling variation, not the score. This says nothing "
            "about whether hypothesis strength works - only that it has "
            "not been tested yet.")
        significant = False
        monotonic = None
    else:
        means = [b.expectancy_r for b in comparable]
        monotonic = all(
            earlier <= later
            for earlier, later in zip(means, means[1:])
            if earlier is not None and later is not None)
        # Significant only if the extreme bands' intervals separate.
        overlap = intervals_overlap(comparable[0], comparable[-1])
        significant = bool(monotonic and overlap is False)
        if not monotonic:
            verdict = MonotonicityVerdict.NOT_MONOTONIC
            detail = (
                "outcomes do not improve with strength across the "
                "comparable bands, so the score is not ordering trades "
                "the way it claims to")
        elif significant:
            verdict = MonotonicityVerdict.MONOTONIC_AND_SIGNIFICANT
            detail = (
                "outcomes improve with strength and the extreme bands' "
                "intervals do not overlap")
        else:
            verdict = MonotonicityVerdict.MONOTONIC_BUT_NOT_SIGNIFICANT
            detail = (
                "outcomes improve with strength, but the bands' confidence "
                "intervals overlap, so the ordering is consistent with "
                "chance")

    return {
        "config_version": CONFIG_VERSION,
        "verdict": str(verdict),
        "detail": detail,
        "is_evidence": significant,
        "monotonic": monotonic,
        "buckets": [b.as_dict() for b in rows],
        "bands_with_trades": len(scored),
        "bands_comparable": len(comparable),
        "min_bucket_size": MIN_BUCKET_SIZE,
        "trades_with_strength": len(with_strength),
        "trades_without_strength": len(without_strength),
        "excluded_note": (
            "trades with no recorded hypothesis strength are excluded "
            "rather than binned at zero, which would fabricate a reading "
            "the system never made"),
        "action": (
            "No parameter change is justified by this." if not significant
            else "The ordering is measurable; a change may be considered "
                 "and must then be tested out of sample."),
    }


def strength_vs_outcome_correlation(trades: Sequence[TradeRecord]) -> Dict:
    """Pearson correlation between strength and R.

    Reported with its sample size and WITHOUT a p-value, because a
    correlation from forty points has a confidence interval so wide that
    quoting a coefficient alone is misleading. The interval is what
    matters and it is included.
    """
    pairs = [(t.hypothesis_strength, t.r_multiple) for t in trades
             if t.hypothesis_strength is not None
             and t.r_multiple is not None]
    n = len(pairs)
    if n < 3:
        return {"n": n, "r": None, "ci_low": None, "ci_high": None,
                "adequacy": str(Adequacy.INSUFFICIENT),
                "note": f"{n} usable pairs; a correlation needs more"}

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return {"n": n, "r": None, "ci_low": None, "ci_high": None,
                "adequacy": str(adequacy_for(n)),
                "note": ("no variation in one of the series, so a "
                         "correlation is undefined - not zero")}

    r = cov / math.sqrt(var_x * var_y)

    # Fisher z interval. Wide at small n, which is the point.
    ci_low = ci_high = None
    if n > 3 and abs(r) < 1.0:
        z = 0.5 * math.log((1 + r) / (1 - r))
        se = 1.0 / math.sqrt(n - 3)
        lo, hi = z - 1.959964 * se, z + 1.959964 * se
        ci_low = math.tanh(lo)
        ci_high = math.tanh(hi)

    spans_zero = (ci_low is not None and ci_low <= 0.0 <= ci_high)
    return {
        "n": n,
        "r": round(r, 4),
        "ci_low": None if ci_low is None else round(ci_low, 4),
        "ci_high": None if ci_high is None else round(ci_high, 4),
        "adequacy": str(adequacy_for(n)),
        "spans_zero": spans_zero,
        "is_evidence": bool(
            adequacy_for(n) is Adequacy.DEMONSTRATED and not spans_zero),
        "note": ("the interval contains zero, so this is consistent with "
                 "strength having no relationship to outcome"
                 if spans_zero else ""),
    }
