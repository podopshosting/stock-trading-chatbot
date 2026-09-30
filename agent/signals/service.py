"""
Signal engine orchestration.

Turns scanner candidates into quantitative signal results: fetch the
history, run the canonical engine, persist, emit events.

Deliberately bounded. Deep signal analysis costs one bars request per
symbol, so it runs on the scanner's top N rather than the whole
universe - the scanner exists precisely to decide what is worth this
much work. N is configurable and the provider cost is tracked
separately from the scanner's own, because a single "provider_calls"
number that mixed the two would make neither controllable.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from ..observability import log_event
from . import engine
from .models import (
    DataFreshness, QuantitativeSignalResult, SignalDirection, SignalRun,
    utcnow,
)

# How many daily closes the engine wants. 220 comfortably covers SMA200
# with headroom for the leading window the EMAs consume.
HISTORY_BARS = 220

# Default cap on how many scanner candidates get deep analysis.
DEFAULT_TOP_N = 10


class SignalService:
    """Runs the canonical engine over a set of symbols."""

    def __init__(self, provider, store=None, config=None,
                 top_n: int = DEFAULT_TOP_N,
                 clock=None):
        self.provider = provider
        self.store = store
        self.config = config
        self.top_n = top_n
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).date().isoformat())
        self._provider_calls = 0

    # --- data ------------------------------------------------------------

    def _freshness(self, quote) -> DataFreshness:
        """Freshness from the quote's own provenance, never assumed.

        A missing timestamp is UNKNOWN rather than FRESH: treating an
        un-dated quote as current is how stale data gets presented as a
        live reading.
        """
        provenance = getattr(quote, "provenance", None)
        retrieved = getattr(provenance, "retrieved_at", None)
        if retrieved is None:
            return DataFreshness.UNKNOWN
        age = max(0.0, time.time() - retrieved)
        if age <= 300:
            return DataFreshness.FRESH
        if age <= 1800:
            return DataFreshness.STALE
        return DataFreshness.MISSING

    def _data_quality(self, quote, closes: Sequence[float]) -> Dict:
        provenance = getattr(quote, "provenance", None)
        retrieved = getattr(provenance, "retrieved_at", None)
        age = None if retrieved is None else max(0.0, time.time() - retrieved)
        return {
            "price_source": getattr(provenance, "provider", "unknown"),
            "price_as_of": getattr(provenance, "as_of", None),
            "age_seconds": None if age is None else round(age, 1),
            "freshness": str(self._freshness(quote)),
            "is_delayed": getattr(provenance, "is_delayed", None),
            "feed_note": getattr(provenance, "note", ""),
            "history_bars": len(closes),
            "sources": {
                "price_and_bars": getattr(provenance, "provider", "unknown"),
                "fundamentals": "not used in this analysis",
            },
        }

    # --- one symbol ------------------------------------------------------

    def evaluate_symbol(self, symbol: str, market_regime: str = "UNKNOWN",
                        regime_confidence: float = 0.0
                        ) -> QuantitativeSignalResult:
        """Evaluate one symbol. Provider failures become a stated absence
        rather than an exception, so one bad symbol cannot abort a run."""
        symbol = symbol.upper()
        try:
            quote = self.provider.get_quote(symbol)
            self._provider_calls += 1
            bars = self.provider.get_bars(symbol, "1day", limit=HISTORY_BARS)
            self._provider_calls += 1
            closes = bars.closes()
        except Exception as exc:                       # provider-side failure
            log_event("indicator_error", symbol=symbol,
                      error=type(exc).__name__)
            result = QuantitativeSignalResult(
                symbol=symbol, market_regime=market_regime,
                regime_confidence=regime_confidence,
            )
            result.warnings.append(
                f"market data unavailable ({type(exc).__name__})")
            result.reasons.append(
                "No reading: market data could not be retrieved.")
            return result

        result = engine.evaluate(
            symbol, closes, self._freshness(quote), market_regime,
            regime_confidence, self._data_quality(quote, closes),
            price=getattr(quote, "price", None),
        )

        for sig in result.indicator_results:
            if sig.direction is SignalDirection.NO_SIGNAL:
                log_event("indicator_no_signal", symbol=symbol,
                          indicator=sig.indicator, reason=sig.reason)
        for group in result.group_results:
            if group.internal_disagreement:
                log_event("group_disagreement", symbol=symbol,
                          group=str(group.group),
                          buy_votes=group.buy_votes,
                          sell_votes=group.sell_votes)
        if result.regime_adjustment != 1.0:
            log_event("regime_adjustment_applied", symbol=symbol,
                      regime=result.market_regime,
                      adjustment=result.regime_adjustment,
                      raw_magnitude=round(result.signal_magnitude, 4),
                      adjusted_magnitude=round(
                          result.regime_adjusted_magnitude, 4))
        return result

    # --- a batch ---------------------------------------------------------

    def run(self, symbols: Sequence[str], market_regime: str = "UNKNOWN",
            regime_confidence: float = 0.0, market_session: str = "UNKNOWN",
            scanner_run_id: Optional[str] = None,
            session_date: Optional[str] = None,
            previous: Optional[Dict[str, str]] = None) -> SignalRun:
        """Evaluate a batch, persist it, and return the run.

        `previous` maps symbol -> last known direction, used only to emit
        a change event. It never influences the reading itself: a signal
        that depended on its own previous value would be a filter, not a
        measurement.
        """
        started = time.time()
        session_date = session_date or self._clock()
        started_at = utcnow()
        self._provider_calls = 0

        requested = [s.upper() for s in symbols][:self.top_n]
        run = SignalRun(
            signal_run_id=SignalRun.make_id(session_date, started_at),
            session_date=session_date, started_at=started_at,
            scanner_run_id=scanner_run_id, market_regime=market_regime,
            regime_confidence=regime_confidence, market_session=market_session,
            requested_count=len(requested),
        )

        log_event("signal_engine_started", signal_run_id=run.signal_run_id,
                  scanner_run_id=scanner_run_id, symbols=len(requested),
                  regime=market_regime, session=market_session)

        try:
            for symbol in requested:
                result = self.evaluate_symbol(symbol, market_regime,
                                              regime_confidence)
                run.results.append(result)
                if result.analysis_available:
                    run.evaluated_count += 1
                    run.direction_counts[str(result.direction)] = \
                        run.direction_counts.get(str(result.direction), 0) + 1
                elif result.warnings and any("unavailable" in w
                                             for w in result.warnings):
                    run.error_count += 1
                else:
                    run.skipped_count += 1

                if previous:
                    was = previous.get(symbol)
                    now = str(result.direction)
                    if was and was != now:
                        log_event("signal_direction_changed", symbol=symbol,
                                  previous=was, current=now,
                                  signal_run_id=run.signal_run_id)

            run.provider_calls = self._provider_calls
            run.completed_at = utcnow()
            run.duration_seconds = round(time.time() - started, 3)

            if self.store is not None:
                self.store.save_run(run)
                log_event("signal_result_persisted",
                          signal_run_id=run.signal_run_id,
                          results=len(run.results))

            log_event("signal_engine_completed",
                      signal_run_id=run.signal_run_id,
                      requested=run.requested_count,
                      evaluated=run.evaluated_count,
                      skipped=run.skipped_count, errors=run.error_count,
                      directions=run.direction_counts,
                      provider_calls=run.provider_calls,
                      duration_seconds=run.duration_seconds)
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"
            run.completed_at = utcnow()
            log_event("signal_engine_failed",
                      signal_run_id=run.signal_run_id,
                      error=type(exc).__name__)
            raise

        return run

    def run_for_scanner(self, scanner_run, top_n: Optional[int] = None
                        ) -> SignalRun:
        """Enrich a scanner run's top candidates.

        The scanner ranks research priority; this answers what the
        quantitative evidence actually says about the ones it surfaced.
        Ranking order is preserved, so "top N" means the scanner's top N
        and not an arbitrary subset.
        """
        limit = top_n if top_n is not None else self.top_n
        candidates = sorted(getattr(scanner_run, "candidates", []),
                            key=lambda c: c.rank)[:limit]
        return self.run(
            [c.symbol for c in candidates],
            market_regime=getattr(scanner_run, "market_regime", "UNKNOWN"),
            regime_confidence=getattr(scanner_run, "regime_confidence", 0.0),
            market_session=getattr(scanner_run, "market_session", "UNKNOWN"),
            scanner_run_id=getattr(scanner_run, "scanner_run_id", None),
            session_date=getattr(scanner_run, "session_date", None),
        )
