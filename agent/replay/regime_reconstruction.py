"""Reconstructing the market regime as it was, at each replay timestamp.

WHY THIS EXISTS

Historical replay could only trade under an injected permissive regime
- BULLISH, confidence 0.8, posture NORMAL, on every bar. That removes
the long-only regime gate entirely, so any number it produced described
the strategy WITHOUT its market filter. Useful as a counterfactual,
useless as a statement about the deployed strategy.

THE SAME ENGINE, NOT A SECOND MODEL

This calls `agent.market.regime.MarketRegimeEngine.evaluate`, the one
the live cycle uses. A reimplementation here would be a second model
that agrees with the first until it doesn't, and the disagreement would
be invisible: both would produce plausible regimes.

AS-OF, BY CONSTRUCTION

Benchmark series are sliced to `timestamp <= T` once, and the engine
receives only the slice. It cannot read forward because it is never
handed anything forward.

NO SILENT FALLBACK

When the regime cannot be established the answer is INSUFFICIENT_DATA
or UNKNOWN, never a permissive default. If the live system would refuse
a candidate because the regime is unknown, the faithful replay refuses
it too - which is the entire point of calling it faithful.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..market.regime import IndexInput, MarketRegimeEngine

# The benchmarks the deployed engine weights. Taken from its own
# weighting table rather than chosen here, so a change there surfaces as
# a missing input rather than as a quietly different regime.
BENCHMARKS = ("SPY", "QQQ", "IWM")

# Availability states. Explicit, because "we could not establish the
# regime" and "the regime was neutral" lead to opposite decisions.
OBSERVED_RECONSTRUCTED = "OBSERVED_RECONSTRUCTED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
UNKNOWN = "UNKNOWN"

# The engine needs history before a trend means anything. Below this a
# regime is arithmetic over too few points, and saying INSUFFICIENT_DATA
# is more useful than saying NEUTRAL with low confidence.
MIN_DAILY_CLOSES = 50

# At least this many benchmarks must be readable. Two of three allows
# one index to be halted or missing a bar; one of three is not a
# breadth measure at all.
MIN_BENCHMARKS = 2

RECONSTRUCTION_VERSION = "regime-reconstruction-v1.0.0"


@dataclass(frozen=True)
class ReconstructedRegime:
    """One regime, and how much of it could be established."""
    availability: str
    timestamp: str
    regime: Optional[str] = None
    confidence: Optional[float] = None
    risk_posture: Optional[str] = None
    market_session: Optional[str] = None
    benchmarks_used: tuple = ()
    benchmarks_missing: tuple = ()
    detail: Optional[str] = None
    reasons: tuple = ()

    @property
    def established(self) -> bool:
        return self.availability == OBSERVED_RECONSTRUCTED

    def as_regime_dict(self) -> Dict:
        """The shape the replay engine's `regime_for` must return.

        When the regime is NOT established this returns the unknown
        state rather than omitting keys: the hypothesis engine treats a
        missing regime and an UNKNOWN regime differently, and the
        unknown one is what the live system would see.
        """
        if not self.established:
            return {
                "regime": "UNKNOWN",
                "regime_confidence": 0.0,
                # NO_NEW_TRADES, not NORMAL. An unestablished regime
                # must refuse exactly as the live system refuses.
                "risk_posture": "NO_NEW_TRADES",
                "market_session": self.market_session or "UNKNOWN",
                "availability": self.availability,
                "detail": self.detail,
            }
        return {
            "regime": self.regime,
            "regime_confidence": self.confidence,
            "risk_posture": self.risk_posture,
            "market_session": self.market_session or "OPEN",
            "availability": self.availability,
        }

    def to_dict(self) -> Dict:
        return {
            "availability": self.availability,
            "timestamp": self.timestamp,
            "regime": self.regime,
            "confidence": self.confidence,
            "risk_posture": self.risk_posture,
            "market_session": self.market_session,
            "benchmarks_used": list(self.benchmarks_used),
            "benchmarks_missing": list(self.benchmarks_missing),
            "established": self.established,
            "detail": self.detail,
            "reasons": list(self.reasons),
            "version": RECONSTRUCTION_VERSION,
        }


def _timestamp(bar: Any) -> Optional[str]:
    raw = bar.get("timestamp") if isinstance(bar, dict) else getattr(
        bar, "timestamp", None)
    if raw is None:
        return None
    return raw.isoformat() if hasattr(raw, "isoformat") else str(raw)


def _close(bar: Any) -> Optional[float]:
    raw = bar.get("close") if isinstance(bar, dict) else getattr(
        bar, "close", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _bars_up_to(bars: Sequence[Any], timestamp: str) -> List[Any]:
    """Every bar at or before `timestamp`. The as-of slice.

    Inclusive of T: a decision taken after bar T completes may use bar
    T's close. Exclusive would discard the most recent information the
    live system genuinely had.
    """
    out = []
    for bar in bars:
        ts = _timestamp(bar)
        if ts is None or ts > timestamp:
            continue
        out.append(bar)
    return out


def build_index_input(symbol: str, bars: Sequence[Any],
                      timestamp: str) -> Optional[IndexInput]:
    """An IndexInput for one benchmark, as of `timestamp`.

    None when the benchmark has no usable bar at or before T - a
    holiday, a halt, or a series that starts later than the others.
    The caller counts it as missing rather than substituting a price.
    """
    history = _bars_up_to(bars, timestamp)
    if not history:
        return None
    closes = [_close(b) for b in history]
    if any(c is None for c in closes):
        # A hole makes every average over the window wrong by an
        # unknown amount. Refusing the whole benchmark is honest;
        # dropping the bad bars would silently shorten the window.
        return None
    latest = history[-1]
    price = closes[-1]
    previous_close = closes[-2] if len(closes) > 1 else None
    return IndexInput(
        symbol=symbol,
        price=price,
        # The session's open is not reconstructible from daily bars
        # alone, and guessing it from the previous close would feed the
        # engine a fabricated gap. Left as None so the engine's own
        # handling of a missing open applies.
        session_open=None,
        previous_close=previous_close,
        daily_closes=closes,
        # EMPTY LIST, not None. IndexInput declares
        # `intraday_bars: List = field(default_factory=list)` - it is
        # not Optional - and session_vwap iterates it. Passing None
        # made the engine raise on every bar, which the reconstruction
        # correctly reported as UNKNOWN; the fault was here, not in the
        # engine. An empty list is also the accurate value: a daily-bar
        # replay has no intraday bars, and session_vwap returns None
        # for zero volume rather than inventing an unweighted mean.
        intraday_bars=[],
        # Zero, not None: within a replay the bar AT T is exactly as of
        # T. Staleness between bars is the replay engine's measurement,
        # not this function's.
        data_age_seconds=0.0,
        provider="replay-reconstruction",
        as_of=_timestamp(latest),
    )


def reconstruct(benchmark_bars: Dict[str, Sequence[Any]], timestamp: str, *,
                engine: Optional[MarketRegimeEngine] = None,
                min_daily_closes: int = MIN_DAILY_CLOSES,
                min_benchmarks: int = MIN_BENCHMARKS,
                market_session: str = "OPEN") -> ReconstructedRegime:
    """The regime at `timestamp`, from information available by then.

    Returns INSUFFICIENT_DATA rather than a regime when too few
    benchmarks are readable or the history is too short, and UNKNOWN
    when the engine itself could not decide. Neither is ever converted
    into a permissive default.
    """
    used: List[str] = []
    missing: List[str] = []
    inputs: Dict[str, IndexInput] = {}
    short: List[str] = []

    for symbol in BENCHMARKS:
        bars = benchmark_bars.get(symbol)
        if not bars:
            missing.append(symbol)
            continue
        index_input = build_index_input(symbol, bars, timestamp)
        if index_input is None:
            missing.append(symbol)
            continue
        if len(index_input.daily_closes or []) < min_daily_closes:
            # Present but too short. Counted separately from missing,
            # because "no data" and "not enough data yet" are different
            # problems and only one of them resolves with time.
            short.append(symbol)
            missing.append(symbol)
            continue
        inputs[symbol] = index_input
        used.append(symbol)

    if len(used) < min_benchmarks:
        detail = (f"only {len(used)} of {len(BENCHMARKS)} benchmark(s) "
                  f"usable at {timestamp}; {min_benchmarks} required")
        if short:
            detail += (f". Too little history for {', '.join(short)} "
                       f"(needs {min_daily_closes} daily closes)")
        return ReconstructedRegime(
            availability=INSUFFICIENT_DATA, timestamp=timestamp,
            benchmarks_used=tuple(used),
            benchmarks_missing=tuple(missing),
            market_session=market_session, detail=detail)

    engine = engine or MarketRegimeEngine()
    try:
        result = engine.evaluate(inputs)
    except Exception as exc:                                  # noqa: BLE001
        # An engine failure is UNKNOWN, not neutral. Reporting a regime
        # the engine did not produce would be inventing one.
        return ReconstructedRegime(
            availability=UNKNOWN, timestamp=timestamp,
            benchmarks_used=tuple(used),
            benchmarks_missing=tuple(missing),
            market_session=market_session,
            detail=f"the regime engine raised {type(exc).__name__}: "
                   f"{str(exc)[:160]}")

    regime = getattr(result, "regime", None)
    if regime is None or str(regime).upper() == "UNKNOWN":
        return ReconstructedRegime(
            availability=UNKNOWN, timestamp=timestamp,
            regime="UNKNOWN",
            confidence=getattr(result, "confidence", None),
            risk_posture=getattr(result, "risk_posture", None),
            benchmarks_used=tuple(used),
            benchmarks_missing=tuple(missing),
            market_session=market_session,
            detail="the engine ran and could not determine a regime",
            reasons=tuple(getattr(result, "reasons", None) or ()))

    return ReconstructedRegime(
        availability=OBSERVED_RECONSTRUCTED, timestamp=timestamp,
        regime=str(regime),
        confidence=getattr(result, "confidence", None),
        risk_posture=str(getattr(result, "risk_posture", "")) or None,
        benchmarks_used=tuple(used),
        benchmarks_missing=tuple(missing),
        market_session=market_session,
        reasons=tuple(getattr(result, "reasons", None) or ()))


def regime_for(benchmark_bars: Dict[str, Sequence[Any]], *,
               engine: Optional[MarketRegimeEngine] = None,
               min_daily_closes: int = MIN_DAILY_CLOSES,
               min_benchmarks: int = MIN_BENCHMARKS,
               record: Optional[List[Dict]] = None):
    """A `regime_for(symbol, clock)` callable for the replay engine.

    The engine is cached across calls because constructing it per bar
    would be wasteful, and the regime is recomputed per TIMESTAMP - not
    per symbol - because the market regime is a property of the market,
    and computing it per candidate would both waste work and invite a
    per-symbol regime, which the live system does not have.

    `record` collects every reconstruction so a run can report how many
    bars were INSUFFICIENT_DATA rather than silently trading only the
    ones that worked.
    """
    engine = engine or MarketRegimeEngine()
    cache: Dict[str, ReconstructedRegime] = {}

    def _regime_for(symbol, clock):                           # noqa: ARG001
        timestamp = getattr(clock, "timestamp", None)
        if callable(timestamp):
            timestamp = timestamp()
        if timestamp is None:
            timestamp = getattr(clock, "now", None)
            if callable(timestamp):
                timestamp = timestamp()
        timestamp = str(timestamp) if timestamp is not None else ""
        if timestamp not in cache:
            reconstructed = reconstruct(
                benchmark_bars, timestamp, engine=engine,
                min_daily_closes=min_daily_closes,
                min_benchmarks=min_benchmarks)
            cache[timestamp] = reconstructed
            if record is not None:
                record.append(reconstructed.to_dict())
        return cache[timestamp].as_regime_dict()

    return _regime_for
