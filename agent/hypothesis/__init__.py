"""
Trade Hypothesis Engine.

Combines scanner context, quantitative signals, published evidence and
the market regime into a structured, inspectable argument. Produces no
order, no quantity and no approval.
"""
from .engine import (
    CONFIG_VERSION, MIN_AGREEMENT, MIN_CATALYST_MATERIALITY,
    MIN_CATALYST_NOVELTY, MIN_MAGNITUDE, choose_strategy, compute_strength,
    find_contradictions, generate, suggest_stop_distance,
)
from .models import (
    DISCLAIMER, STRENGTH_MEANING, Contradiction, ContradictionSeverity,
    EvidenceView, HypothesisDirection, HypothesisRun, HypothesisStatus,
    MarketView, QuantitativeView, Strategy, TradeHypothesis,
)
from .reasons import build_reasons
from .service import HypothesisService
from .store import (
    DynamoDBHypothesisStore, HypothesisStore, InMemoryHypothesisStore,
)

__all__ = [
    "CONFIG_VERSION", "DISCLAIMER", "MIN_AGREEMENT",
    "MIN_CATALYST_MATERIALITY", "MIN_CATALYST_NOVELTY", "MIN_MAGNITUDE",
    "STRENGTH_MEANING", "Contradiction", "ContradictionSeverity",
    "EvidenceView", "HypothesisDirection", "HypothesisRun",
    "HypothesisStatus", "MarketView", "QuantitativeView", "Strategy",
    "TradeHypothesis", "build_reasons", "choose_strategy", "compute_strength",
    "find_contradictions", "generate", "suggest_stop_distance",
    "HypothesisService", "DynamoDBHypothesisStore", "HypothesisStore",
    "InMemoryHypothesisStore",
]
