"""
Alpha Vantage implementation of MarketDataProvider.

Carries forward the throttle handling proven during the 2026-09-30
recovery: the free tier allows ~1 request/second and 25 requests/day, and
throttling arrives as HTTP 200 with an "Information" key rather than an
error status. Reading that as "no data" is what silently disabled the ML
layer, so it is handled explicitly here and surfaced as RateLimited.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from .base import (
    Bar,
    BarSet,
    DataUnavailable,
    MarketDataProvider,
    Provenance,
    Quote,
    RateLimited,
    SymbolNotFound,
)

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

_THROTTLE_MARKERS = (
    "per second", "sparingly", "rate limit", "requests per day",
    "call frequency", "higher api call",
)

_TIMEFRAME_TO_FUNCTION = {
    "1day": ("TIME_SERIES_DAILY", "Time Series (Daily)", {}),
    "1min": ("TIME_SERIES_INTRADAY", "Time Series (1min)", {"interval": "1min"}),
    "5min": ("TIME_SERIES_INTRADAY", "Time Series (5min)", {"interval": "5min"}),
    "15min": ("TIME_SERIES_INTRADAY", "Time Series (15min)", {"interval": "15min"}),
    "30min": ("TIME_SERIES_INTRADAY", "Time Series (30min)", {"interval": "30min"}),
    "60min": ("TIME_SERIES_INTRADAY", "Time Series (60min)", {"interval": "60min"}),
}


class AlphaVantageProvider(MarketDataProvider):

    name = "alphavantage"

    # Free tier: ~1 request/second burst, 25 requests/day.
    MIN_INTERVAL = 1.2
    MAX_RETRIES = 2
    RETRY_DELAY = 1.5
    DAILY_BUDGET = 25

    def __init__(self, api_key: str, http=None, clock=time.monotonic,
                 sleep=time.sleep, min_interval: Optional[float] = None,
                 wall_clock=time.time):
        """`clock` paces requests (monotonic); `wall_clock` stamps provenance.

        They are separate because elapsed-time pacing must not be affected
        by clock adjustments, while provenance needs real epoch time. Both
        are injectable so age-related behaviour is testable.
        """
        if not api_key:
            raise ValueError("AlphaVantageProvider requires an api_key")
        self._api_key = api_key
        self._http = http
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._min_interval = self.MIN_INTERVAL if min_interval is None else min_interval
        self._last_call = 0.0
        self.request_count = 0

    def _requests(self):
        if self._http is None:
            import requests
            self._http = requests
        return self._http

    @staticmethod
    def _is_throttled(data) -> bool:
        if not isinstance(data, dict):
            return False
        for key in ("Note", "Information"):
            message = data.get(key)
            if not message:
                continue
            text = str(message).lower()
            if any(marker in text for marker in _THROTTLE_MARKERS):
                return True
        return False

    def _request(self, params: Dict, timeout: int = 15) -> Dict:
        attempt = 0
        while True:
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)

            try:
                resp = self._requests().get(
                    ALPHA_VANTAGE_URL,
                    params={**params, "apikey": self._api_key},
                    timeout=timeout,
                )
                data = resp.json()
            except Exception as e:
                self._last_call = self._clock()
                raise DataUnavailable(
                    f"alphavantage request failed ({params.get('function')}): {e}"
                ) from e
            finally:
                self._last_call = self._clock()

            self.request_count += 1

            if not self._is_throttled(data):
                if isinstance(data, dict) and data.get("Error Message"):
                    raise SymbolNotFound(self.name, str(params.get("symbol", "?")))
                return data

            attempt += 1
            if attempt > self.MAX_RETRIES:
                raise RateLimited(
                    self.name,
                    f"{params.get('function')} throttled after {attempt} attempts",
                    retry_after=self.RETRY_DELAY,
                )
            self._sleep(self.RETRY_DELAY)

    def _provenance(self, as_of: Optional[str] = None) -> Provenance:
        return Provenance(
            provider=self.name,
            retrieved_at=self._wall_clock(),
            as_of=as_of,
            # Free-tier freshness is not documented per-request; claiming
            # real-time here would be an unverified assertion.
            is_delayed=None,
        )

    # -- interface --------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        data = self._request({"function": "GLOBAL_QUOTE", "symbol": symbol}, timeout=10)
        q = data.get("Global Quote") or {}
        if not q or not q.get("05. price"):
            raise SymbolNotFound(self.name, symbol)

        def num(key, cast=float, default=None):
            raw = q.get(key)
            if raw in (None, "", "None"):
                return default
            try:
                return cast(float(raw))
            except (TypeError, ValueError):
                return default

        return Quote(
            symbol=q.get("01. symbol", symbol).upper(),
            price=num("05. price"),
            change=num("09. change"),
            change_percent=q.get("10. change percent"),
            volume=num("06. volume", int, 0),
            previous_close=num("08. previous close"),
            open=num("02. open"),
            high=num("03. high"),
            low=num("04. low"),
            latest_trading_day=q.get("07. latest trading day"),
            provenance=self._provenance(as_of=q.get("07. latest trading day")),
        )

    def get_bars(self, symbol: str, timeframe: str = "1day",
                 limit: int = 100) -> BarSet:
        if timeframe not in _TIMEFRAME_TO_FUNCTION:
            raise DataUnavailable(
                f"alphavantage has no timeframe {timeframe!r}; "
                f"supported: {sorted(_TIMEFRAME_TO_FUNCTION)}"
            )
        function, series_key, extra = _TIMEFRAME_TO_FUNCTION[timeframe]
        params = {
            "function": function,
            "symbol": symbol,
            "outputsize": "full" if limit > 100 else "compact",
            **extra,
        }
        data = self._request(params)
        series = data.get(series_key)
        if not series:
            raise DataUnavailable(
                f"alphavantage returned no {series_key} for {symbol}"
            )

        bars: List[Bar] = []
        for ts in sorted(series):          # oldest -> newest
            row = series[ts]
            try:
                bars.append(Bar(
                    timestamp=ts,
                    open=float(row["1. open"]),
                    high=float(row["2. high"]),
                    low=float(row["3. low"]),
                    close=float(row["4. close"]),
                    volume=int(float(row.get("5. volume", 0))),
                ))
            except (KeyError, TypeError, ValueError):
                continue        # skip malformed rows rather than fail the series

        if not bars:
            raise DataUnavailable(f"alphavantage {series_key} unusable for {symbol}")

        return BarSet(
            symbol=symbol.upper(),
            timeframe=timeframe,
            bars=bars[-limit:],
            provenance=self._provenance(as_of=bars[-1].timestamp),
        )

    def capabilities(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "intraday": True,
            "bid_ask": False,          # GLOBAL_QUOTE carries no bid/ask
            "streaming": False,
            "batch_quotes": False,     # one request per symbol
            "daily_request_budget": self.DAILY_BUDGET,
            "burst_limit_per_second": 1,
        }
