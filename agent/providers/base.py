"""
Market data provider interface and value types.

Two rules run through this module:

1. **Nothing is returned without provenance.** Every value carries which
   provider produced it, when it was retrieved, and what moment in the
   market it describes. Callers can therefore always answer "how old is
   this?" without guessing.

2. **Unavailable is not the same as zero, and throttled is not the same as
   unavailable.** Providers raise distinct errors so the caller can tell a
   transient rate limit apart from a symbol that does not exist.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


# --- errors ---------------------------------------------------------------

class ProviderError(Exception):
    """Base class for market data provider failures."""


class RateLimited(ProviderError):
    """Provider throttled us. Transient; says nothing about the symbol."""

    def __init__(self, provider: str, detail: str = "", retry_after: float = 0.0):
        super().__init__(f"{provider} rate limited: {detail}")
        self.provider = provider
        self.detail = detail
        self.retry_after = retry_after


class SymbolNotFound(ProviderError):
    """The provider answered, and the symbol does not exist there."""

    def __init__(self, provider: str, symbol: str):
        super().__init__(f"{provider}: unknown symbol {symbol}")
        self.provider = provider
        self.symbol = symbol


class DataUnavailable(ProviderError):
    """Provider reachable but the requested data could not be produced."""


class EntitlementRequired(DataUnavailable):
    """The endpoint exists but this plan cannot access it.

    Deliberately NOT a RateLimited: a paywall is permanent, so retrying
    only spends the request budget. Callers should stop asking for this
    data on this provider rather than back off and try again.
    """

    def __init__(self, provider: str, endpoint: str, detail: str = ""):
        super().__init__(
            f"{provider}: {endpoint} requires a paid plan"
            + (f" ({detail})" if detail else "")
        )
        self.provider = provider
        self.endpoint = endpoint
        self.detail = detail


# --- value types ----------------------------------------------------------

@dataclass(frozen=True)
class Provenance:
    """Where a value came from and how old it is."""

    provider: str
    retrieved_at: float               # epoch seconds, when WE fetched it
    as_of: Optional[str] = None       # provider's own timestamp for the data
    is_delayed: Optional[bool] = None # None = unknown, never assume real-time
    cache_hit: bool = False
    note: str = ""

    def age_seconds(self, now: Optional[float] = None) -> float:
        return (time.time() if now is None else now) - self.retrieved_at

    def is_stale(self, max_age_seconds: float, now: Optional[float] = None) -> bool:
        return self.age_seconds(now) > max_age_seconds


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    change: Optional[float] = None
    change_percent: Optional[str] = None
    volume: Optional[int] = None
    previous_close: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    latest_trading_day: Optional[str] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    # Session volume-weighted average price, when the provider supplies
    # one. Alpaca's snapshot dailyBar carries `vw`, which is the real
    # session VWAP - so it does not have to be recomputed from bars.
    vwap: Optional[float] = None
    provenance: Optional[Provenance] = None

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Optional[float]:
        """Spread as a percentage of the midpoint, or None if unknown.

        Returns None rather than 0.0 when bid/ask are absent: a missing
        spread must never look like a tight one to the risk layer.
        """
        s = self.spread
        if s is None:
            return None
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return None
        return (s / mid) * 100


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. `timestamp` is the bar's OPEN time, ISO-8601."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3


@dataclass(frozen=True)
class BarSet:
    symbol: str
    timeframe: str                    # "1min", "5min", "1day"
    bars: List[Bar] = field(default_factory=list)
    provenance: Optional[Provenance] = None

    def __len__(self) -> int:
        return len(self.bars)

    def closes(self) -> List[float]:
        return [b.close for b in self.bars]

    def volumes(self) -> List[int]:
        return [b.volume for b in self.bars]


@dataclass(frozen=True)
class MarketStatus:
    is_open: bool
    session: str                      # "pre", "regular", "post", "closed"
    as_of: str
    provenance: Optional[Provenance] = None


# --- serialisation helpers (for the cache) --------------------------------

def to_dict(obj) -> Dict:
    return asdict(obj)


def _strip_meta(d: Dict) -> Dict:
    """Drop cache bookkeeping keys (``_kind`` and friends).

    The cache annotates stored payloads; those annotations are not fields of
    the value types and must not reach their constructors.
    """
    return {k: v for k, v in d.items() if not k.startswith("_")}


def quote_from_dict(d: Dict) -> Quote:
    d = _strip_meta(d)
    p = d.get("provenance")
    return Quote(**{**d, "provenance": Provenance(**p) if p else None})


def barset_from_dict(d: Dict) -> BarSet:
    d = _strip_meta(d)
    p = d.get("provenance")
    return BarSet(
        symbol=d["symbol"],
        timeframe=d["timeframe"],
        bars=[Bar(**b) for b in d.get("bars", [])],
        provenance=Provenance(**p) if p else None,
    )


# --- provider interface ---------------------------------------------------

class MarketDataProvider(ABC):
    """Interface every market data source implements.

    Implementations raise RateLimited / SymbolNotFound / DataUnavailable
    rather than returning None, so callers cannot silently mistake a
    throttle for an absence of data. That confusion caused the ML outage
    this project recovered from.
    """

    name: str = "abstract"

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        ...

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str = "1day",
                 limit: int = 100) -> BarSet:
        ...

    def get_intraday_bars(self, symbol: str, interval: str = "5min",
                          limit: int = 100) -> BarSet:
        return self.get_bars(symbol, timeframe=interval, limit=limit)

    def get_market_status(self) -> MarketStatus:
        raise DataUnavailable(f"{self.name} does not expose market status")

    def get_snapshot(self, symbols: List[str]) -> Dict[str, Quote]:
        """Quotes for several symbols.

        Default implementation is sequential. Providers with a native batch
        endpoint should override: on a per-request-quota provider the
        difference is the whole daily budget.
        """
        out: Dict[str, Quote] = {}
        for sym in symbols:
            try:
                out[sym] = self.get_quote(sym)
            except SymbolNotFound:
                continue
        return out

    # Capability advertisement. The scanner uses this to decide what it can
    # actually ask for, instead of discovering limits by failing.
    def capabilities(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "intraday": False,
            "bid_ask": False,
            "streaming": False,
            "batch_quotes": False,
            "daily_request_budget": None,
        }
