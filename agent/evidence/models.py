"""
Canonical evidence and catalyst types.

The question this layer answers:

    What publicly available information may explain why this security is
    moving, how material is it, how new is it, how trustworthy is the
    source, and does independent evidence corroborate it?

It does not answer whether to trade. There is no direction-to-order
mapping anywhere in this package.

Four concepts are kept strictly apart, because collapsing any pair of
them is how a headline becomes a trade:

  quantitative signal   price/volume behaviour                (agent/signals)
  evidence              externally published information      (here)
  catalyst              a material event that may plausibly
                        affect market behaviour               (here)
  sentiment/direction   the tone of an event or source        (here)

None of them individually equals BUY or SELL.

Three further separations run through the model and must not be merged:

  materiality   how much the event could matter
  direction     which way it points, if anywhere
  novelty       whether it is actually new

"CEO resigns unexpectedly" is HIGH materiality with UNCERTAIN direction.
That is a valid, complete reading, and a model that forced it positive or
negative would be inventing information.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


# --- source classification ----------------------------------------------

class SourceClass(str, enum.Enum):
    """Tiered by provenance, not by popularity.

    PRIMARY is "the issuer or a government body said this, in a document
    they are accountable for". It is a claim about *provenance*, not
    about interpretation: a company press release reliably states what
    the company announced while still presenting it favourably. See
    `interpretation_caveat` on SourceReliability.
    """
    PRIMARY = "PRIMARY"                  # Tier A: SEC, issuer PR, Fed, BLS, FDA
    STRUCTURED_NEWS = "STRUCTURED_NEWS"  # Tier B: licensed news feed
    AGGREGATED = "AGGREGATED"            # Tier C: sentiment/summary aggregation
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


# Factual-provenance reliability by tier. This is NOT a measure of how
# well the source predicts price.
SOURCE_RELIABILITY = {
    SourceClass.PRIMARY: 1.00,
    SourceClass.STRUCTURED_NEWS: 0.75,
    SourceClass.AGGREGATED: 0.50,
    SourceClass.UNKNOWN: 0.25,
}

INTERPRETATION_CAVEAT = (
    "Source reliability describes how confidently we can say the source "
    "ACTUALLY STATED this, not whether the interpretation is correct. A "
    "company press release is a reliable record of what the company "
    "announced and is also written to present it favourably."
)


# --- event taxonomy ------------------------------------------------------

class EvidenceType(str, enum.Enum):
    # corporate
    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    REVENUE_UPDATE = "REVENUE_UPDATE"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    CUSTOMER_WIN = "CUSTOMER_WIN"
    CONTRACT = "CONTRACT"
    PARTNERSHIP = "PARTNERSHIP"
    ACQUISITION = "ACQUISITION"
    MERGER = "MERGER"
    DIVESTITURE = "DIVESTITURE"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    LAYOFF = "LAYOFF"
    RESTRUCTURING = "RESTRUCTURING"
    DIVIDEND = "DIVIDEND"
    BUYBACK = "BUYBACK"
    STOCK_SPLIT = "STOCK_SPLIT"

    # capital structure
    SHARE_OFFERING = "SHARE_OFFERING"
    SHELF_REGISTRATION = "SHELF_REGISTRATION"
    ATM_OFFERING = "ATM_OFFERING"
    DEBT_OFFERING = "DEBT_OFFERING"
    CONVERTIBLE_DEBT = "CONVERTIBLE_DEBT"
    DILUTION = "DILUTION"
    BANKRUPTCY = "BANKRUPTCY"

    # regulatory / legal
    FDA = "FDA"
    DOJ = "DOJ"
    FTC = "FTC"
    SEC_ENFORCEMENT = "SEC_ENFORCEMENT"
    LAWSUIT = "LAWSUIT"
    SETTLEMENT = "SETTLEMENT"
    REGULATORY_APPROVAL = "REGULATORY_APPROVAL"
    REGULATORY_REJECTION = "REGULATORY_REJECTION"

    # analyst / market
    ANALYST_UPGRADE = "ANALYST_UPGRADE"
    ANALYST_DOWNGRADE = "ANALYST_DOWNGRADE"
    PRICE_TARGET_CHANGE = "PRICE_TARGET_CHANGE"
    ESTIMATE_REVISION = "ESTIMATE_REVISION"

    # ownership
    INSIDER_BUY = "INSIDER_BUY"
    INSIDER_SELL = "INSIDER_SELL"
    INSTITUTIONAL_CHANGE = "INSTITUTIONAL_CHANGE"
    ACTIVIST_POSITION = "ACTIVIST_POSITION"

    # macro
    FOMC = "FOMC"
    CPI = "CPI"
    PPI = "PPI"
    PAYROLLS = "PAYROLLS"
    UNEMPLOYMENT = "UNEMPLOYMENT"
    GDP = "GDP"
    RETAIL_SALES = "RETAIL_SALES"

    # filings that are informative but not in themselves an event
    PERIODIC_REPORT = "PERIODIC_REPORT"      # 10-K / 10-Q
    MATERIAL_AGREEMENT = "MATERIAL_AGREEMENT"
    UPCOMING_EARNINGS = "UPCOMING_EARNINGS"

    # Present, but every OTHER still carries metrics and a reason. An
    # unknown event must never become a silent catch-all with no scores.
    OTHER = "OTHER"

    def __str__(self) -> str:
        return self.value


class Direction(str, enum.Enum):
    """Tone, not a trade.

    MIXED and UNCERTAIN are load-bearing. "Earnings beat, guidance cut"
    is MIXED and must survive aggregation as MIXED; a net sentiment score
    would hide the contradiction behind a single sign.
    """
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"           # genuinely both ways
    NEUTRAL = "NEUTRAL"       # assessed, no directional tone
    UNCERTAIN = "UNCERTAIN"   # material, but direction genuinely unknown

    def __str__(self) -> str:
        return self.value


class MaterialityBand(str, enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"

    def __str__(self) -> str:
        return self.value


class CatalystWindow(str, enum.Enum):
    """How current an item is, in explicit named bands.

    A month-old story is not "breaking". Boundaries are in
    `WINDOW_BOUNDS_HOURS`.
    """
    BREAKING = "BREAKING"        # <= 1 hour
    INTRADAY = "INTRADAY"        # <= 8 hours
    RECENT = "RECENT"            # <= 72 hours
    BACKGROUND = "BACKGROUND"    # <= 30 days
    STALE = "STALE"              # older
    UNKNOWN = "UNKNOWN"          # no usable timestamp

    def __str__(self) -> str:
        return self.value


WINDOW_BOUNDS_HOURS = {
    CatalystWindow.BREAKING: 1.0,
    CatalystWindow.INTRADAY: 8.0,
    CatalystWindow.RECENT: 72.0,
    CatalystWindow.BACKGROUND: 24.0 * 30,
}


class Freshness(str, enum.Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


class LLMStatus(str, enum.Enum):
    """Whether the optional enrichment ran.

    UNAVAILABLE is a first-class outcome. When the model cannot be
    reached the evidence is still stored with its deterministic
    classification; nothing is invented to fill the gap.
    """
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    SUCCEEDED = "SUCCEEDED"
    UNAVAILABLE = "UNAVAILABLE"
    REJECTED = "REJECTED"        # returned output that failed validation

    def __str__(self) -> str:
        return self.value


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


# --- source --------------------------------------------------------------

@dataclass
class EvidenceSource:
    """Where an item came from. Never optional, never inferred.

    Provenance is the thing that makes evidence auditable. An item
    without a retrievable source is not evidence, it is an assertion.
    """
    provider: str                      # "sec", "alpaca_news", "alpha_vantage"
    publisher: str = ""                # "SEC", "Benzinga", issuer name
    source_class: SourceClass = SourceClass.UNKNOWN
    url: str = ""
    document_id: str = ""              # accession number, article id

    @property
    def reliability(self) -> float:
        return SOURCE_RELIABILITY.get(self.source_class, 0.25)

    @property
    def is_primary(self) -> bool:
        return self.source_class is SourceClass.PRIMARY

    def as_dict(self) -> Dict:
        return {
            "provider": self.provider,
            "publisher": self.publisher,
            "source_class": str(self.source_class),
            "url": self.url,
            "document_id": self.document_id,
            "reliability": self.reliability,
            "is_primary": self.is_primary,
        }


@dataclass
class Fact:
    """One atomic extracted claim, tied back to its source.

    The linkage is the point. A summary that cannot be traced to a
    document is an unfalsifiable paraphrase, so `source_evidence_id` is
    required and a Fact without one is refused at construction.
    """
    claim: str
    source_evidence_id: str
    value: Optional[str] = None
    previous_value: Optional[str] = None
    source_quote_location: str = ""
    extracted_by: str = "deterministic"   # or the model name

    def __post_init__(self):
        if not self.source_evidence_id:
            raise ValueError(
                "a Fact must reference the evidence it came from; an "
                "untraceable claim is not evidence"
            )

    def as_dict(self) -> Dict:
        return asdict(self)


# --- the canonical item --------------------------------------------------

@dataclass
class EvidenceItem:
    """One normalised piece of externally published information.

    Provider-specific payloads stay behind adapters; downstream logic
    must never know which API produced this.
    """
    evidence_id: str
    symbol: str
    source: EvidenceSource
    evidence_type: EvidenceType = EvidenceType.OTHER

    published_at: Optional[str] = None
    retrieved_at: str = field(default_factory=utcnow)

    headline: str = ""
    summary: str = ""                  # only what licensing permits

    direction: Direction = Direction.NEUTRAL
    materiality: float = 0.0           # [0, 1]
    novelty: float = 1.0               # [0, 1]; 1.0 until proven repeated

    catalyst_category: Optional[EvidenceType] = None
    symbols: List[str] = field(default_factory=list)
    freshness: Freshness = Freshness.UNKNOWN
    age_hours: Optional[float] = None
    window: CatalystWindow = CatalystWindow.UNKNOWN

    facts: List[Fact] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)

    # Deduplication. `duplicate_group_id` is shared by every retelling of
    # one event; `canonical_evidence_id` names the best-provenance member.
    duplicate_group_id: Optional[str] = None
    canonical_evidence_id: Optional[str] = None
    is_canonical: bool = True

    llm_status: LLMStatus = LLMStatus.NOT_ATTEMPTED
    llm_model: str = ""

    classification_reason: str = ""
    raw_metadata: Dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.symbol = (self.symbol or "").upper()
        if not self.symbols:
            self.symbols = [self.symbol] if self.symbol else []
        self.symbols = [s.upper() for s in self.symbols]
        self.materiality = max(0.0, min(1.0, float(self.materiality or 0.0)))
        self.novelty = max(0.0, min(1.0, float(self.novelty or 0.0)))

    @staticmethod
    def make_id(provider: str, document_id: str, symbol: str = "") -> str:
        """Stable across runs, so the same article is the same id.

        Derived from provider + document id rather than from content:
        a publisher that edits a headline must not produce a second
        evidence item for the same document.
        """
        digest = hashlib.sha256(
            f"{provider}:{document_id}:{symbol}".encode()).hexdigest()[:16]
        return f"ev_{digest}"

    @property
    def reliability(self) -> float:
        return self.source.reliability

    @property
    def materiality_band(self) -> MaterialityBand:
        if self.materiality >= 0.66:
            return MaterialityBand.HIGH
        if self.materiality >= 0.33:
            return MaterialityBand.MEDIUM
        if self.materiality > 0.0:
            return MaterialityBand.LOW
        return MaterialityBand.NONE

    def as_dict(self) -> Dict:
        return {
            "evidence_id": self.evidence_id,
            "symbol": self.symbol,
            "symbols": list(self.symbols),
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "source": self.source.as_dict(),
            "type": str(self.evidence_type),
            "headline": self.headline,
            "summary": self.summary,
            "direction": str(self.direction),
            "materiality": _round(self.materiality),
            "materiality_band": str(self.materiality_band),
            "novelty": _round(self.novelty),
            "reliability": _round(self.reliability),
            "catalyst_category": (str(self.catalyst_category)
                                  if self.catalyst_category else None),
            "freshness": str(self.freshness),
            "age_hours": _round(self.age_hours, 2),
            "window": str(self.window),
            "facts": [f.as_dict() for f in self.facts],
            "risks": list(self.risks),
            "duplicate_group_id": self.duplicate_group_id,
            "canonical_evidence_id": self.canonical_evidence_id,
            "is_canonical": self.is_canonical,
            "llm_status": str(self.llm_status),
            "llm_used": self.llm_status is LLMStatus.SUCCEEDED,
            "llm_model": self.llm_model,
            "classification_reason": self.classification_reason,
            "warnings": list(self.warnings),
        }


# --- catalyst ------------------------------------------------------------

EVIDENCE_SCORE_MEANING = (
    "evidence_score combines how material the strongest catalyst is, how "
    "new it is, how reliable its source provenance is, and how many "
    "INDEPENDENT sources corroborate it. It is NOT a probability that "
    "the price will rise or fall, not a forecast, and not a measure of "
    "whether the market has already priced the event in."
)

DISCLAIMER = (
    "This is a record of publicly available information and how recent, "
    "material and corroborated it appears. It is not investment advice, "
    "it does not predict prices, and no order can be placed from this "
    "system."
)


@dataclass
class CatalystSummary:
    """The single most consequential item, extracted for display."""
    evidence_id: str
    type: EvidenceType
    direction: Direction
    materiality: float
    novelty: float
    reliability: float
    window: CatalystWindow
    headline: str = ""
    published_at: Optional[str] = None
    source_class: SourceClass = SourceClass.UNKNOWN
    publisher: str = ""
    url: str = ""

    def as_dict(self) -> Dict:
        return {
            "evidence_id": self.evidence_id,
            "type": str(self.type),
            "direction": str(self.direction),
            "materiality": _round(self.materiality),
            "novelty": _round(self.novelty),
            "reliability": _round(self.reliability),
            "window": str(self.window),
            "headline": self.headline,
            "published_at": self.published_at,
            "source_class": str(self.source_class),
            "publisher": self.publisher,
            "url": self.url,
        }


@dataclass
class CatalystResult:
    """What the evidence says about one symbol, right now.

    Carries no trade semantics: no entry, target, stop, size or combined
    score with the quantitative engine. Those belong to the Trade
    Hypothesis Engine, behind the Risk Governor.
    """
    symbol: str
    evaluated_at: str = field(default_factory=utcnow)

    has_active_catalyst: bool = False
    primary_catalyst: Optional[CatalystSummary] = None

    direction: Direction = Direction.NEUTRAL
    materiality: float = 0.0
    novelty: float = 0.0
    evidence_score: float = 0.0

    # Counted AFTER deduplication. `supporting_evidence_count` counts
    # retellings; `independent_source_count` counts distinct provenance.
    # Five syndications of one press release is one event corroborated
    # once, not five catalysts.
    total_evidence_count: int = 0
    supporting_evidence_count: int = 0
    independent_source_count: int = 0
    duplicate_groups: int = 0
    duplicates_collapsed: int = 0
    primary_source_count: int = 0

    conflicting_evidence: bool = False
    conflict_detail: List[str] = field(default_factory=list)

    items: List[EvidenceItem] = field(default_factory=list)
    providers_attempted: List[str] = field(default_factory=list)
    providers_failed: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    execution_available: bool = False

    @property
    def analysis_available(self) -> bool:
        return bool(self.items) or bool(self.providers_attempted)

    def as_dict(self, include_items: bool = True) -> Dict:
        d = {
            "symbol": self.symbol,
            "evaluated_at": self.evaluated_at,
            "has_active_catalyst": self.has_active_catalyst,
            "primary_catalyst": (self.primary_catalyst.as_dict()
                                 if self.primary_catalyst else None),
            "direction": str(self.direction),
            "materiality": _round(self.materiality),
            "novelty": _round(self.novelty),
            "evidence_score": _round(self.evidence_score),
            "evidence_score_meaning": EVIDENCE_SCORE_MEANING,
            "total_evidence_count": self.total_evidence_count,
            "supporting_evidence_count": self.supporting_evidence_count,
            "independent_source_count": self.independent_source_count,
            "duplicate_groups": self.duplicate_groups,
            "duplicates_collapsed": self.duplicates_collapsed,
            "primary_source_count": self.primary_source_count,
            "conflicting_evidence": self.conflicting_evidence,
            "conflict_detail": list(self.conflict_detail),
            "providers_attempted": list(self.providers_attempted),
            "providers_failed": list(self.providers_failed),
            "warnings": list(self.warnings),
            "execution_available": self.execution_available,
            "disclaimer": DISCLAIMER,
            "source_reliability_caveat": INTERPRETATION_CAVEAT,
        }
        if include_items:
            d["items"] = [i.as_dict() for i in self.items]
        return d


@dataclass
class EvidenceRun:
    """A batch of evidence evaluations, usually for scanner candidates."""
    evidence_run_id: str
    session_date: str
    started_at: str
    completed_at: Optional[str] = None
    scanner_run_id: Optional[str] = None
    signal_run_id: Optional[str] = None

    requested_count: int = 0
    evaluated_count: int = 0
    error_count: int = 0

    provider_calls: int = 0
    cache_hits: int = 0
    provider_failures: Dict[str, int] = field(default_factory=dict)
    items_retrieved: int = 0
    items_normalized: int = 0
    duplicates_removed: int = 0
    catalysts_created: int = 0
    primary_source_items: int = 0
    llm_enrichments: int = 0
    llm_failures: int = 0
    duration_seconds: Optional[float] = None

    results: List[CatalystResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    execution_available: bool = False

    @staticmethod
    def make_id(session_date: str, started_at: str) -> str:
        import uuid
        nonce = uuid.uuid4().hex[:8]
        digest = hashlib.sha256(
            f"{session_date}:{started_at}:{nonce}".encode()).hexdigest()[:12]
        return f"evr_{digest}"

    @property
    def primary_source_share(self) -> Optional[float]:
        if not self.items_normalized:
            return None
        return self.primary_source_items / self.items_normalized

    def as_dict(self, include_results: bool = True) -> Dict:
        d = {
            "evidence_run_id": self.evidence_run_id,
            "session_date": self.session_date,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "scanner_run_id": self.scanner_run_id,
            "signal_run_id": self.signal_run_id,
            "requested_count": self.requested_count,
            "evaluated_count": self.evaluated_count,
            "error_count": self.error_count,
            "provider_calls": self.provider_calls,
            "cache_hits": self.cache_hits,
            "provider_failures": dict(self.provider_failures),
            "items_retrieved": self.items_retrieved,
            "items_normalized": self.items_normalized,
            "duplicates_removed": self.duplicates_removed,
            "catalysts_created": self.catalysts_created,
            "primary_source_items": self.primary_source_items,
            "primary_source_share": _round(self.primary_source_share),
            "llm_enrichments": self.llm_enrichments,
            "llm_failures": self.llm_failures,
            "duration_seconds": _round(self.duration_seconds, 3),
            "warnings": list(self.warnings),
            "error": self.error,
            "execution_available": self.execution_available,
        }
        if include_results:
            d["results"] = [r.as_dict() for r in self.results]
        return d
