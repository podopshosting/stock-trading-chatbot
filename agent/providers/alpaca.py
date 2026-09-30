"""
Alpaca implementation of MarketDataProvider.

Everything below was verified against the live API on 2026-09-30 with a free
paper-account key. The numbers are measurements, not documentation quotes.

What the free (Basic) plan actually gives:

  200 requests/minute, no *documented* daily cap
  1Min / 5Min / 1Day bars on the full consolidated tape, back to 2016,
      provided the request ends at least 15 minutes in the past
  NBBO bid/ask and trades on the same 15-minute-delayed basis
  Batch endpoints: 10 symbols in one request, verified
  Real-time IEX feed, which is ~2.5% of US volume and NOT consolidated

The 15-minute boundary is enforced explicitly:

  GET /v2/stocks/bars?feed=sip&start=<10 min ago>
  -> 403 {"message": "subscription does not permit querying recent SIP data"}

So the plan offers a choice between two imperfect feeds, and neither is
simply "the price":

  iex          fresh but partial
  delayed_sip  complete but 15 minutes old

Which one produced a value therefore travels with it in Provenance. A caller
that cannot tell them apart will eventually size a position on a number it
has misunderstood.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from .base import (
    Bar,
    BarSet,
    DataUnavailable,
    EntitlementRequired,
    MarketDataProvider,
    MarketStatus,
    Provenance,
    Quote,
    RateLimited,
    SymbolNotFound,
)

DATA_BASE = "https://data.alpaca.markets"
PAPER_TRADING_BASE = "https://paper-api.alpaca.markets"

# Free plan cannot query consolidated data newer than this.
SIP_DELAY_SECONDS = 900

_TIMEFRAMES = {
    "1min": "1Min", "5min": "5Min", "15min": "15Min", "30min": "30Min",
    "60min": "1Hour", "1hour": "1Hour", "1day": "1Day", "1week": "1Week",
}

_FEED_NOTES = {
    # Measured for AAPL on 2026-09-30: the IEX daily bar reported volume
    # 659,196 against the consolidated 14,681,035 - a ~22x understatement.
    # The price is close to the tape, but volume and OHLC are venue-only
    # quantities, not approximations of the official session.
    "iex": "feed=iex: real-time, but IEX only (~2.5% of US volume), not "
           "consolidated. Price is near the tape; volume and OHLC are "
           "IEX-only and NOT the official session figures - do not use "
           "them for relative volume.",
    "delayed_sip": "feed=delayed_sip: full consolidated tape (official "
                   "volume and OHLC), delayed 15 minutes",
    "sip": "feed=sip: full consolidated tape; a free plan may only query "
           "data older than 15 minutes",
}

# Endpoints do not accept the same feed names. Verified live 2026-09-30:
# /v2/stocks/bars rejects delayed_sip with 400 "invalid feed: delayed_sip",
# while /v2/stocks/snapshots accepts it. Bars therefore use `sip` with an
# `end` bounded behind the delay window, which returns 200.
_BARS_FEEDS = ("sip", "iex")

# timeframe -> (bar duration in seconds, calendar slack multiplier).
# Intraday slack ~4x because only 6.5 of 24 hours trade; daily ~1.6x
# because only ~252 of 365 days trade.
_WINDOW_FACTORS = {
    "1min": (60, 4.5), "5min": (300, 4.5), "15min": (900, 4.5),
    "30min": (1800, 4.5), "60min": (3600, 4.5), "1hour": (3600, 4.5),
    "1day": (86400, 1.6), "1week": (604800, 1.2),
}


class AlpacaProvider(MarketDataProvider):

    name = "alpaca"
    REQUESTS_PER_MINUTE = 200

    def __init__(self, api_key_id: str, api_secret_key: str, http=None,
                 quote_feed: str = "delayed_sip", bar_feed: str = "sip",
                 data_base: str = DATA_BASE,
                 trading_base: str = PAPER_TRADING_BASE,
                 max_symbols_per_request: int = 1000,
                 wall_clock=time.time, sleep=time.sleep):
        if not api_key_id or not api_secret_key:
            raise ValueError(
                "AlpacaProvider requires both api_key_id and api_secret_key"
            )
        self._key = api_key_id
        self._secret = api_secret_key
        self._http = http
        self.quote_feed = quote_feed
        if bar_feed not in _BARS_FEEDS:
            raise ValueError(
                f"bar_feed must be one of {_BARS_FEEDS}; the bars endpoint "
                f"rejects {bar_feed!r}"
            )
        self.bar_feed = bar_feed
        self.data_base = data_base
        self.trading_base = trading_base
        self.max_symbols_per_request = max_symbols_per_request
        self._wall_clock = wall_clock
        self._sleep = sleep
        self.request_count = 0

    # -- plumbing ---------------------------------------------------------

    def _requests(self):
        if self._http is None:
            import requests
            self._http = requests
        return self._http

    def _headers(self) -> Dict[str, str]:
        # Credentials go in headers, never in the query string, so they do
        # not end up in URLs, logs or proxy access records.
        return {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
            "Accept": "application/json",
        }

    def _get(self, base: str, path: str, params: Optional[Dict] = None,
             timeout: int = 20):
        url = f"{base}{path}"
        try:
            resp = self._requests().get(
                url, params=params or {}, headers=self._headers(),
                timeout=timeout,
            )
        except Exception as e:
            raise DataUnavailable(f"alpaca request failed ({path}): {e}") from e

        self.request_count += 1
        status = getattr(resp, "status_code", 200)

        try:
            body = resp.json()
        except Exception:
            body = {}

        if status == 200:
            # Most endpoints return an object, but /v2/calendar returns an
            # array. Normalising that to {} would silently discard the
            # whole calendar, so lists are passed through.
            if isinstance(body, (dict, list)):
                return body
            return {}

        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("msg") or "")

        if status == 429:
            raise RateLimited(self.name, message or "429 from alpaca")

        if status == 401:
            # Bad credentials, not a data problem. Say so plainly rather
            # than letting it look like the symbol or feed was at fault.
            raise DataUnavailable(
                f"alpaca authentication failed (401): {message}"
            )

        if status == 403:
            # Verified live: requesting consolidated data newer than 15
            # minutes on a free plan returns
            # "subscription does not permit querying recent SIP data".
            # A plan boundary, so retrying cannot change the answer.
            raise EntitlementRequired(self.name, path, message or "403")

        if status == 404:
            symbols = (params or {}).get("symbols", "?")
            raise SymbolNotFound(self.name, str(symbols))

        raise DataUnavailable(f"alpaca {path} returned {status}: {message}")

    def _provenance(self, feed: str, as_of: Optional[str] = None) -> Provenance:
        delayed = feed in ("delayed_sip",)
        return Provenance(
            provider=self.name,
            retrieved_at=self._wall_clock(),
            as_of=as_of,
            is_delayed=delayed,
            note=_FEED_NOTES.get(feed, f"feed={feed}"),
        )

    def _delay_bounded_end(self) -> str:
        """An `end` far enough in the past that the free plan will serve it."""
        cutoff = self._wall_clock() - SIP_DELAY_SECONDS - 60
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))

    def _start_from_lookback(self, lookback_seconds: float) -> str:
        """An explicit window, for batched intraday requests."""
        start = (self._wall_clock() - SIP_DELAY_SECONDS - 60
                 - max(60.0, lookback_seconds))
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))

    def _window_start(self, timeframe: str, limit: int) -> str:
        """How far back to ask, to actually receive `limit` bars.

        Alpaca defaults to a small recent window when `start` is omitted:
        a 200-bar daily request measured live returned ONE bar. Bars only
        exist during market hours, so the calendar span must exceed the
        trading span - roughly 1.5x for daily (252 of 365 days trade) and
        about 4x for intraday (6.5 of 24 hours trade), plus a buffer for
        weekends and holidays.
        """
        seconds, slack = _WINDOW_FACTORS.get(
            timeframe.lower(), (86400, 1.6)
        )
        span = seconds * max(limit, 1) * slack
        span += 5 * 86400          # buffer for a long weekend or holiday
        start = self._wall_clock() - SIP_DELAY_SECONDS - 60 - span
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))

    # -- quotes -----------------------------------------------------------

    @staticmethod
    def _quote_from_snapshot(symbol: str, snap: Dict,
                             provenance: Provenance) -> Quote:
        trade = snap.get("latestTrade") or {}
        nbbo = snap.get("latestQuote") or {}
        today = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}

        price = trade.get("p")
        if price is None:
            price = nbbo.get("ap") or today.get("c")
        if price is None:
            raise SymbolNotFound("alpaca", symbol)

        previous_close = prev.get("c")
        change = None
        change_percent = None
        if previous_close:
            change = price - previous_close
            change_percent = f"{(change / previous_close) * 100:.4f}%"

        return Quote(
            symbol=symbol.upper(),
            price=float(price),
            change=change,
            change_percent=change_percent,
            volume=int(today.get("v") or 0),
            previous_close=previous_close,
            open=today.get("o"),
            high=today.get("h"),
            low=today.get("l"),
            latest_trading_day=(today.get("t") or "")[:10] or None,
            bid=nbbo.get("bp"),
            ask=nbbo.get("ap"),
            vwap=today.get("vw"),
            provenance=provenance,
        )

    def get_quote(self, symbol: str) -> Quote:
        body = self._get(self.data_base, "/v2/stocks/snapshots",
                         {"symbols": symbol.upper(), "feed": self.quote_feed})
        snap = body.get(symbol.upper())
        if not snap:
            raise SymbolNotFound(self.name, symbol)
        trade_ts = (snap.get("latestTrade") or {}).get("t")
        return self._quote_from_snapshot(
            symbol, snap, self._provenance(self.quote_feed, as_of=trade_ts)
        )

    def get_snapshot(self, symbols: List[str]) -> Dict[str, Quote]:
        """Batched: many symbols per request.

        Verified live at 2,000 symbols in a single request (~0.9s); the
        default chunk is 1,000 to leave headroom.

        The chunk size is load-bearing, not cosmetic. It was 100 while
        only a 10-symbol batch had been measured, which quietly re-split
        every 1,000-symbol scanner batch into 10 requests and turned a
        13-request universe snapshot into 125.
        """
        out: Dict[str, Quote] = {}
        upper = [s.upper() for s in symbols]
        for i in range(0, len(upper), self.max_symbols_per_request):
            chunk = upper[i:i + self.max_symbols_per_request]
            body = self._get(self.data_base, "/v2/stocks/snapshots",
                             {"symbols": ",".join(chunk),
                              "feed": self.quote_feed})
            for sym in chunk:
                snap = body.get(sym)
                if not snap:
                    continue        # provider omitted it; not an error
                trade_ts = (snap.get("latestTrade") or {}).get("t")
                try:
                    out[sym] = self._quote_from_snapshot(
                        sym, snap,
                        self._provenance(self.quote_feed, as_of=trade_ts),
                    )
                except SymbolNotFound:
                    continue
        return out

    # -- bars -------------------------------------------------------------

    def get_bars(self, symbol: str, timeframe: str = "1day",
                 limit: int = 100) -> BarSet:
        if timeframe.lower() not in _TIMEFRAMES:
            raise DataUnavailable(
                f"alpaca has no timeframe {timeframe!r}; "
                f"supported: {sorted(_TIMEFRAMES)}"
            )
        params = {
            "symbols": symbol.upper(),
            "timeframe": _TIMEFRAMES[timeframe.lower()],
            "limit": max(1, min(limit, 10000)),
            "feed": self.bar_feed,
            "adjustment": "split",
            # Bound the window: asking up to "now" on a delayed feed is a 403.
            "start": self._window_start(timeframe, limit),
            "end": self._delay_bounded_end(),
            # Newest-first, because `limit` truncates the response: with
            # "asc" a 200-bar daily request returned the OLDEST 200 bars in
            # the window, ending over a month ago while the market had
            # traded that morning. The rows are reversed below so callers
            # still receive oldest -> newest.
            "sort": "desc",
        }
        body = self._get(self.data_base, "/v2/stocks/bars", params)
        rows = (body.get("bars") or {}).get(symbol.upper())
        if not rows:
            raise DataUnavailable(
                f"alpaca returned no {timeframe} bars for {symbol}"
            )

        bars: List[Bar] = []
        for row in rows:
            try:
                bars.append(Bar(
                    timestamp=row["t"],
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    volume=int(row.get("v") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue

        if not bars:
            raise DataUnavailable(f"alpaca bars unusable for {symbol}")

        # The request is newest-first; indicators want oldest-first. Sort
        # rather than assume the provider's ordering held.
        bars.sort(key=lambda b: b.timestamp)

        return BarSet(
            symbol=symbol.upper(),
            timeframe=timeframe,
            bars=bars[-limit:],
            provenance=self._provenance(self.bar_feed,
                                        as_of=bars[-1].timestamp),
        )

    # -- market status ----------------------------------------------------

    def get_assets(self, status: str = "active",
                   asset_class: str = "us_equity") -> List[Dict]:
        """Reference data for tradable assets.

        Measured 2026-09-30: 14,388 active us_equity assets in ONE request
        (~3s). Carries exchange, tradable, fractionable, shortable, status,
        name and attributes - but no price or volume, so liquidity is a
        market-data question.
        """
        body = self._get(self.trading_base, "/v2/assets",
                         {"status": status, "asset_class": asset_class},
                         timeout=60)
        return body if isinstance(body, list) else []

    def get_bars_multi(self, symbols: List[str], timeframe: str = "1day",
                       limit: int = 100,
                       lookback_seconds: Optional[float] = None,
                       max_pages: int = 12) -> Dict[str, List[Bar]]:
        """Bars for several symbols per request, following pagination.

        Unlike snapshots, this endpoint paginates: a 500-symbol daily
        request returned 243 symbols with a next_page_token.

        `lookback_seconds` bounds the window explicitly, and for intraday
        batches it matters enormously. Deriving the window from
        limit x slack (as the single-symbol path does) asked for ~5.5 days
        of 5-minute bars across 300 symbols - roughly 150,000 bars, far
        past the page cap, so most symbols came back with no recent bars
        at all and their short-window returns were silently absent.
        """
        if timeframe.lower() not in _TIMEFRAMES:
            raise DataUnavailable(
                f"alpaca has no timeframe {timeframe!r}; "
                f"supported: {sorted(_TIMEFRAMES)}"
            )
        symbols = [s.upper() for s in symbols if s]
        if not symbols:
            # An empty list would be sent as symbols= and 400. Spending a
            # request to be told nothing was asked for is pure waste.
            self.last_page_count = 0
            return {}

        out: Dict[str, List[Bar]] = {}
        page_token = None
        pages = 0

        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": _TIMEFRAMES[timeframe.lower()],
                "limit": 10000,
                "feed": self.bar_feed,
                "adjustment": "split",
                "start": (self._window_start(timeframe, limit)
                          if lookback_seconds is None
                          else self._start_from_lookback(lookback_seconds)),
                "end": self._delay_bounded_end(),
                "sort": "desc",
            }
            if page_token:
                params["page_token"] = page_token

            body = self._get(self.data_base, "/v2/stocks/bars", params,
                             timeout=90)
            for symbol, rows in (body.get("bars") or {}).items():
                bucket = out.setdefault(symbol, [])
                for row in rows:
                    try:
                        bucket.append(Bar(
                            timestamp=row["t"],
                            open=float(row["o"]), high=float(row["h"]),
                            low=float(row["l"]), close=float(row["c"]),
                            volume=int(row.get("v") or 0),
                        ))
                    except (KeyError, TypeError, ValueError):
                        continue

            page_token = body.get("next_page_token")
            pages += 1
            # Bounded so a pathological response cannot loop forever.
            if not page_token or pages >= max_pages:
                break

        self.last_page_count = pages

        # Requested newest-first; indicators want oldest-first.
        for symbol in out:
            out[symbol].sort(key=lambda b: b.timestamp)
            if limit and len(out[symbol]) > limit:
                out[symbol] = out[symbol][-limit:]
        return out

    def get_clock(self) -> Dict:
        """Raw broker clock.

        `is_open` refers to the REGULAR session only; it is false during
        pre-market and after-hours, so it cannot distinguish those on its
        own. MarketSessionService combines it with the calendar.
        """
        return self._get(self.trading_base, "/v2/clock")

    def get_calendar(self, start: str, end: str) -> List[Dict]:
        """Trading calendar rows for [start, end], dates as YYYY-MM-DD.

        Non-trading days are ABSENT rather than flagged: Thanksgiving
        2026-11-26 has no row between 11-25 and 11-27. Early closes appear
        as a shorter `close` (13:00 on 2026-11-27), with `session_open` /
        `session_close` giving the extended-hours window.
        """
        body = self._get(self.trading_base, "/v2/calendar",
                         {"start": start, "end": end})
        return body if isinstance(body, list) else []

    def get_market_status(self) -> MarketStatus:
        body = self._get(self.trading_base, "/v2/clock")
        if "is_open" not in body:
            raise DataUnavailable("alpaca clock returned no is_open")
        is_open = bool(body["is_open"])
        return MarketStatus(
            is_open=is_open,
            session="regular" if is_open else "closed",
            as_of=body.get("timestamp", ""),
            provenance=Provenance(provider=self.name,
                                  retrieved_at=self._wall_clock(),
                                  as_of=body.get("timestamp"),
                                  is_delayed=False),
        )

    # -- capabilities -----------------------------------------------------

    def capabilities(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "intraday": True,
            "bid_ask": True,
            "streaming": True,          # websocket exists; not used here
            "batch_quotes": True,
            "max_symbols_per_request": self.max_symbols_per_request,
            "requests_per_minute": self.REQUESTS_PER_MINUTE,
            # Alpaca documents no daily cap. An absent limit is UNKNOWN,
            # not a confirmed absence, so this is None rather than a
            # number implying "unlimited".
            "daily_request_budget": None,
            "daily_budget_note": (
                "undocumented: no daily cap is published, which is not the "
                "same as a confirmed absence of one"
            ),
            "sip_delay_seconds": SIP_DELAY_SECONDS,
            "quote_feed": self.quote_feed,
            "bar_feed": self.bar_feed,
            # False on IEX: volume and OHLC are venue-only there, so
            # relative-volume features must not be built on them.
            "consolidated_volume": self.quote_feed != "iex",
            "history_since": "2016",
        }
