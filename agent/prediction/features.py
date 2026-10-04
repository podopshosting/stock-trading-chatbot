"""Feature records: what was knowable at one instant, and nothing else.

Every defect that matters in a prediction system is in this file or in
`targets.py`, because between them they decide where "now" ends. A
feature computed from a bar the strategy had not seen is not a feature,
it is the answer.

THE AS-OF RULE

A FeatureRecord is built from bars `[0 .. index]` inclusive and nothing
beyond. `index` is the DECISION bar: its open, high, low and close are
all known, because the decision is taken after the bar completes. The
bar at `index + 1` is the future and is unreachable from here - it is
where the fill happens, which is `targets.py`'s business.

The separation is enforced by construction: `build()` slices the series
once and the feature functions receive only the slice. They cannot
reach forward because they are never given anything forward to reach.

IMMUTABILITY

A FeatureRecord is frozen and carries `feature_hash` over its own
values. A feature vector that can be edited after a prediction was made
from it is not evidence of anything: the prediction's inputs would be
whatever they were last set to.

UNKNOWN IS NOT ZERO

A feature that cannot be computed - too little history, a missing bar,
a provider that was not asked - is None. Never 0.0. A zero RSI and an
absent RSI are different claims, and a model trained on the conflation
learns that the start of a series is oversold.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

FEATURE_SCHEMA_VERSION = "features-v1.0.0"

# How much history each feature needs before it means anything. A
# shorter window produces a number; it does not produce the feature.
MIN_BARS_FOR_RSI = 15
MIN_BARS_FOR_MACD = 26
MIN_BARS_FOR_BOLLINGER = 20
MIN_BARS_FOR_LONG_MA = 50
MIN_BARS_FOR_RELATIVE_VOLUME = 20

# Regime provenance, mirroring the replay engine's vocabulary. Anything
# not OBSERVED is synthetic, and the default says so.
REGIME_OBSERVED = "OBSERVED"
REGIME_RECONSTRUCTED = "RECONSTRUCTED"
REGIME_SYNTHETIC = "SYNTHETIC"
REGIME_NONE = "NONE"


def _get(bar: Any, field_name: str) -> Optional[float]:
    """One price off a bar, tolerating object or mapping, inventing none.

    A non-positive price is corrupt input rather than a market event, so
    it returns None instead of propagating into a division.
    """
    raw = bar.get(field_name) if isinstance(bar, dict) else getattr(
        bar, field_name, None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if field_name == "volume":
        # Zero volume is a real observation (a halted or untraded bar),
        # so it is kept. Negative volume is not.
        return value if value >= 0 else None
    return value if value > 0 else None


def _timestamp(bar: Any) -> Optional[str]:
    raw = bar.get("timestamp") if isinstance(bar, dict) else getattr(
        bar, "timestamp", None)
    if raw is None:
        return None
    return raw.isoformat() if hasattr(raw, "isoformat") else str(raw)


def _closes(bars: Sequence[Any]) -> Optional[List[float]]:
    """Every close, or None if ANY is unreadable.

    Not "the ones that parsed". A gap in the middle of a window makes
    every indicator over that window wrong by an unknown amount, and a
    silently shortened window answers a different question.
    """
    out = []
    for bar in bars:
        close = _get(bar, "close")
        if close is None:
            return None
        out.append(close)
    return out


# ---------------------------------------------------------------- maths
def _sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def _ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def _rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(len(closes) - period, len(closes)):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        # Every move up. 100 is the correct RSI, not a division error,
        # and not None: it is a real and informative extreme.
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100.0 - (100.0 / (1.0 + rs)), 6)


def _macd(closes: Sequence[float]) -> Dict[str, Optional[float]]:
    if len(closes) < MIN_BARS_FOR_MACD:
        return {"macd": None, "macd_signal": None, "macd_histogram": None}
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    line = [f - s for f, s in zip(fast, slow)]
    signal = _ema_series(line, 9)
    if not signal:
        return {"macd": None, "macd_signal": None, "macd_histogram": None}
    return {"macd": round(line[-1], 6),
            "macd_signal": round(signal[-1], 6),
            "macd_histogram": round(line[-1] - signal[-1], 6)}


def _stdev(values: Sequence[float]) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def _volatility_pct(closes: Sequence[float], period: int = 20
                    ) -> Optional[float]:
    """Standard deviation of per-bar returns, as a percentage."""
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    returns = [(window[i] - window[i - 1]) / window[i - 1] * 100.0
               for i in range(1, len(window))]
    sd = _stdev(returns)
    return None if sd is None else round(sd, 6)


def _bollinger_position(closes: Sequence[float], period: int = 20,
                        sigma: float = 2.0) -> Optional[float]:
    """Where price sits in the band: 0 at the lower, 1 at the upper.

    Values outside [0, 1] are real and kept - price does trade outside
    its bands, and clamping would erase the signal.
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    sd = _stdev(window)
    if sd is None or sd == 0:
        # A flat window has no band. 0.5 would be a fabricated midpoint.
        return None
    lower = mid - sigma * sd
    upper = mid + sigma * sd
    return round((closes[-1] - lower) / (upper - lower), 6)


def _vwap_distance_pct(bars: Sequence[Any]) -> Optional[float]:
    """Distance from the session's volume-weighted average price.

    Typical price per bar, weighted by volume. Returns None when total
    volume is zero: an unweighted average would be a different measure
    wearing VWAP's name.
    """
    total_pv, total_v, last_close = 0.0, 0.0, None
    for bar in bars:
        high, low, close = (_get(bar, "high"), _get(bar, "low"),
                            _get(bar, "close"))
        volume = _get(bar, "volume")
        if None in (high, low, close) or volume is None:
            return None
        typical = (high + low + close) / 3.0
        total_pv += typical * volume
        total_v += volume
        last_close = close
    if total_v <= 0 or last_close is None:
        return None
    vwap = total_pv / total_v
    if vwap <= 0:
        return None
    return round((last_close - vwap) / vwap * 100.0, 6)


def _relative_volume(bars: Sequence[Any], period: int = 20
                     ) -> Optional[float]:
    """This bar's volume against the average of the preceding `period`.

    Excludes the current bar from its own baseline: including it damps
    exactly the spike the feature exists to detect.
    """
    if len(bars) < period + 1:
        return None
    volumes = [_get(b, "volume") for b in bars[-(period + 1):]]
    if any(v is None for v in volumes):
        return None
    current, history = volumes[-1], volumes[:-1]
    average = sum(history) / len(history)
    if average <= 0:
        return None
    return round(current / average, 6)


def _minutes_since_midnight(ts: Optional[str]) -> Optional[int]:
    """Time of day, from the bar's own timestamp.

    From the TIMESTAMP, never from the clock. Reading the wall clock
    here would make a feature's value depend on when the record was
    built rather than on when the bar happened, so replaying history
    would produce different features every time it ran.
    """
    if not ts:
        return None
    try:
        time_part = ts.split("T")[1] if "T" in ts else None
        if not time_part:
            return None
        hh, mm = time_part[:2], time_part[3:5]
        return int(hh) * 60 + int(mm)
    except (ValueError, IndexError):
        return None


@dataclass(frozen=True)
class FeatureRecord:
    """One immutable observation of what was knowable at `feature_time`.

    Frozen, and hashed over its own values. A feature vector that can be
    edited after a prediction was made from it is not evidence: the
    prediction's inputs would be whatever they were last set to.
    """
    prediction_feature_id: str
    symbol: str
    feature_time: str
    bar_index: int
    dataset_id: Optional[str]
    dataset_checksum: Optional[str]
    feature_schema_version: str
    regime_source: str
    regime_state: Optional[str]
    market_session: Optional[str]
    values: Dict[str, Any] = field(default_factory=dict)
    unavailable: List[str] = field(default_factory=list)
    feature_hash: str = ""

    @property
    def regime_is_synthetic(self) -> bool:
        """True unless the regime was genuinely observed or reconstructed.

        Derived to be true by default, so a source added later is
        assumed manufactured until someone states otherwise. That is the
        direction of error that cannot overstate a result.
        """
        return self.regime_source not in (REGIME_OBSERVED,
                                          REGIME_RECONSTRUCTED)

    @property
    def complete(self) -> bool:
        return not self.unavailable

    def to_dict(self) -> Dict:
        return {
            "prediction_feature_id": self.prediction_feature_id,
            "symbol": self.symbol,
            "feature_time": self.feature_time,
            "bar_index": self.bar_index,
            "dataset_id": self.dataset_id,
            "dataset_checksum": self.dataset_checksum,
            "feature_schema_version": self.feature_schema_version,
            "regime_source": self.regime_source,
            "regime_state": self.regime_state,
            "regime_is_synthetic": self.regime_is_synthetic,
            "market_session": self.market_session,
            "values": dict(self.values),
            "unavailable": list(self.unavailable),
            "complete": self.complete,
            "feature_hash": self.feature_hash,
        }


def compute_feature_hash(symbol: str, feature_time: str,
                         schema_version: str,
                         values: Dict[str, Any]) -> str:
    """A hash over the values, for drift detection.

    Keyed by symbol, time and schema as well as the values, so two
    different symbols with coincidentally identical readings do not
    collide into one identity.

    Sorted keys and a fixed float repr, because a hash that depends on
    dict ordering would differ between runs of identical code.
    """
    payload = {
        "symbol": symbol,
        "feature_time": feature_time,
        "schema": schema_version,
        "values": {k: (repr(v) if isinstance(v, float) else v)
                   for k, v in sorted(values.items())},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def build(bars: Sequence[Any], index: int, symbol: str, *,
          dataset_id: Optional[str] = None,
          dataset_checksum: Optional[str] = None,
          regime_source: str = REGIME_NONE,
          regime_state: Optional[str] = None,
          market_session: Optional[str] = None,
          signal_direction: Optional[str] = None,
          signal_agreement: Optional[float] = None,
          signal_magnitude: Optional[float] = None,
          spread_pct: Optional[float] = None,
          evidence_materiality: Optional[float] = None,
          evidence_novelty: Optional[float] = None,
          days_to_earnings: Optional[int] = None,
          days_to_corporate_action: Optional[int] = None,
          ) -> Optional[FeatureRecord]:
    """A feature record for the decision at `bars[index]`.

    THE WHOLE POINT: the series is sliced ONCE here, to
    `bars[:index + 1]`, and every feature function receives only that
    slice. They cannot read the future because they are never handed it.

    Returns None when `index` is out of range or the decision bar itself
    is unreadable - there is no feature vector for a bar that does not
    exist, and an empty one would be mistaken for a quiet market.

    Caller-supplied values (signals, evidence, spread, event proximity)
    must already be as-of `feature_time`. This function cannot verify
    that, so it records `regime_source` and leaves the claim attributable
    rather than silently trusting it.
    """
    if index < 0 or index >= len(bars):
        return None

    history = list(bars[:index + 1])          # inclusive; nothing beyond
    decision_bar = history[-1]
    feature_time = _timestamp(decision_bar)
    close = _get(decision_bar, "close")
    if close is None:
        return None
    if feature_time is None:
        # Without a timestamp the record cannot be ordered, matured, or
        # compared with a target. A feature with no "as of" is not a
        # feature.
        return None

    closes = _closes(history)
    values: Dict[str, Any] = {}
    unavailable: List[str] = []

    def put(name: str, value: Any, needed: Optional[int] = None) -> None:
        if value is None:
            unavailable.append(
                f"{name} (needs {needed} bars, have {len(history)})"
                if needed else name)
        values[name] = value

    put("price", close)
    put("volume", _get(decision_bar, "volume"))
    put("dollar_volume",
        None if _get(decision_bar, "volume") is None
        else round(close * _get(decision_bar, "volume"), 4))

    # Returns over several lookbacks. Each needs its own history and
    # says so rather than returning 0.0 for "not enough data".
    for lookback in (1, 5, 10, 20):
        name = f"return_{lookback}b_pct"
        if closes is None or len(closes) < lookback + 1:
            put(name, None, lookback + 1)
        else:
            prior = closes[-(lookback + 1)]
            put(name, round((close - prior) / prior * 100.0, 6)
                if prior > 0 else None)

    put("volatility_20b_pct",
        None if closes is None else _volatility_pct(closes, 20), 21)
    put("rsi_14",
        None if closes is None else _rsi(closes, 14), MIN_BARS_FOR_RSI)

    macd = _macd(closes or [])
    for name, value in macd.items():
        put(name, value, MIN_BARS_FOR_MACD)

    # Moving averages, and price's position relative to them. The
    # RELATIONSHIP is the feature; the raw average is kept because a
    # model may want the level too.
    for period in (10, 20, 50):
        sma = None if closes is None else _sma(closes, period)
        put(f"sma_{period}", None if sma is None else round(sma, 6), period)
        put(f"price_vs_sma_{period}_pct",
            None if sma is None or sma <= 0
            else round((close - sma) / sma * 100.0, 6), period)
    fast = None if closes is None else _sma(closes, 10)
    slow = None if closes is None else _sma(closes, 50)
    put("sma_10_vs_50_pct",
        None if fast is None or slow is None or slow <= 0
        else round((fast - slow) / slow * 100.0, 6), MIN_BARS_FOR_LONG_MA)

    put("bollinger_position",
        None if closes is None else _bollinger_position(closes),
        MIN_BARS_FOR_BOLLINGER)
    put("vwap_distance_pct", _vwap_distance_pct(history))
    put("relative_volume_20b", _relative_volume(history, 20),
        MIN_BARS_FOR_RELATIVE_VOLUME)
    put("minutes_since_midnight", _minutes_since_midnight(feature_time))
    put("bars_of_history", len(history))

    # Caller-supplied, already as-of feature_time.
    put("signal_direction", signal_direction)
    put("signal_agreement", signal_agreement)
    put("signal_magnitude", signal_magnitude)
    put("spread_pct", spread_pct)
    put("evidence_materiality", evidence_materiality)
    put("evidence_novelty", evidence_novelty)
    put("days_to_earnings", days_to_earnings)
    put("days_to_corporate_action", days_to_corporate_action)

    feature_hash = compute_feature_hash(
        symbol, feature_time, FEATURE_SCHEMA_VERSION, values)
    return FeatureRecord(
        prediction_feature_id=f"feat_{feature_hash[:16]}",
        symbol=symbol,
        feature_time=feature_time,
        bar_index=index,
        dataset_id=dataset_id,
        dataset_checksum=dataset_checksum,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        regime_source=regime_source,
        regime_state=regime_state,
        market_session=market_session,
        values=values,
        unavailable=unavailable,
        feature_hash=feature_hash,
    )


# The names a model may train on. Explicit, because a model that
# iterates whatever happens to be in `values` would silently start
# training on a new feature the day someone adds one - and the
# comparison with every earlier result would quietly stop being valid.
NUMERIC_FEATURE_NAMES = (
    "return_1b_pct", "return_5b_pct", "return_10b_pct", "return_20b_pct",
    "volatility_20b_pct", "rsi_14", "macd", "macd_signal",
    "macd_histogram", "price_vs_sma_10_pct", "price_vs_sma_20_pct",
    "price_vs_sma_50_pct", "sma_10_vs_50_pct", "bollinger_position",
    "vwap_distance_pct", "relative_volume_20b",
)


def numeric_vector(record: FeatureRecord,
                   names: Sequence[str] = NUMERIC_FEATURE_NAMES
                   ) -> Optional[List[float]]:
    """A dense vector, or None if ANY named feature is unavailable.

    None rather than a zero-filled vector. Imputing zero for a missing
    RSI teaches a model that early bars are oversold; dropping the row
    is honest and the caller counts how many it dropped.
    """
    out = []
    for name in names:
        value = record.values.get(name)
        if value is None or not isinstance(value, (int, float)):
            return None
        out.append(float(value))
    return out
