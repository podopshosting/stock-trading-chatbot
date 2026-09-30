"""
Market Regime Engine.

A pure function from normalised market data to a structured classification.
No provider code, no network, no LLM. The same inputs always produce the
same output, which is what makes it testable and what makes a later
explanation trustworthy - the LLM describes this result, it never decides
it.

Three design decisions worth stating plainly:

**Volatility is not a directional input.** A violent selloff and a violent
rally both raise realised volatility, so giving it a weight in a
directional score would be a category error. It acts as a confidence
penalty and, at the extreme, overrides the label to VOLATILE.

**Confidence is not a probability.** It measures agreement among the
inputs and the quality of the data behind them. A confidence of 0.9 means
"the evidence is consistent and fresh", never "there is a 90% chance the
market rises". Nothing here forecasts anything.

**Missing is not neutral.** An index we could not price is excluded from
the score and lowers confidence; it is never treated as flat, which would
quietly pull the score toward zero and look like genuine indecision.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..config import DEFAULT_CONFIG, RegimeConfig
from ..observability import log_event

# Reference magnitudes used to squash percentage features into [-1, +1].
# These are judgement calls about what counts as a big move for a broad
# index, not measured constants.
_REF_PRICE_VS_SMA = 0.02      # 2% above/below the short average is decisive
_REF_SMA_SPREAD = 0.015       # 1.5% between the averages is a clear trend
_REF_INTRADAY = 0.010         # a 1% index day is a decisive session
_REF_VWAP_DISTANCE = 0.005    # 0.5% from VWAP is meaningful intraday
_VWAP_DEADBAND = 0.0005       # within 0.05% counts as "at" VWAP, not above it


class Freshness:
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _scaled(value: float, reference: float) -> float:
    """Map a proportional move onto [-1, +1] against a reference size."""
    if reference <= 0:
        return 0.0
    return _clamp(value / reference)


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    # Sample standard deviation: these returns are a sample of a process,
    # not the whole population.
    var = sum((v - avg) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return _mean(values[-period:])


def realised_volatility(closes: List[float], lookback: int = 20) -> Optional[float]:
    """Annualised realised volatility from daily closes.

    Log returns, sample stdev, scaled by sqrt(252). Returns None rather
    than 0.0 when there is not enough history: zero volatility would read
    as a calm market, which is the opposite of "we do not know".
    """
    if len(closes) < lookback + 1:
        return None
    rets = []
    for prev, cur in zip(closes[-(lookback + 1):-1], closes[-lookback:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    return _stdev(rets) * math.sqrt(252)


def session_vwap(bars) -> Optional[float]:
    """Volume-weighted average price over the supplied bars.

    Returns None when total volume is zero: dividing by zero volume, or
    silently falling back to an unweighted mean, would produce a number
    that looks like a VWAP but is not one.
    """
    total_volume = 0
    total_value = 0.0
    for bar in bars:
        volume = getattr(bar, "volume", 0) or 0
        if volume <= 0:
            continue
        total_value += bar.typical_price * volume
        total_volume += volume
    if total_volume <= 0:
        return None
    return total_value / total_volume


@dataclass
class IndexInput:
    """Normalised market data for one instrument.

    Provider-agnostic on purpose: the engine must not know or care which
    source produced this.
    """
    symbol: str
    price: Optional[float] = None
    session_open: Optional[float] = None
    previous_close: Optional[float] = None
    daily_closes: List[float] = field(default_factory=list)
    intraday_bars: List = field(default_factory=list)
    data_age_seconds: Optional[float] = None
    provider: str = ""
    as_of: Optional[str] = None
    error: Optional[str] = None

    def freshness(self, fresh_max: float, stale_max: float) -> str:
        if self.price is None or self.error:
            return Freshness.MISSING
        if self.data_age_seconds is None:
            return Freshness.FRESH      # age unknown but data present
        if self.data_age_seconds <= fresh_max:
            return Freshness.FRESH
        if self.data_age_seconds <= stale_max:
            return Freshness.STALE
        return Freshness.MISSING        # too old to rely on


@dataclass
class IndexFeatures:
    symbol: str
    freshness: str
    trend_score: Optional[float] = None
    price_vs_sma_short: Optional[float] = None
    sma_spread: Optional[float] = None
    intraday_change_pct: Optional[float] = None
    vwap: Optional[float] = None
    vwap_distance_pct: Optional[float] = None
    above_vwap: Optional[bool] = None
    realised_vol: Optional[float] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.trend_score is not None and self.freshness != Freshness.MISSING

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "freshness": self.freshness,
            "trend_score": _round(self.trend_score),
            "price_vs_sma_short": _round(self.price_vs_sma_short, 5),
            "sma_spread": _round(self.sma_spread, 5),
            "intraday_change_pct": _round(self.intraday_change_pct, 5),
            "vwap": _round(self.vwap, 4),
            "vwap_distance_pct": _round(self.vwap_distance_pct, 5),
            "above_vwap": self.above_vwap,
            "realised_vol": _round(self.realised_vol, 4),
            "reasons": list(self.reasons),
        }


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


@dataclass
class MarketRegimeResult:
    regime: str
    confidence: float
    raw_score: Optional[float]
    risk_posture: str
    trend: str
    risk_mode: str
    volatility: str
    breadth_proxy: str
    evaluated_at: str
    inputs: Dict = field(default_factory=dict)
    features: Dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    weights: Dict = field(default_factory=dict)
    data_quality: Dict = field(default_factory=dict)

    @property
    def primary_reason(self) -> str:
        return self.reasons[0] if self.reasons else ""

    def as_dict(self) -> Dict:
        return {
            "regime": self.regime,
            "confidence": round(self.confidence, 4),
            "raw_score": _round(self.raw_score),
            "risk_posture": self.risk_posture,
            "trend": self.trend,
            "risk_mode": self.risk_mode,
            "volatility": self.volatility,
            "breadth_proxy": self.breadth_proxy,
            "evaluated_at": self.evaluated_at,
            "primary_reason": self.primary_reason,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "features": self.features,
            "inputs": self.inputs,
            "weights": self.weights,
            "data_quality": self.data_quality,
            "confidence_meaning": (
                "agreement among inputs and data quality; NOT a probability "
                "that the market will rise"
            ),
        }


class MarketRegimeEngine:
    """Deterministic regime classification."""

    def __init__(self, config: RegimeConfig = None):
        self.config = config or DEFAULT_CONFIG.regime
        self.config.validate()

    # -- per-index features ----------------------------------------------

    def _features_for(self, data: IndexInput) -> IndexFeatures:
        th = self.config.thresholds
        freshness = data.freshness(th.fresh_max_age, th.stale_max_age)
        feat = IndexFeatures(symbol=data.symbol, freshness=freshness)

        if freshness == Freshness.MISSING:
            feat.reasons.append(
                f"{data.symbol}: unusable ({data.error or 'no price'})"
            )
            return feat

        price = data.price
        components: List[float] = []

        sma_short = _sma(data.daily_closes, self.config.sma_short)
        sma_long = _sma(data.daily_closes, self.config.sma_long)

        if sma_short and sma_short > 0:
            feat.price_vs_sma_short = (price - sma_short) / sma_short
            components.append(_scaled(feat.price_vs_sma_short, _REF_PRICE_VS_SMA))
            feat.reasons.append(
                f"{data.symbol}: price {'above' if feat.price_vs_sma_short > 0 else 'below'} "
                f"SMA{self.config.sma_short} by {abs(feat.price_vs_sma_short) * 100:.2f}%"
            )
        else:
            feat.reasons.append(
                f"{data.symbol}: no SMA{self.config.sma_short} "
                f"({len(data.daily_closes)} daily bars)"
            )

        if sma_short and sma_long and sma_long > 0:
            feat.sma_spread = (sma_short - sma_long) / sma_long
            components.append(_scaled(feat.sma_spread, _REF_SMA_SPREAD))
            feat.reasons.append(
                f"{data.symbol}: SMA{self.config.sma_short} "
                f"{'>' if feat.sma_spread > 0 else '<'} SMA{self.config.sma_long} "
                f"({feat.sma_spread * 100:+.2f}%)"
            )

        # Intraday direction, preferring the session open and falling back
        # to the previous close when the session has not opened yet.
        baseline = data.session_open or data.previous_close
        if baseline and baseline > 0:
            feat.intraday_change_pct = (price - baseline) / baseline
            components.append(_scaled(feat.intraday_change_pct, _REF_INTRADAY))
            feat.reasons.append(
                f"{data.symbol}: {feat.intraday_change_pct * 100:+.2f}% on the session"
            )

        vwap = session_vwap(data.intraday_bars)
        if vwap and vwap > 0:
            feat.vwap = vwap
            feat.vwap_distance_pct = (price - vwap) / vwap
            # A deadband, because sitting *on* VWAP is not being above it.
            # Without this, price == vwap contributes a full +1 to the
            # alignment term and nudges a flat market toward bullish.
            if abs(feat.vwap_distance_pct) < _VWAP_DEADBAND:
                feat.above_vwap = None
                feat.reasons.append(
                    f"{data.symbol}: at VWAP "
                    f"({feat.vwap_distance_pct * 100:+.2f}%, within deadband)"
                )
            else:
                feat.above_vwap = feat.vwap_distance_pct > 0
                feat.reasons.append(
                    f"{data.symbol}: {'above' if feat.above_vwap else 'below'} "
                    f"VWAP ({feat.vwap_distance_pct * 100:+.2f}%)"
                )

        feat.realised_vol = realised_volatility(data.daily_closes)

        if components:
            feat.trend_score = _clamp(_mean(components))
        return feat

    # -- classification ---------------------------------------------------

    def evaluate(self, market_data: Dict[str, IndexInput]) -> MarketRegimeResult:
        th = self.config.thresholds
        weights = self.config.weights
        evaluated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        features = {sym: self._features_for(d) for sym, d in market_data.items()}
        usable = {s: f for s, f in features.items() if f.usable}
        reasons: List[str] = []
        warnings: List[str] = []

        for sym, f in features.items():
            if f.freshness == Freshness.MISSING:
                warnings.append(f"{sym}: data missing or unusable")
                log_event("market_data_missing", symbol=sym)
            elif f.freshness == Freshness.STALE:
                warnings.append(f"{sym}: data stale")
                log_event("market_data_stale", symbol=sym,
                          age_seconds=market_data[sym].data_age_seconds)

        quality = {
            "instruments_requested": len(market_data),
            "instruments_usable": len(usable),
            "fresh": sum(1 for f in features.values() if f.freshness == Freshness.FRESH),
            "stale": sum(1 for f in features.values() if f.freshness == Freshness.STALE),
            "missing": sum(1 for f in features.values() if f.freshness == Freshness.MISSING),
        }

        if len(usable) < th.min_indices_for_regime:
            reasons.insert(0, (
                f"only {len(usable)} of {len(market_data)} instruments usable; "
                f"at least {th.min_indices_for_regime} required"
            ))
            return MarketRegimeResult(
                regime="UNKNOWN", confidence=0.0, raw_score=None,
                risk_posture="NO_NEW_TRADES", trend="UNKNOWN",
                risk_mode="UNKNOWN", volatility="UNKNOWN",
                breadth_proxy="UNKNOWN", evaluated_at=evaluated_at,
                reasons=reasons, warnings=warnings,
                features={s: f.as_dict() for s, f in features.items()},
                inputs=self._describe_inputs(market_data),
                weights=weights.as_dict(), data_quality=quality,
            )

        raw_score, score_parts = self._score(usable, weights)
        dispersion = self._dispersion(usable)
        volatility_label, vol_value = self._volatility(usable, th)
        breadth = self._breadth(usable)
        confidence = self._confidence(features, usable, dispersion, th)

        regime, trend, overrode = self._label(
            raw_score, dispersion, volatility_label, th
        )

        reasons.append(
            f"weighted score {raw_score:+.3f} from {len(usable)} instruments"
        )
        if overrode:
            reasons.insert(0, overrode)
        for f in usable.values():
            reasons.extend(f.reasons)

        risk_mode = self._risk_mode(raw_score, volatility_label, th)
        risk_posture = self._risk_posture(regime, volatility_label, confidence)

        result = MarketRegimeResult(
            regime=regime, confidence=confidence, raw_score=raw_score,
            risk_posture=risk_posture, trend=trend, risk_mode=risk_mode,
            volatility=volatility_label, breadth_proxy=breadth,
            evaluated_at=evaluated_at, reasons=reasons, warnings=warnings,
            features={s: f.as_dict() for s, f in features.items()},
            inputs=self._describe_inputs(market_data),
            weights=weights.as_dict(),
            data_quality={**quality, "dispersion": round(dispersion, 4),
                          "realised_vol": _round(vol_value, 4),
                          "score_parts": score_parts},
        )
        return result

    # -- scoring ----------------------------------------------------------

    def _score(self, usable: Dict[str, IndexFeatures], weights
               ) -> Tuple[float, Dict[str, float]]:
        """Weighted directional score in [-1, +1].

        Weights for instruments that are unusable are redistributed across
        the rest, so a missing index reduces confidence rather than
        silently dragging the score toward zero.
        """
        per_symbol = {
            "SPY": weights.spy_trend,
            "QQQ": weights.qqq_trend,
            "IWM": weights.iwm_trend,
        }
        trend_weight_total = sum(
            w for sym, w in per_symbol.items() if sym in usable
        )
        parts: Dict[str, float] = {}
        trend_component = 0.0

        if trend_weight_total > 0:
            for sym, weight in per_symbol.items():
                f = usable.get(sym)
                if f is None or f.trend_score is None:
                    continue
                share = weight / trend_weight_total
                contribution = f.trend_score * share
                trend_component += contribution
                parts[f"{sym.lower()}_trend"] = round(contribution, 4)
        else:
            # No named index present; average whatever is usable so an
            # alternative instrument set still scores.
            scores = [f.trend_score for f in usable.values()
                      if f.trend_score is not None]
            trend_component = _mean(scores)
            parts["unnamed_trend"] = round(trend_component, 4)

        confirmation = self._confirmation(usable)
        vwap_alignment = self._vwap_alignment(usable)

        trend_weight = (weights.spy_trend + weights.qqq_trend
                        + weights.iwm_trend)
        raw = (trend_component * trend_weight
               + confirmation * weights.cross_index_confirmation
               + vwap_alignment * weights.vwap_alignment)

        parts["cross_index_confirmation"] = round(
            confirmation * weights.cross_index_confirmation, 4)
        parts["vwap_alignment"] = round(
            vwap_alignment * weights.vwap_alignment, 4)

        # The VWAP term contributes only when VWAP was computable, so
        # rescale to keep the score comparable across evaluations.
        active = trend_weight + weights.cross_index_confirmation
        if vwap_alignment is not None and any(
                f.above_vwap is not None for f in usable.values()):
            active += weights.vwap_alignment
        if active > 0:
            raw = raw / active

        return _clamp(raw), parts

    @staticmethod
    def _confirmation(usable: Dict[str, IndexFeatures]) -> float:
        """Net agreement in direction: +1 all up, -1 all down, 0 split."""
        scores = [f.trend_score for f in usable.values()
                  if f.trend_score is not None]
        if not scores:
            return 0.0
        positive = sum(1 for s in scores if s > 0.05)
        negative = sum(1 for s in scores if s < -0.05)
        return (positive - negative) / len(scores)

    @staticmethod
    def _vwap_alignment(usable: Dict[str, IndexFeatures]) -> float:
        flags = [f.above_vwap for f in usable.values() if f.above_vwap is not None]
        if not flags:
            return 0.0
        above = sum(1 for f in flags if f)
        return (above - (len(flags) - above)) / len(flags)

    @staticmethod
    def _dispersion(usable: Dict[str, IndexFeatures]) -> float:
        scores = [f.trend_score for f in usable.values()
                  if f.trend_score is not None]
        if len(scores) < 2:
            return 0.0
        return max(scores) - min(scores)

    @staticmethod
    def _volatility(usable: Dict[str, IndexFeatures], th
                    ) -> Tuple[str, Optional[float]]:
        vols = [f.realised_vol for f in usable.values()
                if f.realised_vol is not None]
        if not vols:
            return "UNKNOWN", None
        vol = _mean(vols)
        if vol >= th.vol_extreme:
            return "EXTREME", vol
        if vol >= th.vol_elevated:
            return "ELEVATED", vol
        return "NORMAL", vol

    @staticmethod
    def _breadth(usable: Dict[str, IndexFeatures]) -> str:
        """Small caps versus large-cap tech, as a crude breadth proxy.

        Not true breadth, which needs advance/decline data. Named
        `breadth_proxy` so it is not mistaken for the real measure.
        """
        qqq = usable.get("QQQ")
        iwm = usable.get("IWM")
        if not qqq or not iwm or qqq.trend_score is None or iwm.trend_score is None:
            scores = [f.trend_score for f in usable.values()
                      if f.trend_score is not None]
            if not scores:
                return "UNKNOWN"
            return "POSITIVE" if _mean(scores) > 0 else "NEGATIVE"

        if iwm.trend_score > 0.15 and qqq.trend_score > 0.15:
            return "BROAD_POSITIVE"
        if iwm.trend_score < -0.15 and qqq.trend_score < -0.15:
            return "BROAD_NEGATIVE"
        if qqq.trend_score - iwm.trend_score > 0.40:
            return "NARROW_LARGE_CAP"
        if iwm.trend_score - qqq.trend_score > 0.40:
            return "SMALL_CAP_LEADERSHIP"
        return "NEUTRAL"

    def _confidence(self, features: Dict[str, IndexFeatures],
                    usable: Dict[str, IndexFeatures], dispersion: float,
                    th) -> float:
        """Agreement and data quality. Explicitly not a probability.

        Three multiplicative factors, because each can independently
        invalidate the classification:
          agreement    - do the instruments point the same way?
          completeness - how many of them could we price?
          freshness    - how current is what we used?
        """
        scores = [f.trend_score for f in usable.values()
                  if f.trend_score is not None]
        if not scores:
            return 0.0

        # Agreement: full dispersion (2.0 across [-1,1]) means none.
        agreement = _clamp(1.0 - (dispersion / 2.0), 0.0, 1.0)

        completeness = len(usable) / max(len(features), 1)

        stale = sum(1 for f in usable.values() if f.freshness == Freshness.STALE)
        freshness = 1.0
        if stale:
            share = stale / len(usable)
            freshness = 1.0 - share * (1.0 - th.stale_confidence_multiplier)

        confidence = agreement * completeness * freshness
        return round(_clamp(confidence, 0.0, 1.0), 4)

    def _label(self, raw_score: float, dispersion: float,
               volatility: str, th) -> Tuple[str, str, Optional[str]]:
        """Map the score to a regime, applying overrides."""
        if raw_score >= th.strong_bullish:
            regime, trend = "STRONG_BULLISH", "UP"
        elif raw_score >= th.bullish:
            regime, trend = "BULLISH", "UP"
        elif raw_score > th.bearish:
            regime, trend = "NEUTRAL", "FLAT"
        elif raw_score > th.strong_bearish:
            regime, trend = "BEARISH", "DOWN"
        else:
            regime, trend = "STRONG_BEARISH", "DOWN"

        override = None
        # Extreme volatility describes the environment more usefully than
        # a direction does, so it takes the label.
        if volatility == "EXTREME":
            override = (
                f"realised volatility is EXTREME; the environment is "
                f"classified VOLATILE rather than {regime}"
            )
            regime = "VOLATILE"
        elif dispersion >= th.mixed_dispersion and regime in ("NEUTRAL", "BULLISH",
                                                              "BEARISH"):
            override = (
                f"instruments disagree sharply (dispersion {dispersion:.2f}); "
                f"classified MIXED rather than {regime}"
            )
            regime = "MIXED"

        return regime, trend, override

    @staticmethod
    def _risk_mode(raw_score: float, volatility: str, th) -> str:
        if volatility == "EXTREME":
            return "RISK_OFF"
        if raw_score >= th.bullish:
            return "RISK_ON" if volatility != "ELEVATED" else "NEUTRAL"
        if raw_score <= th.bearish:
            return "RISK_OFF"
        return "NEUTRAL"

    @staticmethod
    def _risk_posture(regime: str, volatility: str, confidence: float) -> str:
        """A recommendation for the future Risk Governor, not a decision.

        The Risk Governor decides what any of this means operationally;
        this only reports how hostile the environment looks.
        """
        if regime == "UNKNOWN":
            return "NO_NEW_TRADES"
        if regime == "STRONG_BEARISH" or volatility == "EXTREME":
            return "RESTRICTED"
        if regime in ("BEARISH", "MIXED", "VOLATILE"):
            return "CAUTIOUS"
        if confidence < 0.35:
            return "CAUTIOUS"
        return "NORMAL"

    @staticmethod
    def _describe_inputs(market_data: Dict[str, IndexInput]) -> Dict:
        return {
            sym: {
                "price": _round(d.price, 4),
                "session_open": _round(d.session_open, 4),
                "previous_close": _round(d.previous_close, 4),
                "daily_bars": len(d.daily_closes),
                "intraday_bars": len(d.intraday_bars),
                "provider": d.provider,
                "as_of": d.as_of,
                "age_seconds": (None if d.data_age_seconds is None
                                else round(d.data_age_seconds, 1)),
                "error": d.error,
            }
            for sym, d in market_data.items()
        }
