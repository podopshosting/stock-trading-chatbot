"""
Catalyst aggregation.

Turns a pile of normalised evidence into one auditable answer:

    What happened, how recent and material is it, which trustworthy
    sources support it, and does the evidence agree or conflict?

It does NOT decide whether to trade, and it does not combine with the
quantitative signal engine. Those stay separate until the Trade
Hypothesis Engine, because the moment a single number blends price
behaviour with a headline, neither can be inspected.

Two properties are defended throughout:

  Conflict survives.   "Revenue beat, guidance cut" aggregates to MIXED.
                       A net sentiment score would report a small
                       positive and hide the contradiction, which is the
                       opposite of what a reader needs.

  Republication is not corroboration.  Deduplication runs BEFORE any
                       counting, and corroboration is counted in distinct
                       provenances, never in articles.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from ..observability import log_event
from .dedup import deduplicate, independent_source_count, novelty_for
from .models import (
    CatalystResult, CatalystSummary, CatalystWindow, Direction, EvidenceItem,
    EvidenceType, Freshness, SourceClass, WINDOW_BOUNDS_HOURS,
)

# How long an event stays relevant, as a half-life in hours. These are
# CHOICES about how quickly a kind of news stops explaining today's
# price, not measurements. A 10-K remains useful fundamentally for a long
# time while being nobody's intraday catalyst; an analyst upgrade is
# mostly spent within a couple of sessions.
DECAY_HALF_LIFE_HOURS: Dict[EvidenceType, float] = {
    EvidenceType.EARNINGS: 48.0,
    EvidenceType.GUIDANCE: 72.0,
    EvidenceType.REVENUE_UPDATE: 48.0,
    EvidenceType.ACQUISITION: 168.0,
    EvidenceType.MERGER: 168.0,
    EvidenceType.BANKRUPTCY: 336.0,
    EvidenceType.FDA: 168.0,
    EvidenceType.REGULATORY_APPROVAL: 168.0,
    EvidenceType.REGULATORY_REJECTION: 168.0,
    EvidenceType.SHARE_OFFERING: 72.0,
    EvidenceType.DILUTION: 72.0,
    EvidenceType.SHELF_REGISTRATION: 336.0,
    EvidenceType.MANAGEMENT_CHANGE: 120.0,
    EvidenceType.ANALYST_UPGRADE: 24.0,
    EvidenceType.ANALYST_DOWNGRADE: 24.0,
    EvidenceType.PRICE_TARGET_CHANGE: 12.0,
    EvidenceType.LAWSUIT: 168.0,
    EvidenceType.SETTLEMENT: 168.0,
    EvidenceType.PERIODIC_REPORT: 720.0,
    EvidenceType.INSIDER_BUY: 240.0,
    EvidenceType.INSIDER_SELL: 240.0,
    EvidenceType.PRODUCT_LAUNCH: 120.0,
    EvidenceType.CONTRACT: 120.0,
    EvidenceType.PARTNERSHIP: 120.0,
}
DEFAULT_HALF_LIFE_HOURS = 48.0

# An item must clear this much decayed materiality to count as an active
# catalyst at all.
ACTIVE_CATALYST_THRESHOLD = 0.30

# Beyond this window an item is background context, never "the catalyst".
ACTIVE_CATALYST_MAX_HOURS = 24.0 * 14


def classify_window(age_hours: Optional[float]) -> CatalystWindow:
    """Name the recency band. A month-old story is not breaking."""
    if age_hours is None:
        return CatalystWindow.UNKNOWN
    if age_hours <= WINDOW_BOUNDS_HOURS[CatalystWindow.BREAKING]:
        return CatalystWindow.BREAKING
    if age_hours <= WINDOW_BOUNDS_HOURS[CatalystWindow.INTRADAY]:
        return CatalystWindow.INTRADAY
    if age_hours <= WINDOW_BOUNDS_HOURS[CatalystWindow.RECENT]:
        return CatalystWindow.RECENT
    if age_hours <= WINDOW_BOUNDS_HOURS[CatalystWindow.BACKGROUND]:
        return CatalystWindow.BACKGROUND
    return CatalystWindow.STALE


def classify_freshness(age_hours: Optional[float]) -> Freshness:
    if age_hours is None:
        return Freshness.UNKNOWN
    if age_hours <= 24:
        return Freshness.FRESH
    if age_hours <= 24 * 30:
        return Freshness.STALE
    return Freshness.MISSING


def decay_factor(evidence_type: EvidenceType,
                 age_hours: Optional[float]) -> float:
    """Exponential decay on a per-type half-life.

    Old evidence is never deleted - it is still true that the filing
    happened - but its claim on explaining *today's* movement fades.
    """
    if age_hours is None:
        # Unknown age cannot be assumed current. Assuming freshness would
        # let an undated item outrank a dated, genuinely recent one.
        return 0.5
    if age_hours <= 0:
        return 1.0
    half_life = DECAY_HALF_LIFE_HOURS.get(evidence_type,
                                          DEFAULT_HALF_LIFE_HOURS)
    return math.pow(0.5, age_hours / half_life)


def current_relevance(item: EvidenceItem) -> float:
    """How much this item can explain present behaviour."""
    return item.materiality * decay_factor(item.evidence_type, item.age_hours)


# --- conflict ------------------------------------------------------------

_POSITIVE = (Direction.POSITIVE,)
_NEGATIVE = (Direction.NEGATIVE,)


def detect_conflict(items: Sequence[EvidenceItem]) -> Dict:
    """Find contradiction among the material evidence.

    Only items that still carry weight are considered: a stale positive
    story and a fresh negative one are not a live conflict, they are one
    current event and some history.
    """
    weighted = [(i, current_relevance(i)) for i in items]
    material = [(i, w) for i, w in weighted if w >= 0.15]

    positives = [i for i, _ in material if i.direction in _POSITIVE]
    negatives = [i for i, _ in material if i.direction in _NEGATIVE]
    explicit_mixed = [i for i, _ in material if i.direction is Direction.MIXED]

    detail: List[str] = []
    for item in explicit_mixed:
        detail.append(f"{item.headline[:90]} - assessed as MIXED on its own")
    if positives and negatives:
        detail.append(
            "positive: " + "; ".join(i.headline[:70] for i in positives[:3]))
        detail.append(
            "negative: " + "; ".join(i.headline[:70] for i in negatives[:3]))

    conflicting = bool(positives and negatives) or bool(explicit_mixed)
    return {
        "conflicting": conflicting,
        "detail": detail,
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "mixed_count": len(explicit_mixed),
    }


def aggregate_direction(items: Sequence[EvidenceItem],
                        conflict: Dict) -> Direction:
    """The overall tone, with contradiction preserved.

    MIXED is a real answer and must be reachable. An aggregate that could
    only return POSITIVE, NEGATIVE or NEUTRAL would have to resolve
    "beat but guided down" by picking a side.
    """
    if conflict["conflicting"]:
        return Direction.MIXED

    weighted = [(i, current_relevance(i)) for i in items]
    material = [(i, w) for i, w in weighted if w >= 0.15]
    if not material:
        return Direction.NEUTRAL

    positive = sum(w for i, w in material if i.direction is Direction.POSITIVE)
    negative = sum(w for i, w in material if i.direction is Direction.NEGATIVE)
    uncertain = sum(w for i, w in material
                    if i.direction is Direction.UNCERTAIN)

    if positive > negative and positive > 0:
        return Direction.POSITIVE
    if negative > positive and negative > 0:
        return Direction.NEGATIVE
    if uncertain > 0:
        # Material, but the evidence genuinely does not say which way.
        return Direction.UNCERTAIN
    return Direction.NEUTRAL


# --- scoring -------------------------------------------------------------

def evidence_score(primary: Optional[EvidenceItem],
                   independent_sources: int) -> float:
    """A single summary number, and what it does NOT mean.

    It combines how material the strongest catalyst is, how new it is,
    how reliable its provenance is, and how many independent sources
    corroborate it.

    It is NOT a probability that the price will move, not a forecast, and
    not a statement about whether the market has already priced the event
    in. It is a measure of the EVIDENCE, not of the opportunity.
    """
    if primary is None:
        return 0.0

    relevance = current_relevance(primary)
    base = relevance * primary.novelty * primary.reliability

    # Corroboration helps, with sharply diminishing returns: a second
    # independent source is worth a great deal, a fifth very little.
    # Linear growth here would recreate the "more articles = more
    # conviction" error that deduplication exists to prevent.
    corroboration = 1.0 + 0.25 * math.log2(max(1, independent_sources))
    return max(0.0, min(1.0, base * corroboration))


# --- top level -----------------------------------------------------------

def build_catalyst_result(symbol: str, items: Sequence[EvidenceItem],
                          providers_attempted: Sequence[str] = (),
                          providers_failed: Sequence[str] = (),
                          prior_items: Optional[Sequence[EvidenceItem]] = None,
                          warnings: Optional[Sequence[str]] = None
                          ) -> CatalystResult:
    """Aggregate evidence for one symbol into an auditable result."""
    result = CatalystResult(
        symbol=(symbol or "").upper(),
        providers_attempted=list(providers_attempted),
        providers_failed=list(providers_failed),
        warnings=list(warnings or []),
    )

    if providers_failed:
        result.warnings.append(
            f"evidence is incomplete: {', '.join(providers_failed)} did not "
            f"respond, so this result may be missing material information")

    items = list(items)
    result.total_evidence_count = len(items)

    if not items:
        result.has_active_catalyst = False
        result.direction = Direction.NEUTRAL
        # Explicitly a valid outcome. Inventing a narrative because the
        # price moved is the failure mode this guards against.
        result.warnings.append(
            "no evidence retrieved for this symbol in the requested window")
        return result

    # 1. Window and freshness, before anything else uses age.
    for item in items:
        item.window = classify_window(item.age_hours)
        item.freshness = classify_freshness(item.age_hours)

    # 2. Deduplicate BEFORE counting anything.
    grouped = deduplicate(items)
    result.duplicate_groups = grouped["group_count"]
    result.duplicates_collapsed = grouped["duplicates_collapsed"]
    canonical = grouped["canonical_items"]

    # 3. Novelty, using the whole group so a follow-up is not treated as
    #    a break.
    for group in grouped["groups"]:
        for member in group:
            member.novelty = novelty_for(member, group, prior_items)

    result.items = items
    result.supporting_evidence_count = len(canonical)
    result.primary_source_count = sum(
        1 for i in canonical if i.source.is_primary)

    # 4. Conflict, on canonical items only - counting duplicates would
    #    let a heavily syndicated positive outvote a primary negative.
    conflict = detect_conflict(canonical)
    result.conflicting_evidence = conflict["conflicting"]
    result.conflict_detail = conflict["detail"]
    result.direction = aggregate_direction(canonical, conflict)

    # 5. The strongest current catalyst.
    ranked = sorted(canonical,
                    key=lambda i: (current_relevance(i) * i.novelty
                                   * i.reliability),
                    reverse=True)
    best = ranked[0] if ranked else None

    if best is not None:
        group = next((g for g in grouped["groups"]
                      if any(m.evidence_id == best.evidence_id for m in g)),
                     [best])
        sources = independent_source_count(group)
        result.independent_source_count = sources

        active = (current_relevance(best) >= ACTIVE_CATALYST_THRESHOLD
                  and (best.age_hours is None
                       or best.age_hours <= ACTIVE_CATALYST_MAX_HOURS))
        result.has_active_catalyst = active
        result.materiality = best.materiality
        result.novelty = best.novelty
        result.evidence_score = evidence_score(best, sources)

        if active:
            result.primary_catalyst = CatalystSummary(
                evidence_id=best.evidence_id,
                type=best.evidence_type,
                direction=best.direction,
                materiality=best.materiality,
                novelty=best.novelty,
                reliability=best.reliability,
                window=best.window,
                headline=best.headline,
                published_at=best.published_at,
                source_class=best.source.source_class,
                publisher=best.source.publisher,
                url=best.source.url,
            )
            log_event("catalyst_created", symbol=result.symbol,
                      type=str(best.evidence_type),
                      direction=str(best.direction),
                      materiality=round(best.materiality, 3),
                      novelty=round(best.novelty, 3),
                      independent_sources=sources,
                      window=str(best.window))
        else:
            result.warnings.append(
                "evidence was found but none of it is current or material "
                "enough to be an active catalyst")

    if result.conflicting_evidence:
        log_event("conflicting_evidence_detected", symbol=result.symbol,
                  positive=conflict["positive_count"],
                  negative=conflict["negative_count"],
                  mixed=conflict["mixed_count"])

    if result.duplicates_collapsed:
        log_event("evidence_duplicate_detected", symbol=result.symbol,
                  collapsed=result.duplicates_collapsed,
                  groups=result.duplicate_groups)

    stale = [i for i in canonical if i.window is CatalystWindow.STALE]
    if stale and not result.has_active_catalyst:
        log_event("evidence_stale", symbol=result.symbol, count=len(stale))

    return result
