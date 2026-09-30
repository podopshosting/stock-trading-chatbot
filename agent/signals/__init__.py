"""
Canonical quantitative signal engine.

One home for the question "what direction do the independent market
signals support, how strongly, and where do they disagree?" - so the
chatbot, the review UX and the future trading agent cannot drift into
three different answers.

It produces evidence, never instructions. There is no entry, target,
stop, size or expected return anywhere in this package.
"""
from .engine import (
    MIN_HISTORY, aggregate, evaluate, evaluate_indicators, group_signals,
    regime_adjustment,
)
from .models import (
    AGREEMENT_MEANING, ALL_GROUPS, DISCLAIMER, INDICATOR_GROUPS,
    MAGNITUDE_MEANING, CorrelationGroup, DataFreshness, GroupDirection,
    QuantitativeSignalResult, SignalDirection, SignalGroupResult,
    SignalResult, SignalRun, StrengthBand,
)
from .reasons import build_reasons, indicator_reasons
from .service import DEFAULT_TOP_N, HISTORY_BARS, SignalService
from .store import (
    DynamoDBSignalStore, InMemorySignalStore, SignalStore, SignalStoreError,
)

__all__ = [
    "AGREEMENT_MEANING", "ALL_GROUPS", "DISCLAIMER", "INDICATOR_GROUPS",
    "MAGNITUDE_MEANING", "MIN_HISTORY", "CorrelationGroup", "DataFreshness",
    "GroupDirection", "QuantitativeSignalResult", "SignalDirection",
    "SignalGroupResult", "SignalResult", "SignalRun", "StrengthBand",
    "aggregate", "build_reasons", "evaluate", "evaluate_indicators",
    "group_signals", "indicator_reasons", "regime_adjustment",
    "DEFAULT_TOP_N", "HISTORY_BARS", "SignalService",
    "DynamoDBSignalStore", "InMemorySignalStore", "SignalStore",
    "SignalStoreError",
]
