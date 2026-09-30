"""
Scanner scoring.

Produces a 0–100 research-priority score with every component retained so
a ranking can be audited after the fact.

**What the score means.** It is an ordering of *where to look first*. A
score of 85 says "this symbol is liquid, unusually active and moving, so
analyse it before the one scoring 40". It does **not** say the price will
rise, and it is not a probability or an expected return. The
`score_meaning` field travels with every candidate for exactly this
reason.

Each component is normalised to [0, 1] before weighting, so a weight of
0.30 really does contribute at most 30 points. Components that cannot be
computed are excluded and their weight redistributed across the rest,
rather than scored as zero — a missing feature would otherwise be
indistinguishable from a genuinely poor one.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..config import ScannerWeights
from .models import DataFreshness, ScannerFeatures, ScannerSnapshot

# Reference magnitudes. Judgement calls about what "notable" looks like
# for a liquid US equity intraday, not measured constants.
_REF_DOLLAR_VOLUME = 200_000_000.0   # $200M session turnover scores full marks
_REF_SPREAD_PCT = 0.10               # 0.10% spread is excellent
_REF_RELATIVE_VOLUME = 2.0           # 2x projected normal volume is notable
_REF_MOMENTUM_PCT = 2.0              # a 2% short-window move is decisive
_REF_RELATIVE_STRENGTH_PCT = 1.5     # 1.5pp ahead of the benchmark is strong


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _log_scaled(value: Optional[float], reference: float) -> Optional[float]:
    """Compress a positive, heavy-tailed quantity into [0, 1].

    Dollar volume spans several orders of magnitude, so a linear scale
    would give every ordinary name ~0 and let a handful of mega-caps
    occupy the whole range.
    """
    if value is None or value <= 0 or reference <= 0:
        return None
    return _clamp01(math.log1p(value) / math.log1p(reference))


def _signed_scaled(value: Optional[float], reference: float) -> Optional[float]:
    """Map a signed percentage onto [0, 1] with 0.5 as neutral.

    Long-only research: upside is rewarded and downside is not merely
    unrewarded but penalised below neutral.
    """
    if value is None or reference <= 0:
        return None
    return _clamp01(0.5 + (value / reference) * 0.5)


class ScannerScorer:

    def __init__(self, weights: Optional[ScannerWeights] = None,
                 momentum_weight_multiplier: float = 1.0,
                 stale_score_penalty: float = 0.5):
        self.weights = weights or ScannerWeights()
        self.weights.validate()
        self.momentum_weight_multiplier = momentum_weight_multiplier
        self.stale_score_penalty = stale_score_penalty

    # -- components -------------------------------------------------------

    def _liquidity(self, snapshot: ScannerSnapshot) -> Optional[float]:
        """Turnover and spread together: a name can be heavily traded and
        still expensive to get in and out of."""
        turnover = _log_scaled(snapshot.dollar_volume, _REF_DOLLAR_VOLUME)
        spread_pct = snapshot.spread_pct
        tightness = None
        if spread_pct is not None and spread_pct >= 0:
            tightness = _clamp01(_REF_SPREAD_PCT / max(spread_pct, 1e-6))

        parts = [p for p in (turnover, tightness) if p is not None]
        if not parts:
            return None
        # Turnover weighted more heavily: it is the harder constraint to
        # satisfy and the less noisy measurement.
        if turnover is not None and tightness is not None:
            return 0.65 * turnover + 0.35 * tightness
        return parts[0]

    @staticmethod
    def _relative_volume(features: ScannerFeatures) -> Optional[float]:
        if features.relative_volume is None:
            return None
        return _clamp01(features.relative_volume / _REF_RELATIVE_VOLUME)

    def _momentum(self, features: ScannerFeatures) -> Optional[float]:
        """Short-window returns, weighted toward the recent end.

        Windows that are unavailable are dropped and the remaining
        weights renormalised, so a symbol with only a session change is
        not scored as though its 5-minute return were zero.
        """
        windows = (
            (features.return_5m, 0.40),
            (features.return_15m, 0.35),
            (features.return_30m, 0.25),
        )
        available = [(v, w) for v, w in windows if v is not None]
        if not available:
            # Fall back to the session change, which the snapshot alone
            # provides, rather than returning nothing.
            return _signed_scaled(features.session_change_pct, _REF_MOMENTUM_PCT * 2)

        total_weight = sum(w for _v, w in available)
        score = 0.0
        for value, weight in available:
            scaled = _signed_scaled(value, _REF_MOMENTUM_PCT)
            score += (scaled if scaled is not None else 0.5) * (weight / total_weight)
        return _clamp01(score)

    @staticmethod
    def _relative_strength(features: ScannerFeatures) -> Optional[float]:
        return _signed_scaled(features.market_relative_strength,
                              _REF_RELATIVE_STRENGTH_PCT)

    @staticmethod
    def _vwap_and_range(features: ScannerFeatures) -> Optional[float]:
        """Above VWAP and holding the upper part of the day's range is the
        shape worth a second look on the long side."""
        parts: List[float] = []
        if features.distance_from_vwap_pct is not None:
            parts.append(_signed_scaled(features.distance_from_vwap_pct, 1.0) or 0.5)
        if features.range_position is not None:
            parts.append(_clamp01(features.range_position))
        if not parts:
            return None
        return sum(parts) / len(parts)

    @staticmethod
    def _data_quality(snapshot: ScannerSnapshot,
                      features: ScannerFeatures) -> float:
        """Rewards complete, fresh inputs. Always computable, so it never
        needs redistribution."""
        if snapshot.freshness is DataFreshness.MISSING:
            return 0.0
        score = 1.0 if snapshot.freshness is DataFreshness.FRESH else 0.4
        present = sum(1 for v in (
            features.return_5m, features.return_15m, features.relative_volume,
            features.distance_from_vwap_pct, features.market_relative_strength,
        ) if v is not None)
        completeness = present / 5.0
        return _clamp01(0.5 * score + 0.5 * completeness)

    # -- aggregation ------------------------------------------------------

    def score(self, snapshot: ScannerSnapshot, features: ScannerFeatures
              ) -> Tuple[float, Dict[str, float]]:
        """Returns (0–100 score, component detail) — ranking only."""
        weights = self.weights
        momentum_weight = weights.short_term_momentum * self.momentum_weight_multiplier

        components: Dict[str, Tuple[Optional[float], float]] = {
            "liquidity": (self._liquidity(snapshot), weights.liquidity),
            "relative_volume": (self._relative_volume(features),
                                weights.relative_volume),
            "short_term_momentum": (self._momentum(features), momentum_weight),
            "market_relative_strength": (self._relative_strength(features),
                                         weights.market_relative_strength),
            "vwap_and_range": (self._vwap_and_range(features),
                               weights.vwap_and_range),
            "data_quality": (self._data_quality(snapshot, features),
                             weights.data_quality),
        }

        usable = {k: (v, w) for k, (v, w) in components.items() if v is not None}
        detail: Dict[str, float] = {}

        if not usable:
            return 0.0, {"unscorable": 1.0}

        active_weight = sum(w for _v, w in usable.values())
        raw = 0.0
        for name, (value, weight) in usable.items():
            share = weight / active_weight
            contribution = value * share
            raw += contribution
            detail[name] = round(value, 4)
            detail[f"{name}_contribution"] = round(contribution * 100, 3)

        for name in components:
            if name not in usable:
                detail[f"{name}_missing"] = 1.0

        score = _clamp01(raw) * 100.0

        # A stale snapshot is penalised rather than silently ranked
        # alongside fresh ones.
        if snapshot.freshness is DataFreshness.STALE:
            score *= self.stale_score_penalty
            detail["stale_penalty_applied"] = self.stale_score_penalty

        detail["active_weight"] = round(active_weight, 4)
        return round(score, 2), detail


def rank_candidates(candidates: List) -> List:
    """Sort by score descending, then symbol ascending.

    The symbol tie-break makes the ordering total and therefore
    reproducible: two symbols with identical scores must not swap places
    between runs on the same data, or a ranking cannot be audited.
    """
    ordered = sorted(candidates, key=lambda c: (-c.scanner_score, c.symbol))
    for index, candidate in enumerate(ordered, start=1):
        candidate.rank = index
    return ordered
