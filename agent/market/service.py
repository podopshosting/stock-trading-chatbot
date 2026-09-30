"""
MarketRegimeService: provider -> normalised data -> engine -> agent state.

All provider-specific knowledge lives here. The engine never sees a Quote,
a BarSet or an HTTP error, which is what lets it stay a pure function and
lets a provider swap leave it untouched.

Request discipline: one batched snapshot covers every instrument, then one
daily-bars and one intraday-bars call per instrument. For three indices
that is 7 requests, and the cache makes repeat evaluations inside the TTL
free. A partial failure degrades one instrument rather than the whole
evaluation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..config import DEFAULT_CONFIG, AgentConfig
from ..observability import log_event
from ..providers.base import (
    DataUnavailable, EntitlementRequired, ProviderError, RateLimited,
    SymbolNotFound,
)
from .regime import IndexInput, MarketRegimeEngine, MarketRegimeResult


@dataclass
class FetchStats:
    provider_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    provider_errors: int = 0

    def as_dict(self) -> Dict:
        return {
            "provider_calls": self.provider_calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "provider_errors": self.provider_errors,
        }


class MarketRegimeService:

    def __init__(self, provider, config: AgentConfig = DEFAULT_CONFIG,
                 engine: Optional[MarketRegimeEngine] = None,
                 clock=time.time):
        self.provider = provider
        self.config = config
        self.engine = engine or MarketRegimeEngine(config.regime)
        self._clock = clock

    # -- normalisation ----------------------------------------------------

    def _age_of(self, provenance) -> Optional[float]:
        if provenance is None or provenance.retrieved_at is None:
            return None
        # Age from when the provider produced it, not from now-minus-zero:
        # a cached value keeps its original retrieved_at precisely so this
        # stays honest.
        return max(0.0, self._clock() - provenance.retrieved_at)

    def collect(self) -> tuple:
        """Fetch and normalise data for the configured instruments."""
        symbols: List[str] = list(self.config.regime.instruments)
        stats = FetchStats()
        inputs: Dict[str, IndexInput] = {
            s: IndexInput(symbol=s, error="not fetched") for s in symbols
        }

        # One batched call for all quotes.
        quotes = {}
        try:
            quotes = self.provider.get_snapshot(symbols)
        except ProviderError as e:
            stats.provider_errors += 1
            log_event("provider_error", operation="get_snapshot",
                      symbols=",".join(symbols), error=str(e)[:200])
        except Exception as e:
            stats.provider_errors += 1
            log_event("provider_error", operation="get_snapshot",
                      symbols=",".join(symbols), error=str(e)[:200])

        for symbol in symbols:
            quote = quotes.get(symbol)
            if quote is None:
                inputs[symbol] = IndexInput(
                    symbol=symbol, error="quote unavailable"
                )
                continue

            data = IndexInput(
                symbol=symbol,
                price=quote.price,
                session_open=quote.open,
                previous_close=quote.previous_close,
                provider=(quote.provenance.provider if quote.provenance else ""),
                as_of=(quote.provenance.as_of if quote.provenance else None),
                data_age_seconds=self._age_of(quote.provenance),
            )
            if quote.provenance is not None and quote.provenance.cache_hit:
                stats.cache_hits += 1
            else:
                stats.cache_misses += 1

            data.daily_closes = self._daily_closes(symbol, stats)
            data.intraday_bars = self._intraday_bars(symbol, stats)
            inputs[symbol] = data

        return inputs, stats

    def _daily_closes(self, symbol: str, stats: FetchStats) -> List[float]:
        try:
            bars = self.provider.get_bars(
                symbol, "1day", limit=self.config.regime.daily_bars
            )
            if bars.provenance is not None and bars.provenance.cache_hit:
                stats.cache_hits += 1
            else:
                stats.cache_misses += 1
            return bars.closes()
        except (RateLimited, EntitlementRequired, DataUnavailable,
                SymbolNotFound) as e:
            stats.provider_errors += 1
            log_event("provider_error", operation="get_bars", symbol=symbol,
                      timeframe="1day", error=str(e)[:200])
            return []
        except Exception as e:
            stats.provider_errors += 1
            log_event("provider_error", operation="get_bars", symbol=symbol,
                      timeframe="1day", error=str(e)[:200])
            return []

    def _intraday_bars(self, symbol: str, stats: FetchStats) -> List:
        cfg = self.config.regime
        try:
            bars = self.provider.get_bars(
                symbol, cfg.intraday_timeframe, limit=cfg.intraday_bars
            )
            if bars.provenance is not None and bars.provenance.cache_hit:
                stats.cache_hits += 1
            else:
                stats.cache_misses += 1
            return list(bars.bars)
        except (RateLimited, EntitlementRequired, DataUnavailable,
                SymbolNotFound) as e:
            # Intraday is optional: VWAP is one weighted term, and losing
            # it should cost a feature, not the whole evaluation.
            stats.provider_errors += 1
            log_event("provider_error", operation="get_bars", symbol=symbol,
                      timeframe=cfg.intraday_timeframe, error=str(e)[:200])
            return []
        except Exception as e:
            stats.provider_errors += 1
            log_event("provider_error", operation="get_bars", symbol=symbol,
                      timeframe=cfg.intraday_timeframe, error=str(e)[:200])
            return []

    # -- evaluation -------------------------------------------------------

    def evaluate(self) -> tuple:
        inputs, stats = self.collect()
        result = self.engine.evaluate(inputs)
        result.data_quality["fetch"] = stats.as_dict()
        return result, stats

    def evaluate_and_record(self, state_service) -> MarketRegimeResult:
        """Evaluate, then persist onto today's agent session."""
        result, _stats = self.evaluate()
        state_service.record_regime(
            regime=result.regime,
            score=result.raw_score,
            confidence=result.confidence,
            risk_posture=result.risk_posture,
            detail=result.as_dict(),
        )
        return result
