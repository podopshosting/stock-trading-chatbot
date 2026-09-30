"""
Performance analytics, with the sample size attached to every claim.

The uncomfortable arithmetic this module exists to enforce: with two
concurrent positions and a $50 daily ceiling, this system will take a
handful of trades a day at most. After a month that is perhaps forty
trades. Forty trades cannot distinguish a real edge from luck.

A 60% win rate over 20 trades has a 95% confidence interval of roughly
36% to 81%. That interval contains 50%. It is not evidence of anything.
Reporting "60% win rate" without it invites exactly the wrong decision -
scaling up a strategy that has demonstrated nothing.

So every metric here is returned alongside an explicit adequacy
judgement, and `describe()` refuses to characterise an edge until the
sample can support it. The numbers are always shown; what changes is
whether they are allowed to be called evidence.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import TradeOutcome, TradeRecord

CONFIG_VERSION = "metrics-v1.0.0"

# Sample thresholds. These are deliberately conservative and are about
# what a number is ALLOWED TO BE CALLED, not whether it is computed.
MIN_SAMPLE_FOR_DIRECTION = 20    # may say "so far positive/negative"
MIN_SAMPLE_FOR_ESTIMATE = 30     # may quote an estimate with an interval
MIN_SAMPLE_FOR_CLAIM = 100       # may describe an edge as demonstrated

# Below this many trades in a subgroup, no per-group comparison is
# reported at all. Slicing forty trades by strategy and time of day
# produces cells of two or three, and a "best performing strategy"
# chosen from those is noise with a label on it.
MIN_SAMPLE_PER_GROUP = 15

Z_95 = 1.959964


class Adequacy(str, enum.Enum):
    """What the sample size permits you to say."""
    INSUFFICIENT = "INSUFFICIENT"      # report the number, claim nothing
    DIRECTIONAL = "DIRECTIONAL"        # may state a sign, not a magnitude
    ESTIMATE = "ESTIMATE"              # may quote a value with an interval
    DEMONSTRATED = "DEMONSTRATED"      # interval excludes the null

    def __str__(self) -> str:
        return self.value


def adequacy_for(n: int) -> Adequacy:
    if n >= MIN_SAMPLE_FOR_CLAIM:
        return Adequacy.DEMONSTRATED
    if n >= MIN_SAMPLE_FOR_ESTIMATE:
        return Adequacy.ESTIMATE
    if n >= MIN_SAMPLE_FOR_DIRECTION:
        return Adequacy.DIRECTIONAL
    return Adequacy.INSUFFICIENT


@dataclass
class Metric:
    """A number, its uncertainty, and what may be said about it."""
    name: str
    value: Optional[float]
    sample_size: int
    adequacy: Adequacy
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    null_value: Optional[float] = None
    note: str = ""

    @property
    def excludes_null(self) -> Optional[bool]:
        """Does the interval exclude the no-edge value?

        Derived, so it cannot be asserted independently of the interval
        it is supposed to describe.
        """
        if (self.ci_low is None or self.ci_high is None
                or self.null_value is None):
            return None
        return self.ci_low > self.null_value or self.ci_high < self.null_value

    @property
    def is_evidence(self) -> bool:
        """The only property that should gate a scaling decision."""
        return (self.adequacy is Adequacy.DEMONSTRATED
                and self.excludes_null is True)

    def as_dict(self) -> Dict:
        return {
            "name": self.name,
            "value": None if self.value is None else round(self.value, 4),
            "sample_size": self.sample_size,
            "adequacy": str(self.adequacy),
            "ci_low": None if self.ci_low is None else round(self.ci_low, 4),
            "ci_high": None if self.ci_high is None else round(self.ci_high, 4),
            "null_value": self.null_value,
            "excludes_null": self.excludes_null,
            "is_evidence": self.is_evidence,
            "note": self.note,
        }


def wilson_interval(successes: int, n: int,
                    z: float = Z_95) -> Tuple[Optional[float],
                                              Optional[float]]:
    """Wilson score interval for a proportion.

    Used rather than the normal approximation because the normal one is
    badly wrong at the sample sizes this system will actually have, and
    wrong in the direction of looking more certain than it is.
    """
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def mean_interval(values: Sequence[float],
                  z: float = Z_95) -> Tuple[Optional[float], Optional[float],
                                            Optional[float]]:
    """Mean and its confidence interval. Returns (mean, low, high)."""
    n = len(values)
    if n == 0:
        return None, None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None, None
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    stderr = math.sqrt(variance / n)
    return mean, mean - z * stderr, mean + z * stderr


def _counted(trades: Iterable[TradeRecord]) -> List[TradeRecord]:
    """Trades that can be SCORED.

    UNKNOWN outcomes are excluded from statistical denominators rather
    than treated as scratches, because a trade whose result could not be
    determined is missing data, not a neutral result.

    This is deliberately NOT used for the money totals. A trade with no
    measurable R still moved real money, and a reported P&L that omits
    it would disagree with the account balance - which is a worse
    failure than an incomplete statistic. "Cannot compute R" and "did
    not happen" are different things.
    """
    return [t for t in trades if t.outcome is not TradeOutcome.UNKNOWN]


def _all_with_money(trades: Iterable[TradeRecord]) -> List[TradeRecord]:
    """Every trade that moved cash, scoreable or not.

    Used for the account-fact metrics: total P&L, drawdown, costs.
    """
    return list(trades)


def _decisive(trades: Iterable[TradeRecord]) -> List[TradeRecord]:
    """Wins and losses only.

    Scratches are excluded from the win rate denominator. Including
    them would let a strategy that mostly goes nowhere report a
    flattering win rate by diluting its losses.
    """
    return [t for t in _counted(trades)
            if t.outcome in (TradeOutcome.WIN, TradeOutcome.LOSS)]


def win_rate(trades: Sequence[TradeRecord]) -> Metric:
    decisive = _decisive(trades)
    n = len(decisive)
    wins = sum(1 for t in decisive if t.outcome is TradeOutcome.WIN)
    low, high = wilson_interval(wins, n)
    adequacy = adequacy_for(n)
    note = ""
    if adequacy is Adequacy.INSUFFICIENT:
        note = (f"{n} decisive trades cannot distinguish skill from luck; "
                "the number is shown for completeness only")
    elif low is not None and low <= 0.5 <= high:
        note = ("the interval contains 50%, so this is consistent with "
                "having no edge at all")
    return Metric(name="win_rate",
                  value=(wins / n) if n else None,
                  sample_size=n, adequacy=adequacy,
                  ci_low=low, ci_high=high, null_value=0.5, note=note)


def expectancy_r(trades: Sequence[TradeRecord]) -> Metric:
    """Average R per trade - the metric that actually matters.

    A 40% win rate is excellent if the winners are 3R and the losers
    1R. Win rate alone says nothing without this.
    """
    counted = _counted(trades)
    values = [t.r_multiple for t in counted if t.r_multiple is not None]
    mean, low, high = mean_interval(values)
    n = len(values)
    adequacy = adequacy_for(n)
    note = ""
    if adequacy is Adequacy.INSUFFICIENT:
        note = (f"{n} trades is too few for an expectancy; the variance of "
                "R over a handful of trades is larger than any plausible "
                "edge")
    elif low is not None and low <= 0.0 <= high:
        note = "the interval contains zero, so no edge is demonstrated"
    return Metric(name="expectancy_r", value=mean, sample_size=n,
                  adequacy=adequacy, ci_low=low, ci_high=high,
                  null_value=0.0, note=note)


def profit_factor(trades: Sequence[TradeRecord]) -> Metric:
    """Gross wins divided by gross losses.

    None when there are no losses yet. A profit factor of infinity is
    not a good sign, it is a sign of too small a sample.
    """
    counted = _counted(trades)
    wins = sum(t.net_pnl for t in counted if t.net_pnl > 0)
    losses = -sum(t.net_pnl for t in counted if t.net_pnl < 0)
    n = len(counted)
    value = None if losses <= 0 else wins / losses
    note = ""
    if losses <= 0 and n:
        note = ("no losing trades yet, so a profit factor cannot be "
                "computed; this reflects sample size, not quality")
    return Metric(name="profit_factor", value=value, sample_size=n,
                  adequacy=adequacy_for(n), null_value=1.0, note=note)


def max_drawdown(trades: Sequence[TradeRecord]) -> Metric:
    """Largest peak-to-trough fall in cumulative net P&L.

    Computed in close order, which is the order the equity curve was
    actually experienced.
    """
    # The equity curve is an account fact: a trade with no measurable
    # R still moved the balance, and omitting it would understate the
    # drawdown actually experienced.
    counted = sorted(_all_with_money(trades), key=lambda t: t.closed_at)
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for trade in counted:
        equity += trade.net_pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return Metric(name="max_drawdown", value=worst,
                  sample_size=len(counted),
                  adequacy=adequacy_for(len(counted)),
                  note=("the worst drawdown in a small sample is almost "
                        "always smaller than the worst still to come"))


def total_net_pnl(trades: Sequence[TradeRecord]) -> float:
    """Every trade that moved money, so this reconciles with the account."""
    return sum(t.net_pnl for t in _all_with_money(trades))


def total_costs(trades: Sequence[TradeRecord]) -> Dict:
    """What the trades paid to exist, and how that compares to the result.

    The ratio is the number that reveals a strategy whose gross edge is
    real but entirely consumed by execution.
    """
    counted = _all_with_money(trades)
    costs = sum(t.costs.total for t in counted)
    gross = sum(t.gross_pnl for t in counted)
    return {
        "total_costs": round(costs, 4),
        "gross_pnl": round(gross, 4),
        "net_pnl": round(total_net_pnl(counted), 4),
        "costs_as_pct_of_gross_wins": (
            None if gross <= 0 else round(costs / gross * 100.0, 2)),
        "note": ("costs are already inside the fill prices; this is a "
                 "breakdown of gross_pnl, not a further deduction"),
    }


def stop_integrity(trades: Sequence[TradeRecord]) -> Dict:
    """Whether the stops did what they claimed.

    The single most important diagnostic in the whole module. If losses
    routinely exceed 1R, the stops are not holding, every risk
    calculation upstream is wrong, and the position sizing is
    systematically too large.
    """
    counted = [t for t in _counted(trades)
               if t.exceeded_planned_risk is not None]
    n = len(counted)
    breaches = [t for t in counted if t.exceeded_planned_risk]
    worst = min((t.r_multiple for t in counted
                 if t.r_multiple is not None), default=None)
    return {
        "trades_assessed": n,
        "stop_breaches": len(breaches),
        "breach_rate": (None if n == 0 else round(len(breaches) / n, 4)),
        "worst_r": None if worst is None else round(worst, 4),
        "breached_symbols": sorted({t.symbol for t in breaches}),
        "note": ("a loss worse than -1R means the stop did not hold; if "
                 "this is not rare, the risk model is wrong and position "
                 "sizes are too large"),
    }


def group_by(trades: Sequence[TradeRecord], key: str) -> Dict[str, Dict]:
    """Break results down by strategy, exit reason, symbol and so on.

    Groups below MIN_SAMPLE_PER_GROUP are reported but explicitly
    marked, because picking the "best" cell out of a set of three-trade
    groups is choosing noise and calling it insight.
    """
    buckets: Dict[str, List[TradeRecord]] = {}
    for trade in _counted(trades):
        value = str(getattr(trade, key, "") or "UNSPECIFIED")
        buckets.setdefault(value, []).append(trade)

    out = {}
    for value, group in sorted(buckets.items()):
        n = len(group)
        expectancy = expectancy_r(group)
        out[value] = {
            "count": n,
            "net_pnl": round(total_net_pnl(group), 4),
            "expectancy_r": expectancy.value,
            "win_rate": win_rate(group).value,
            "comparable": n >= MIN_SAMPLE_PER_GROUP,
            "note": ("" if n >= MIN_SAMPLE_PER_GROUP else
                     f"only {n} trades; not comparable with other groups"),
        }
    return out


def describe(trades: Sequence[TradeRecord]) -> Dict:
    """A full report that states plainly what it does and does not show.

    `verdict` is the field to read. It is derived from the metrics and
    cannot be set to something more encouraging than the sample allows.
    """
    counted = _counted(trades)
    n = len(counted)
    wr = win_rate(trades)
    exp = expectancy_r(trades)
    pf = profit_factor(trades)
    dd = max_drawdown(trades)

    if n == 0:
        verdict = "NO_TRADES"
        summary = "No completed trades yet."
    elif not exp.is_evidence and not wr.is_evidence:
        verdict = "NO_EDGE_DEMONSTRATED"
        summary = (
            f"{n} completed trade(s). This is not enough to establish "
            "whether the strategy has an edge, whichever way the numbers "
            "happen to point. At this sample size the difference between "
            "a good strategy and a lucky one is not measurable, so these "
            "figures must not be used to justify increasing size.")
    elif exp.value is not None and exp.value > 0:
        verdict = "POSITIVE_EDGE_DEMONSTRATED"
        summary = (
            f"{n} completed trades with a positive expectancy whose "
            "confidence interval excludes zero.")
    else:
        verdict = "NEGATIVE_EDGE_DEMONSTRATED"
        summary = (
            f"{n} completed trades with a negative expectancy whose "
            "confidence interval excludes zero. The strategy is losing "
            "money in a way that is unlikely to be chance.")

    unknown = [t for t in trades if t.outcome is TradeOutcome.UNKNOWN]

    return {
        "config_version": CONFIG_VERSION,
        "verdict": verdict,
        "summary": summary,
        "trades_counted": n,
        "trades_excluded_unknown": len(unknown),
        "sample_thresholds": {
            "directional": MIN_SAMPLE_FOR_DIRECTION,
            "estimate": MIN_SAMPLE_FOR_ESTIMATE,
            "claim": MIN_SAMPLE_FOR_CLAIM,
            "per_group": MIN_SAMPLE_PER_GROUP,
        },
        "metrics": {m.name: m.as_dict() for m in (wr, exp, pf, dd)},
        "total_net_pnl": round(total_net_pnl(trades), 4),
        "costs": total_costs(trades),
        "stop_integrity": stop_integrity(trades),
        "by_strategy": group_by(trades, "strategy"),
        "by_exit_reason": group_by(trades, "exit_reason"),
        "by_symbol": group_by(trades, "symbol"),
        "paper_only": all(t.is_paper for t in trades) if trades else True,
    }
