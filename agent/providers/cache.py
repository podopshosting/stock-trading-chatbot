"""
Caching layer for market data providers.

Why this exists: the Alpha Vantage free tier allows ~1 request/second and
25 requests/day. A single stock analysis costs 2 requests, capping the
system at roughly 12 analyses per day. Caching is the difference between a
toy and something that can scan a universe.

Design:

* `CachedProvider` wraps *any* `MarketDataProvider`, so caching is not
  reimplemented per provider and a provider swap keeps it.
* Two tiers: an in-process dict (free, survives warm Lambda invocations)
  in front of an optional shared store (DynamoDB, survives cold starts and
  is shared across concurrent Lambdas).
* TTL is per data type. A daily bar series from last week is still a
  perfectly good daily bar series; a quote from last week is not.
* Cache hits are marked in `Provenance.cache_hit` and keep the ORIGINAL
  `retrieved_at`, so age is measured from when the data was actually
  fetched from the provider, never from when it was served from cache.
  Without that, cached data would appear to refresh itself.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Dict, Optional, Tuple

from .base import (
    BarSet,
    MarketDataProvider,
    Quote,
    RateLimited,
    barset_from_dict,
    quote_from_dict,
    to_dict,
)

# Default TTLs in seconds.
DEFAULT_TTLS: Dict[str, float] = {
    "quote": 60.0,          # a minute-old quote is fine for scanning
    "bars:1day": 6 * 3600,  # daily bars change once per session
    "bars:intraday": 120.0,
    "market_status": 300.0,
    "assets": 6 * 3600,     # reference data changes at most daily
}


def ttl_for(kind: str, ttls: Optional[Dict[str, float]] = None) -> float:
    table = {**DEFAULT_TTLS, **(ttls or {})}
    if kind in table:
        return table[kind]
    if kind.startswith("bars:") and kind != "bars:1day":
        return table["bars:intraday"]
    return 60.0


class CacheBackend(ABC):
    """Key/value store with TTL. Must fail soft: a broken cache degrades to
    a cache miss, never to an application error."""

    @abstractmethod
    def get(self, key: str) -> Optional[dict]:
        ...

    @abstractmethod
    def put(self, key: str, value: dict, ttl_seconds: float) -> None:
        ...

    def get_stale(self, key: str) -> Optional[dict]:
        """Return an expired-but-retained value, if the backend keeps one.

        Used only to survive a provider rate limit, and the caller must
        label whatever it serves as stale. Backends that cannot retain
        expired entries return None and the rate limit simply propagates.
        """
        return None


class MemoryCache(CacheBackend):
    """In-process cache. Survives warm Lambda invocations only.

    Expired entries are retained rather than deleted on read, so
    `get_stale` can still offer them when the provider is rate limited.
    They are purged when the store exceeds `max_entries`.
    """

    def __init__(self, clock=time.time, max_entries: int = 512):
        self._store: Dict[str, Tuple[float, dict]] = {}
        self._clock = clock
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[dict]:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self.misses += 1
            return None
        self.hits += 1
        return value

    def get_stale(self, key: str) -> Optional[dict]:
        entry = self._store.get(key)
        return None if entry is None else entry[1]

    def put(self, key: str, value: dict, ttl_seconds: float) -> None:
        self._store[key] = (self._clock() + ttl_seconds, value)
        if len(self._store) > self.max_entries:
            self._purge()

    def _purge(self) -> None:
        """Drop expired entries first, then the soonest-to-expire."""
        now = self._clock()
        for k in [k for k, (exp, _) in self._store.items() if now >= exp]:
            del self._store[k]
        if len(self._store) > self.max_entries:
            for k, _ in sorted(self._store.items(), key=lambda kv: kv[1][0])[
                : len(self._store) - self.max_entries
            ]:
                del self._store[k]

    def clear(self) -> None:
        self._store.clear()

    @property
    def hit_rate(self) -> Optional[float]:
        total = self.hits + self.misses
        return None if total == 0 else self.hits / total


class DynamoDBCache(CacheBackend):
    """Shared cache backed by DynamoDB with native TTL expiry.

    Degrades to a no-op if the table or credentials are unavailable, so
    development and tests never require infrastructure.
    """

    def __init__(self, table_name: str = "stock-agent-dev-cache",
                 region: str = "us-east-2", client=None, clock=time.time):
        self.table_name = table_name
        self.region = region
        self._clock = clock
        self._client = client
        self._disabled = False
        self.hits = 0
        self.misses = 0
        self.errors = 0

    def _get_client(self):
        if self._client is None and not self._disabled:
            try:
                import boto3
                self._client = boto3.client("dynamodb", region_name=self.region)
            except Exception as e:  # pragma: no cover - env dependent
                print(f"[cache] DynamoDB unavailable, disabling: {e}")
                self._disabled = True
        return self._client

    def get(self, key: str) -> Optional[dict]:
        client = self._get_client()
        if client is None:
            self.misses += 1
            return None
        try:
            resp = client.get_item(
                TableName=self.table_name,
                Key={"cache_key": {"S": key}},
            )
        except Exception as e:
            self.errors += 1
            print(f"[cache] read failed for {key}: {e}")
            self.misses += 1
            return None

        item = resp.get("Item")
        if not item:
            self.misses += 1
            return None

        # DynamoDB TTL deletion is asynchronous and may lag by minutes, so
        # the expiry must be re-checked here rather than trusted.
        expires_at = float(item.get("expires_at", {}).get("N", 0))
        if expires_at and self._clock() >= expires_at:
            self.misses += 1
            return None

        try:
            self.hits += 1
            return json.loads(item["payload"]["S"])
        except Exception as e:
            self.errors += 1
            print(f"[cache] corrupt entry {key}: {e}")
            self.hits -= 1
            self.misses += 1
            return None

    def put(self, key: str, value: dict, ttl_seconds: float) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            client.put_item(
                TableName=self.table_name,
                Item={
                    "cache_key": {"S": key},
                    "payload": {"S": json.dumps(value)},
                    "expires_at": {"N": str(int(self._clock() + ttl_seconds))},
                    "stored_at": {"N": str(int(self._clock()))},
                },
            )
        except Exception as e:
            self.errors += 1
            print(f"[cache] write failed for {key}: {e}")

    @property
    def hit_rate(self) -> Optional[float]:
        total = self.hits + self.misses
        return None if total == 0 else self.hits / total


class TieredCache(CacheBackend):
    """Memory in front of a shared store. Promotes shared hits into memory."""

    def __init__(self, fast: CacheBackend, slow: Optional[CacheBackend] = None):
        self.fast = fast
        self.slow = slow

    def get(self, key: str) -> Optional[dict]:
        value = self.fast.get(key)
        if value is not None:
            return value
        if self.slow is None:
            return None
        value = self.slow.get(key)
        if value is not None:
            # Promote, but only for the remaining lifetime we know about.
            self.fast.put(key, value, ttl_seconds=self._remaining_ttl(value))
        return value

    @staticmethod
    def _remaining_ttl(value: dict) -> float:
        prov = (value or {}).get("provenance") or {}
        retrieved = prov.get("retrieved_at")
        kind = (value or {}).get("_kind", "quote")
        if not retrieved:
            return 30.0
        elapsed = time.time() - float(retrieved)
        return max(1.0, ttl_for(kind) - elapsed)

    def put(self, key: str, value: dict, ttl_seconds: float) -> None:
        self.fast.put(key, value, ttl_seconds)
        if self.slow is not None:
            self.slow.put(key, value, ttl_seconds)


def cache_key(provider: str, kind: str, symbol: str, **extra) -> str:
    """Keys include the provider, so switching providers cannot serve data
    attributed to the wrong source."""
    parts = [provider, kind, symbol.upper()]
    for k in sorted(extra):
        parts.append(f"{k}={extra[k]}")
    return "|".join(parts)


class CachedProvider(MarketDataProvider):
    """Wraps a provider with caching. Delegates anything it does not cache."""

    def __init__(self, inner: MarketDataProvider,
                 backend: Optional[CacheBackend] = None,
                 ttls: Optional[Dict[str, float]] = None,
                 serve_stale_on_rate_limit: bool = True,
                 stale_grace_seconds: float = 900.0,
                 clock=time.time):
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self.backend = backend if backend is not None else MemoryCache()
        self.ttls = ttls or {}
        self.serve_stale_on_rate_limit = serve_stale_on_rate_limit
        self.stale_grace_seconds = stale_grace_seconds
        self._clock = clock
        self.provider_calls = 0

    # -- internals --------------------------------------------------------

    def _cached(self, kind: str, key: str, fetch, revive):
        hit = self.backend.get(key)
        if hit is not None:
            obj = revive(hit)
            prov = obj.provenance
            if prov is not None:
                obj = replace(obj, provenance=replace(prov, cache_hit=True))
            return obj

        try:
            obj = fetch()
        except RateLimited:
            # Throttled with no fresh value. A recently expired entry is
            # worth more than nothing, provided it is labelled honestly.
            stale = self._get_stale(key)
            if self.serve_stale_on_rate_limit and stale is not None:
                obj = revive(stale)
                prov = obj.provenance
                if prov is not None and not prov.is_stale(
                    self.stale_grace_seconds, self._clock()
                ):
                    age = int(prov.age_seconds(self._clock()))
                    return replace(obj, provenance=replace(
                        prov,
                        cache_hit=True,
                        note=f"STALE: served from cache after rate limit, {age}s old",
                    ))
            raise

        self.provider_calls += 1
        payload = to_dict(obj)
        payload["_kind"] = kind
        self.backend.put(key, payload, ttl_for(kind, self.ttls))
        return obj

    def _get_stale(self, key: str) -> Optional[dict]:
        """Read past TTL via the backend's retention hook."""
        value = self.backend.get_stale(key)
        if value is None:
            fast = getattr(self.backend, "fast", None)
            if fast is not None:
                value = fast.get_stale(key)
        return value

    # -- interface --------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        key = cache_key(self.inner.name, "quote", symbol)
        return self._cached("quote", key,
                            lambda: self.inner.get_quote(symbol),
                            quote_from_dict)

    def get_bars(self, symbol: str, timeframe: str = "1day",
                 limit: int = 100) -> BarSet:
        # Cache by symbol+timeframe only, not by limit: a 100-bar series
        # already answers a 50-bar request, and keying on limit would cause
        # avoidable refetches of data we hold.
        key = cache_key(self.inner.name, "bars", symbol, tf=timeframe)
        bs = self._cached(f"bars:{timeframe}", key,
                          lambda: self.inner.get_bars(symbol, timeframe, limit),
                          barset_from_dict)
        if len(bs.bars) > limit:
            return replace(bs, bars=bs.bars[-limit:])
        return bs

    def get_snapshot(self, symbols) -> Dict[str, Quote]:
        """Serve what is cached, then batch-fetch only the rest.

        The base class's sequential loop would call get_quote per symbol
        and throw away the inner provider's batch endpoint - measured as 3
        requests instead of 1 for a three-index evaluation. Batching is
        the reason request budget stopped being a constraint, so it has to
        survive being wrapped.
        """
        out: Dict[str, Quote] = {}
        missing = []

        for symbol in symbols:
            sym = symbol.upper()
            hit = self.backend.get(cache_key(self.inner.name, "quote", sym))
            if hit is None:
                missing.append(sym)
                continue
            quote = quote_from_dict(hit)
            if quote.provenance is not None:
                quote = replace(quote, provenance=replace(
                    quote.provenance, cache_hit=True))
            out[sym] = quote

        if missing:
            fetched = self.inner.get_snapshot(missing)
            self.provider_calls += 1
            ttl = ttl_for("quote", self.ttls)
            for sym, quote in fetched.items():
                payload = to_dict(quote)
                payload["_kind"] = "quote"
                self.backend.put(cache_key(self.inner.name, "quote", sym),
                                 payload, ttl)
                out[sym] = quote

        return out

    def get_market_status(self):
        return self.inner.get_market_status()

    def get_assets(self, status: str = "active",
                   asset_class: str = "us_equity"):
        """Cached: reference data changes at most daily, and it is the
        single largest response the scanner fetches."""
        key = cache_key(self.inner.name, "assets", "ALL",
                        status=status, cls=asset_class)
        hit = self.backend.get(key)
        if hit is not None and isinstance(hit.get("rows"), list):
            self.backend_hit_assets = True
            return hit["rows"]
        rows = self.inner.get_assets(status=status, asset_class=asset_class)
        self.provider_calls += 1
        self.backend.put(key, {"rows": rows, "_kind": "assets"},
                         ttl_for("assets", self.ttls))
        return rows

    def get_bars_multi(self, symbols, timeframe: str = "1day",
                       limit: int = 100, **kwargs):
        return self.inner.get_bars_multi(symbols, timeframe, limit=limit,
                                         **kwargs)

    def get_clock(self):
        return self.inner.get_clock()

    def get_calendar(self, start: str, end: str):
        return self.inner.get_calendar(start, end)

    def capabilities(self) -> Dict[str, object]:
        caps = dict(self.inner.capabilities())
        caps["cached"] = True
        return caps

    @property
    def stats(self) -> Dict[str, object]:
        backend = self.backend
        return {
            "provider_calls": self.provider_calls,
            "hits": getattr(backend, "hits", None),
            "misses": getattr(backend, "misses", None),
            "hit_rate": getattr(backend, "hit_rate", None),
        }
