"""
The trade journal and performance analytics.

The principle running through this package: a number is only evidence
if the sample behind it can support the claim being made. Every metric
carries its sample size and an explicit judgement of what may be said
about it, and `describe()` will not characterise an edge as demonstrated
until the confidence interval excludes the no-edge value.

This matters more here than it would in most systems. With two
concurrent positions and a small daily ceiling, the sample will remain
statistically inadequate for months. The natural failure is to look at
fifteen profitable trades, conclude the strategy works, and scale up.
"""
from .metrics import (
    CONFIG_VERSION, MIN_SAMPLE_FOR_CLAIM, MIN_SAMPLE_FOR_DIRECTION,
    MIN_SAMPLE_FOR_ESTIMATE, MIN_SAMPLE_PER_GROUP, Adequacy, Metric,
    adequacy_for, describe, expectancy_r, group_by, max_drawdown,
    mean_interval, profit_factor, stop_integrity, total_costs,
    total_net_pnl, wilson_interval, win_rate,
)
from .models import (
    SCRATCH_THRESHOLD_R, TradeCosts, TradeOutcome, TradeRecord,
)
from .recorder import RecorderError, costs_from_orders, record_closed_position
from .store import (
    DynamoDBJournal, InMemoryJournal, JournalError, JournalStore,
    TradeAlreadyRecorded, from_dict,
)

__all__ = [
    "CONFIG_VERSION", "MIN_SAMPLE_FOR_CLAIM", "MIN_SAMPLE_FOR_DIRECTION",
    "MIN_SAMPLE_FOR_ESTIMATE", "MIN_SAMPLE_PER_GROUP",
    "SCRATCH_THRESHOLD_R", "Adequacy", "DynamoDBJournal", "InMemoryJournal",
    "JournalError", "JournalStore", "Metric", "RecorderError",
    "TradeAlreadyRecorded", "TradeCosts", "TradeOutcome", "TradeRecord",
    "adequacy_for", "costs_from_orders", "describe", "expectancy_r",
    "from_dict", "group_by", "max_drawdown", "mean_interval",
    "profit_factor", "record_closed_position", "stop_integrity",
    "total_costs", "total_net_pnl", "wilson_interval", "win_rate",
]
