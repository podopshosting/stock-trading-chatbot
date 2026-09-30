"""
Evidence & Catalyst Engine.

Answers what publicly available information may explain a security's
behaviour, how material and new it is, how trustworthy the source is,
and whether independent evidence corroborates it.

It never answers whether to trade. Evidence output is kept separate from
the quantitative signal engine, and the two are not combined into a
score anywhere in this package.
"""
from .classification import (
    FinancingStage, classify_earnings_outcome, classify_eight_k,
    classify_form, classify_form4_transaction, classify_headline,
)
from .dedup import (
    canonical_url, deduplicate, headline_tokens, independent_source_count,
    jaccard, novelty_for, same_story,
)
from .engine import (
    ACTIVE_CATALYST_THRESHOLD, DECAY_HALF_LIFE_HOURS, aggregate_direction,
    build_catalyst_result, classify_freshness, classify_window,
    current_relevance, decay_factor, detect_conflict, evidence_score,
)
from .llm import enrich_item, load_api_key, validate_enrichment
from .relevance import (
    apply_subject_relevance, mentions_symbol, name_tokens, subject_relevance,
)
from .service import (
    DEFAULT_ITEM_LIMIT, DEFAULT_LOOKBACK_HOURS, DEFAULT_TOP_N, EvidenceService,
)
from .store import (
    DynamoDBEvidenceStore, EvidenceStore, EvidenceStoreError,
    InMemoryEvidenceStore,
)
from .models import (
    DISCLAIMER, EVIDENCE_SCORE_MEANING, INTERPRETATION_CAVEAT,
    SOURCE_RELIABILITY, CatalystResult, CatalystSummary, CatalystWindow,
    Direction, EvidenceItem, EvidenceRun, EvidenceSource, EvidenceType,
    Fact, Freshness, LLMStatus, MaterialityBand, SourceClass,
)

__all__ = [
    "ACTIVE_CATALYST_THRESHOLD", "DECAY_HALF_LIFE_HOURS", "DISCLAIMER",
    "EVIDENCE_SCORE_MEANING", "INTERPRETATION_CAVEAT", "SOURCE_RELIABILITY",
    "CatalystResult", "CatalystSummary", "CatalystWindow", "Direction",
    "EvidenceItem", "EvidenceRun", "EvidenceSource", "EvidenceType", "Fact",
    "FinancingStage", "Freshness", "LLMStatus", "MaterialityBand",
    "SourceClass", "aggregate_direction", "build_catalyst_result",
    "canonical_url", "classify_earnings_outcome", "classify_eight_k",
    "classify_form", "classify_form4_transaction", "classify_freshness",
    "classify_headline", "classify_window", "current_relevance",
    "decay_factor", "deduplicate", "detect_conflict", "evidence_score",
    "headline_tokens", "independent_source_count", "jaccard", "novelty_for",
    "same_story", "enrich_item", "load_api_key", "validate_enrichment",
    "DEFAULT_ITEM_LIMIT", "DEFAULT_LOOKBACK_HOURS", "DEFAULT_TOP_N",
    "EvidenceService", "DynamoDBEvidenceStore", "EvidenceStore",
    "EvidenceStoreError", "InMemoryEvidenceStore",
    "apply_subject_relevance", "mentions_symbol", "name_tokens",
    "subject_relevance",
]
