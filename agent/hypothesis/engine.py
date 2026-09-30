"""
Trade hypothesis generation.

Combines the quantitative reading, the published evidence and the market
regime into a structured argument. Produces no order, no quantity and no
approval; the Risk Governor is the only component that can approve
anything and it runs afterwards.

Three cases drive the design, and all three must be expressible:

  quant BUY, no evidence          a MOMENTUM hypothesis, honestly
                                  labelled as having no catalyst behind
                                  it. Price action is real information
                                  even when nobody has published a
                                  reason.

  quant BUY, material NEGATIVE    a BLOCKING contradiction. Buying into
                                  a published negative because the chart
                                  looks good is the single most
                                  expensive way to use this system.

  quant NEUTRAL, evidence POSITIVE  NOT a trade. Good news about a stock
                                  that is not moving is a story. The
                                  market has seen the news and declined
                                  to act on it, and a system that bought
                                  every positive headline would be
                                  trading the news wire, not the market.

Everything here is deterministic. A language model never decides a
strategy, a strength or a contradiction.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..observability import log_event
from .models import (
    Contradiction, ContradictionSeverity, EvidenceView, HypothesisDirection,
    HypothesisStatus, MarketView, QuantitativeView, Strategy, TradeHypothesis,
    utcnow,
)

# Version stamped onto every hypothesis. Changing any threshold below
# means bumping this, or later analysis cannot reproduce why a trade
# happened.
CONFIG_VERSION = "hypothesis-v1.0.0"

# --- thresholds ---------------------------------------------------------
# Every number here is a CHOICE, not a measurement. None has been
# validated out of sample.

# Below this, the independent groups do not agree enough to act on.
MIN_AGREEMENT = 0.50

# Below this, the readings are technically directional but feeble.
MIN_MAGNITUDE = 0.35

# A catalyst must clear these to count as supporting a trade.
MIN_CATALYST_MATERIALITY = 0.40
MIN_CATALYST_NOVELTY = 0.30

# Regimes in which no new long exposure is proposed at all. The scanner
# already raises its bar in these; the hypothesis engine declines
# outright, because a long-only system in a falling market is choosing
# the one direction the market is punishing.
HOSTILE_REGIMES = frozenset({"STRONG_BEARISH"})

# Risk postures that forbid new exposure regardless of the setup.
BLOCKING_POSTURES = frozenset({"NO_NEW_TRADES", "HALT"})

# Data older than this cannot support a fresh intraday hypothesis.
BLOCKING_FRESHNESS = frozenset({"STALE", "MISSING", "UNKNOWN"})

# Default stop distance, expressed in units of the security's own recent
# volatility rather than a flat percentage: a 2% stop is noise on one
# name and a disaster on another.
STOP_VOLATILITY_MULTIPLE = 1.5
MIN_STOP_DISTANCE_PCT = 1.0
MAX_STOP_DISTANCE_PCT = 8.0


def _quant_view(signal: Dict) -> QuantitativeView:
    dq = signal.get("data_quality") or {}
    return QuantitativeView(
        direction=str(signal.get("direction", "NO_SIGNAL")),
        agreement=float(signal.get("signal_agreement") or 0.0),
        magnitude=float(signal.get("signal_magnitude") or 0.0),
        regime_adjusted_magnitude=float(
            signal.get("regime_adjusted_magnitude") or 0.0),
        strength_band=str(signal.get("strength_band", "NONE")),
        buy_groups=int(signal.get("buy_groups") or 0),
        sell_groups=int(signal.get("sell_groups") or 0),
        opinionated_groups=int(signal.get("opinionated_groups") or 0),
        freshness=str(dq.get("freshness", "UNKNOWN")),
    )


def _evidence_view(catalyst: Optional[Dict]) -> EvidenceView:
    if not catalyst:
        # Never looked. Distinct from "looked and found nothing", which
        # is a genuine observation about the world.
        return EvidenceView(collected=False)
    primary = catalyst.get("primary_catalyst") or {}
    return EvidenceView(
        active_catalyst=bool(catalyst.get("has_active_catalyst")),
        direction=str(catalyst.get("direction", "NEUTRAL")),
        materiality=float(primary.get("materiality")
                          or catalyst.get("materiality") or 0.0),
        novelty=float(primary.get("novelty")
                      or catalyst.get("novelty") or 0.0),
        evidence_score=float(catalyst.get("evidence_score") or 0.0),
        catalyst_type=primary.get("type"),
        window=primary.get("window"),
        independent_sources=int(catalyst.get("independent_source_count") or 0),
        primary_sources=int(catalyst.get("primary_source_count") or 0),
        conflicting=bool(catalyst.get("conflicting_evidence")),
        collected=True,
    )


def _market_view(regime: Optional[Dict]) -> MarketView:
    regime = regime or {}
    return MarketView(
        regime=str(regime.get("regime", "UNKNOWN")),
        regime_confidence=float(regime.get("regime_confidence")
                                or regime.get("confidence") or 0.0),
        risk_posture=str(regime.get("risk_posture", "NO_NEW_TRADES")),
        session=str(regime.get("market_session", "UNKNOWN")),
    )


def choose_strategy(quant: QuantitativeView, evidence: EvidenceView
                    ) -> Tuple[Strategy, List[str]]:
    """Pick the strategy the setup actually fits, or none.

    The order matters: the most specific claim is tested first, so a
    setup that genuinely has both momentum and a catalyst is labelled
    MOMENTUM_CATALYST rather than falling through to plain MOMENTUM.
    """
    reasons: List[str] = []
    quant_long = quant.direction == "BUY"
    catalyst_supports = (
        evidence.active_catalyst
        and evidence.direction == "POSITIVE"
        and evidence.materiality >= MIN_CATALYST_MATERIALITY
        and evidence.novelty >= MIN_CATALYST_NOVELTY
    )

    if quant_long and catalyst_supports:
        reasons.append(
            f"Price action and a published {evidence.catalyst_type} catalyst "
            f"point the same way.")
        return Strategy.MOMENTUM_CATALYST, reasons

    if quant_long and quant.agreement >= 0.99 and quant.magnitude >= 0.60:
        reasons.append(
            "Every independent signal group agrees and the readings are "
            "emphatic.")
        return Strategy.BREAKOUT, reasons

    if quant_long:
        note = ("no published catalyst was found"
                if evidence.collected else "evidence was not collected")
        reasons.append(
            f"Independent signal groups support upward direction; {note}.")
        return Strategy.MOMENTUM, reasons

    # Quant is not long. A catalyst alone is not a setup.
    if catalyst_supports and quant.direction == "NEUTRAL":
        # Deliberately NOT actionable. The market has seen the news and
        # has not moved on it; buying here is trading the wire.
        reasons.append(
            "A positive catalyst exists but price action does not confirm "
            "it. Published good news that the market has not acted on is a "
            "story, not a setup.")
        return Strategy.NO_VALID_STRATEGY, reasons

    return Strategy.NO_VALID_STRATEGY, [
        "No strategy fits: the quantitative reading does not support a long "
        "position."]


def find_contradictions(quant: QuantitativeView, evidence: EvidenceView,
                        market: MarketView) -> List[Contradiction]:
    """Everything arguing against the idea, recorded rather than netted.

    Kept as itemised records so a reader sees what the engine was
    worried about even where it proceeded anyway.
    """
    out: List[Contradiction] = []

    # --- blocking ---
    if quant.freshness in BLOCKING_FRESHNESS:
        out.append(Contradiction(
            "STALE_QUANTITATIVE_DATA", ContradictionSeverity.BLOCKING,
            f"price data freshness is {quant.freshness}; a fresh intraday "
            f"hypothesis cannot rest on it", 1.0))

    if market.regime in HOSTILE_REGIMES:
        out.append(Contradiction(
            "HOSTILE_REGIME", ContradictionSeverity.BLOCKING,
            f"market regime is {market.regime}; a long-only system does not "
            f"propose new exposure into it", 1.0))

    if market.risk_posture in BLOCKING_POSTURES:
        out.append(Contradiction(
            "RISK_POSTURE_BLOCKS", ContradictionSeverity.BLOCKING,
            f"risk posture is {market.risk_posture}", 1.0))

    if (evidence.active_catalyst and evidence.direction == "NEGATIVE"
            and evidence.materiality >= MIN_CATALYST_MATERIALITY):
        # The most expensive mistake this system could make.
        out.append(Contradiction(
            "MATERIAL_NEGATIVE_EVIDENCE", ContradictionSeverity.BLOCKING,
            f"a material negative {evidence.catalyst_type} catalyst is "
            f"published (materiality {evidence.materiality:.2f}); buying "
            f"into it because the chart looks good is not a thesis", 1.0))

    # --- major ---
    if quant.agreement < MIN_AGREEMENT and quant.direction == "BUY":
        out.append(Contradiction(
            "WEAK_SIGNAL_AGREEMENT", ContradictionSeverity.MAJOR,
            f"only {quant.agreement:.2f} agreement across independent "
            f"groups", 0.35))

    if quant.magnitude < MIN_MAGNITUDE and quant.direction == "BUY":
        out.append(Contradiction(
            "WEAK_SIGNAL_MAGNITUDE", ContradictionSeverity.MAJOR,
            f"readings are only {quant.magnitude:.2f} beyond their "
            f"thresholds", 0.25))

    if evidence.conflicting:
        out.append(Contradiction(
            "CONFLICTING_EVIDENCE", ContradictionSeverity.MAJOR,
            "published evidence points both ways", 0.25))

    if quant.sell_groups > 0 and quant.direction == "BUY":
        out.append(Contradiction(
            "OPPOSING_SIGNAL_GROUP", ContradictionSeverity.MAJOR,
            f"{quant.sell_groups} independent group(s) argue the other way",
            0.20))

    # --- minor ---
    if market.regime in ("MIXED", "VOLATILE", "BEARISH"):
        out.append(Contradiction(
            "UNFAVOURABLE_REGIME", ContradictionSeverity.MINOR,
            f"market regime is {market.regime}", 0.10))

    if market.regime == "UNKNOWN":
        out.append(Contradiction(
            "UNKNOWN_REGIME", ContradictionSeverity.MINOR,
            "the market regime could not be determined, so this reading is "
            "not regime-informed", 0.15))

    if evidence.collected and not evidence.active_catalyst:
        out.append(Contradiction(
            "NO_CATALYST", ContradictionSeverity.MINOR,
            "no published catalyst explains the move", 0.05))

    if not evidence.collected:
        out.append(Contradiction(
            "EVIDENCE_NOT_COLLECTED", ContradictionSeverity.MINOR,
            "evidence was not collected, so a published negative could "
            "exist unseen", 0.10))

    return out


def compute_strength(quant: QuantitativeView, evidence: EvidenceView,
                     market: MarketView,
                     contradictions: List[Contradiction]) -> Tuple[float, Dict]:
    """A combined number whose parts remain visible.

    Returned alongside `strength_components`, so the score can always be
    taken apart. A single opaque figure would make the whole pipeline
    unauditable, which is the thing this project keeps refusing to do.
    """
    components: Dict[str, float] = {}

    # Structure and size contribute separately, as they do upstream.
    components["signal_agreement"] = quant.agreement * 0.35
    components["signal_magnitude"] = quant.magnitude * 0.25

    # Evidence contributes only when it genuinely supports the direction.
    if (evidence.active_catalyst and evidence.direction == "POSITIVE"):
        support = (evidence.materiality * evidence.novelty
                   * min(1.0, 0.6 + 0.2 * evidence.independent_sources))
        components["evidence_support"] = support * 0.25
    else:
        components["evidence_support"] = 0.0

    # A hospitable regime helps a little; a hostile one is handled as a
    # contradiction rather than a negative term here.
    regime_factor = {
        "STRONG_BULLISH": 1.0, "BULLISH": 0.9, "NEUTRAL": 0.6,
        "MIXED": 0.4, "VOLATILE": 0.3, "BEARISH": 0.2,
        "STRONG_BEARISH": 0.0, "UNKNOWN": 0.2,
    }.get(market.regime, 0.2)
    components["market_regime"] = regime_factor * 0.15

    base = sum(components.values())

    penalty = sum(c.penalty for c in contradictions
                  if c.severity is not ContradictionSeverity.BLOCKING)
    components["contradiction_penalty"] = -min(penalty, 0.9)

    strength = max(0.0, min(1.0, base - min(penalty, 0.9)))
    return strength, components


def suggest_stop_distance(volatility_pct: Optional[float]) -> float:
    """How far wrong the idea could go before it is simply wrong.

    Expressed in units of the security's own recent volatility: a flat
    2% stop is noise on one name and a catastrophe on another. This is
    an ABSTRACT risk input for the Risk Governor, not an order.
    """
    if volatility_pct is None or volatility_pct <= 0:
        return MIN_STOP_DISTANCE_PCT * 2
    raw = volatility_pct * STOP_VOLATILITY_MULTIPLE
    return max(MIN_STOP_DISTANCE_PCT, min(MAX_STOP_DISTANCE_PCT, raw))


def generate(symbol: str, signal: Dict,
             catalyst: Optional[Dict] = None,
             regime: Optional[Dict] = None,
             scanner_run_id: Optional[str] = None,
             signal_run_id: Optional[str] = None,
             evidence_run_id: Optional[str] = None) -> TradeHypothesis:
    """Build a hypothesis for one symbol.

    Always returns an object. A setup that does not qualify comes back
    with `status = NOT_GENERATED` and the reasons why, because "we
    looked and declined" is information the journal needs as much as a
    trade is.
    """
    from .reasons import build_reasons

    quant = _quant_view(signal or {})
    evidence = _evidence_view(catalyst)
    market = _market_view(regime)

    generated_at = utcnow()
    hypothesis = TradeHypothesis(
        hypothesis_id=TradeHypothesis.make_id(symbol, generated_at),
        symbol=(symbol or "").upper(),
        generated_at=generated_at,
        quantitative=quant, evidence=evidence, market=market,
        config_version=CONFIG_VERSION,
        scanner_run_id=scanner_run_id, signal_run_id=signal_run_id,
        evidence_run_id=evidence_run_id,
        reference_price=(signal or {}).get("price"),
    )

    strategy, strategy_reasons = choose_strategy(quant, evidence)
    contradictions = find_contradictions(quant, evidence, market)
    hypothesis.contradictions = contradictions

    blocking = [c for c in contradictions
                if c.severity is ContradictionSeverity.BLOCKING]

    if strategy is Strategy.NO_VALID_STRATEGY or blocking:
        hypothesis.strategy = Strategy.NO_VALID_STRATEGY
        hypothesis.direction = HypothesisDirection.NONE
        hypothesis.status = HypothesisStatus.NOT_GENERATED
        hypothesis.hypothesis_strength = 0.0
        hypothesis.supporting_reasons = strategy_reasons
        if blocking:
            hypothesis.warnings.append(
                "blocked by: " + ", ".join(c.code for c in blocking))
        hypothesis.supporting_reasons = build_reasons(hypothesis,
                                                      strategy_reasons)
        log_event("hypothesis_rejected", symbol=hypothesis.symbol,
                  reasons=[c.code for c in blocking] or ["NO_VALID_STRATEGY"])
        return hypothesis

    strength, components = compute_strength(quant, evidence, market,
                                            contradictions)

    volatility = ((signal or {}).get("indicators") or {}).get("volatility_pct")
    hypothesis.strategy = strategy
    hypothesis.direction = HypothesisDirection.LONG
    hypothesis.status = HypothesisStatus.PROPOSED
    hypothesis.hypothesis_strength = strength
    hypothesis.strength_components = components
    hypothesis.reference_volatility_pct = volatility
    hypothesis.suggested_stop_distance_pct = suggest_stop_distance(volatility)
    hypothesis.supporting_reasons = build_reasons(hypothesis, strategy_reasons)

    log_event("hypothesis_generated", symbol=hypothesis.symbol,
              strategy=str(strategy), strength=round(strength, 4),
              contradictions=[c.code for c in contradictions],
              config_version=CONFIG_VERSION)
    return hypothesis
