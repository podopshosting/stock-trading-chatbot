"""
Tests for AlpacaProvider (Milestone 2b).

Offline: a fake HTTP layer replays payloads shaped like the real responses
captured from the live API on 2026-09-30.

The behaviours that matter most here are about honesty, not plumbing. The
free plan offers a genuine choice between two imperfect feeds:

  iex          real-time, but only ~2.5% of US volume (not consolidated)
  delayed_sip  full consolidated tape, 15 minutes stale

Neither is "the price". A caller that cannot tell which one it got will
eventually make a decision on a number it has misunderstood, so the feed
and its limitation must travel with the value.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.providers import (  # noqa: E402
    AlpacaProvider, DataUnavailable, EntitlementRequired, RateLimited,
    SymbolNotFound,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Replays queued payloads per URL path fragment."""

    def __init__(self):
        self.calls = []
        self.queues = {}

    def queue(self, path_fragment, *payloads):
        self.queues.setdefault(path_fragment, []).extend(payloads)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for fragment, q in self.queues.items():
            if fragment in url and q:
                return FakeResponse(*q.pop(0)) if isinstance(q[0], tuple) \
                    else FakeResponse(q.pop(0))
        return FakeResponse({}, 200)


# Shapes captured from the live API on 2026-09-30.
SNAPSHOT_IEX = {
    "AAPL": {
        "latestTrade": {"p": 337.355, "s": 100, "t": "2026-09-30T15:22:09Z"},
        "latestQuote": {"bp": 337.33, "ap": 337.37, "bs": 2, "as": 1,
                        "t": "2026-09-30T15:22:09Z"},
        "dailyBar": {"o": 336.0, "h": 338.5, "l": 335.1, "c": 337.35,
                     "v": 12000000, "t": "2026-09-30T04:00:00Z"},
        "prevDailyBar": {"o": 330.0, "h": 339.0, "l": 328.7, "c": 338.4,
                         "v": 38478037, "t": "2026-09-29T04:00:00Z"},
    }
}

def _daily_rows(n=60):
    """Strictly increasing timestamps, so ordering assertions are real."""
    from datetime import date, timedelta
    d0 = date(2026, 1, 1)
    return [
        {"t": (d0 + timedelta(days=i)).isoformat() + "T04:00:00Z",
         "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i,
         "v": 1000000 + i, "n": 500, "vw": 100 + i}
        for i in range(n)
    ]


BARS_DAILY = {"bars": {"AAPL": _daily_rows(60)}, "next_page_token": None}

QUOTES_SIP = {
    "quotes": {"AAPL": [
        {"t": "2026-09-30T15:07:12Z", "bp": 338.74, "ap": 338.78,
         "bs": 1, "as": 3},
    ]},
    "next_page_token": None,
}


def make_provider(http, **kw):
    return AlpacaProvider(api_key_id="k", api_secret_key="s", http=http,
                          sleep=lambda _s: None, **kw)


class TestAuthAndConfig(unittest.TestCase):
    def test_credentials_are_required(self):
        with self.assertRaises(ValueError):
            AlpacaProvider(api_key_id="", api_secret_key="s")
        with self.assertRaises(ValueError):
            AlpacaProvider(api_key_id="k", api_secret_key="")

    def test_auth_headers_are_sent_and_not_in_params(self):
        http = FakeHTTP()
        http.queue("/snapshots", SNAPSHOT_IEX)
        make_provider(http).get_quote("AAPL")
        _url, params = http.calls[0]
        self.assertNotIn("APCA-API-KEY-ID", params)
        self.assertNotIn("api_key", params)


class TestQuoteProvenance(unittest.TestCase):
    """The core honesty requirement."""

    def setUp(self):
        self.http = FakeHTTP()
        self.http.queue("/snapshots", SNAPSHOT_IEX)

    def test_iex_quote_is_realtime_but_records_its_coverage_limit(self):
        q = make_provider(self.http, quote_feed="iex").get_quote("AAPL")

        self.assertEqual(q.symbol, "AAPL")
        self.assertEqual(q.price, 337.355)
        self.assertIs(q.provenance.is_delayed, False)
        self.assertIn("iex", q.provenance.note.lower())
        self.assertRegex(
            q.provenance.note.lower(), r"not consolidated|iex only|partial",
            msg="an IEX-only price must carry its coverage caveat",
        )

    def test_delayed_sip_quote_is_marked_delayed(self):
        http = FakeHTTP()
        http.queue("/snapshots", SNAPSHOT_IEX)
        q = make_provider(http, quote_feed="delayed_sip").get_quote("AAPL")

        self.assertIs(q.provenance.is_delayed, True)
        self.assertIn("delayed_sip", q.provenance.note.lower())
        self.assertIn("15", q.provenance.note)

    def test_quote_carries_real_bid_ask_and_spread(self):
        """Alpha Vantage gave no bid/ask, so spread_pct was always None and
        the risk layer's spread rule was unenforceable."""
        q = make_provider(self.http).get_quote("AAPL")

        self.assertEqual(q.bid, 337.33)
        self.assertEqual(q.ask, 337.37)
        self.assertIsNotNone(q.spread_pct)
        self.assertAlmostEqual(q.spread_pct, 0.01186, places=3)

    def test_change_is_computed_against_previous_close(self):
        q = make_provider(self.http).get_quote("AAPL")
        self.assertAlmostEqual(q.previous_close, 338.4, places=4)
        self.assertAlmostEqual(q.change, 337.355 - 338.4, places=4)


class TestErrorMapping(unittest.TestCase):
    def test_recent_sip_403_is_entitlement_not_rate_limit(self):
        """Verified live: recent SIP returns
        403 'subscription does not permit querying recent SIP data'.
        That is a plan boundary, so retrying cannot help."""
        http = FakeHTTP()
        http.queue("/bars", (
            {"message": "subscription does not permit querying recent SIP data"},
            403,
        ))
        with self.assertRaises(EntitlementRequired):
            make_provider(http).get_bars("AAPL", "1day")

    def test_429_is_rate_limited(self):
        http = FakeHTTP()
        http.queue("/bars", ({"message": "too many requests"}, 429))
        with self.assertRaises(RateLimited):
            make_provider(http).get_bars("AAPL", "1day")

    def test_404_is_symbol_not_found(self):
        http = FakeHTTP()
        http.queue("/snapshots", ({"message": "not found"}, 404))
        with self.assertRaises(SymbolNotFound):
            make_provider(http).get_quote("ZZZZQQ")

    def test_empty_snapshot_is_symbol_not_found(self):
        http = FakeHTTP()
        http.queue("/snapshots", {})
        with self.assertRaises(SymbolNotFound):
            make_provider(http).get_quote("ZZZZQQ")

    def test_401_reports_authentication_not_a_data_problem(self):
        """Bad credentials must not look like a missing symbol or feed."""
        http = FakeHTTP()
        http.queue("/snapshots", ({"message": "unauthorized"}, 401))
        with self.assertRaises(DataUnavailable) as ctx:
            make_provider(http).get_quote("AAPL")
        self.assertNotIsInstance(ctx.exception, EntitlementRequired)
        self.assertIn("authentication", str(ctx.exception).lower())


class TestBars(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.http.queue("/bars", BARS_DAILY)

    def test_bars_are_parsed_oldest_first(self):
        bs = make_provider(self.http).get_bars("AAPL", "1day", limit=60)
        self.assertEqual(len(bs), 60)
        self.assertEqual(bs.bars[0].close, 100)
        self.assertEqual(bs.bars[-1].close, 159)

    def test_intraday_is_supported(self):
        """Unlike Alpha Vantage free, where intraday is paywalled."""
        http = FakeHTTP()
        http.queue("/bars", BARS_DAILY)
        bs = make_provider(http).get_bars("AAPL", "5min", limit=10)
        self.assertEqual(len(bs), 10)
        _url, params = http.calls[0]
        self.assertEqual(params["timeframe"], "5Min")

    def test_bars_use_the_consolidated_feed_by_default(self):
        """`sip`, not `delayed_sip`: the bars endpoint rejects the latter
        (verified live). Freshness is bounded by `end` instead."""
        make_provider(self.http).get_bars("AAPL", "1day")
        _url, params = self.http.calls[0]
        self.assertEqual(params["feed"], "sip",
                         "bars should use the full tape, not IEX")

    def test_bar_request_ends_before_the_delay_window(self):
        """Requesting right up to 'now' on a delayed feed returns 403."""
        make_provider(self.http).get_bars("AAPL", "1day")
        _url, params = self.http.calls[0]
        self.assertIn("end", params, "must bound the request to avoid a 403")

    def test_unknown_timeframe_is_explicit(self):
        with self.assertRaises(DataUnavailable):
            make_provider(self.http).get_bars("AAPL", "tick")


class TestBatchSnapshot(unittest.TestCase):
    def test_many_symbols_cost_one_request(self):
        """Verified live: 10 symbols returned in a single request. This is
        what removes request budget as a design constraint."""
        http = FakeHTTP()
        symbols = ["AAPL", "MSFT", "NVDA", "SOFI", "AMD"]
        http.queue("/snapshots", {s: SNAPSHOT_IEX["AAPL"] for s in symbols})

        snap = make_provider(http).get_snapshot(symbols)

        self.assertEqual(sorted(snap), sorted(symbols))
        self.assertEqual(len(http.calls), 1,
                         f"expected 1 batched request, got {len(http.calls)}")

    def test_snapshot_skips_symbols_the_provider_omits(self):
        http = FakeHTTP()
        http.queue("/snapshots", {"AAPL": SNAPSHOT_IEX["AAPL"]})
        snap = make_provider(http).get_snapshot(["AAPL", "ZZZZQQ"])
        self.assertIn("AAPL", snap)
        self.assertNotIn("ZZZZQQ", snap)

    def test_large_symbol_lists_are_chunked(self):
        http = FakeHTTP()
        symbols = [f"SYM{i}" for i in range(250)]
        for _ in range(3):
            http.queue("/snapshots", {s: SNAPSHOT_IEX["AAPL"] for s in symbols[:100]})
        make_provider(http, max_symbols_per_request=100).get_snapshot(symbols)
        self.assertEqual(len(http.calls), 3)


class TestCapabilities(unittest.TestCase):
    def setUp(self):
        self.caps = make_provider(FakeHTTP()).capabilities()

    def test_declares_what_was_verified(self):
        self.assertTrue(self.caps["intraday"])
        self.assertTrue(self.caps["bid_ask"])
        self.assertTrue(self.caps["batch_quotes"])
        self.assertEqual(self.caps["requests_per_minute"], 200)

    def test_undocumented_daily_cap_is_unknown_not_unlimited(self):
        """Alpaca documents no daily cap. An absent limit is UNKNOWN, not a
        confirmed absence, and must not be reported as unlimited."""
        self.assertIsNone(self.caps["daily_request_budget"])
        self.assertIn("undocumented", str(self.caps.get("daily_budget_note", "")).lower())

    def test_declares_the_delay_constraint(self):
        self.assertEqual(self.caps["sip_delay_seconds"], 900)


class TestMarketStatus(unittest.TestCase):
    def test_clock_is_parsed(self):
        http = FakeHTTP()
        http.queue("/clock", {"is_open": True, "timestamp": "2026-09-30T15:30:00-04:00",
                              "next_close": "2026-09-30T16:00:00-04:00",
                              "next_open": "2026-10-01T09:30:00-04:00"})
        st = make_provider(http).get_market_status()
        self.assertTrue(st.is_open)
        self.assertEqual(st.session, "regular")

    def test_closed_market(self):
        http = FakeHTTP()
        http.queue("/clock", {"is_open": False, "timestamp": "2026-09-30T20:00:00-04:00",
                              "next_close": "2026-10-01T16:00:00-04:00",
                              "next_open": "2026-10-01T09:30:00-04:00"})
        st = make_provider(http).get_market_status()
        self.assertFalse(st.is_open)
        self.assertEqual(st.session, "closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# Captured live 2026-09-30 for AAPL. The IEX daily bar is an IEX-ONLY
# aggregate, not the official consolidated session:
#   iex          prevClose=329.58  prevVol=1,247,778   todayVol=   659,196
#   delayed_sip  prevClose=329.40  prevVol=38,641,026  todayVol=14,681,035
# Volume differs by ~22x. Relative volume computed from the IEX figure is
# not an approximation of the real thing, it is a different quantity.
SNAPSHOT_IEX_PARTIAL = {
    "AAPL": {
        "latestTrade": {"p": 337.075, "t": "2026-09-30T15:30:00Z"},
        "latestQuote": {"bp": 337.06, "ap": 337.10},
        "dailyBar": {"o": 330.0, "h": 338.0, "l": 329.0, "c": 337.07,
                     "v": 659196, "t": "2026-09-30T04:00:00Z"},
        "prevDailyBar": {"o": 331.0, "h": 335.0, "l": 328.0, "c": 329.58,
                         "v": 1247778, "t": "2026-09-29T04:00:00Z"},
    }
}


class TestFeedValidity(unittest.TestCase):
    """Verified live: /v2/stocks/bars rejects delayed_sip with
    400 'invalid feed: delayed_sip'. It accepts iex and sip."""

    def test_bars_do_not_request_delayed_sip(self):
        http = FakeHTTP()
        http.queue("/bars", BARS_DAILY)
        make_provider(http).get_bars("AAPL", "1day")
        _url, params = http.calls[0]
        self.assertNotEqual(
            params["feed"], "delayed_sip",
            "the bars endpoint rejects delayed_sip; use sip with a bounded end",
        )
        self.assertIn(params["feed"], ("sip", "iex"))


class TestIexPartialCoverageIsDisclosed(unittest.TestCase):
    """An IEX-only quote carries IEX-only volume and OHLC, not just a
    slightly different price. That has to be visible."""

    def test_iex_note_warns_that_volume_and_ohlc_are_partial(self):
        http = FakeHTTP()
        http.queue("/snapshots", SNAPSHOT_IEX_PARTIAL)
        q = make_provider(http, quote_feed="iex").get_quote("AAPL")

        note = q.provenance.note.lower()
        self.assertIn("volume", note,
                      "IEX volume is venue-only and must be called out")

    def test_default_quote_feed_is_the_consolidated_tape(self):
        """The consolidated tape, never IEX, because the numbers feed
        relative-volume and spread rules.

        The default was `delayed_sip` while the plan offered only
        delayed SIP or real-time IEX. With Algo Trader Plus (entitlement
        verified live 2026-10-01) real-time SIP dominates both, so the
        default is `sip`; what must not change is that the default is a
        consolidated feed.
        """
        p = make_provider(FakeHTTP())
        self.assertEqual(p.quote_feed, "sip")
        self.assertTrue(p.capabilities()["consolidated_volume"])

    def test_iex_feed_reports_non_consolidated_volume(self):
        p = make_provider(FakeHTTP(), quote_feed="iex")
        self.assertFalse(
            p.capabilities()["consolidated_volume"],
            "capabilities must not imply consolidated volume on IEX",
        )


class TestBarWindow(unittest.TestCase):
    """Without an explicit `start`, Alpaca returns a tiny recent window.
    Measured live: a 200-bar daily request came back with ONE bar. The ML
    layer needs 50+, so this would starve it silently - the same failure
    shape as the outage this project recovered from."""

    def _params_for(self, timeframe, limit):
        http = FakeHTTP()
        http.queue("/bars", BARS_DAILY)
        make_provider(http).get_bars("AAPL", timeframe, limit=limit)
        return http.calls[0][1]

    def test_request_includes_a_start(self):
        self.assertIn("start", self._params_for("1day", 200))

    def test_daily_window_covers_enough_calendar_days(self):
        """200 trading days needs ~290 calendar days of slack."""
        from datetime import datetime, timezone
        p = self._params_for("1day", 200)
        start = datetime.strptime(p["start"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        end = datetime.strptime(p["end"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        days = (end - start).days
        self.assertGreaterEqual(
            days, 280, f"window of {days} days is too short for 200 daily bars")

    def test_intraday_window_spans_multiple_sessions(self):
        """60 five-minute bars is 5 trading hours, which crosses a session
        boundary unless the request lands mid-afternoon."""
        from datetime import datetime, timezone
        p = self._params_for("5min", 60)
        start = datetime.strptime(p["start"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        end = datetime.strptime(p["end"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        self.assertGreaterEqual((end - start).total_seconds() / 3600, 12)


BARS_DESC = {
    "bars": {"AAPL": [
        {"t": f"2026-09-{30 - i:02d}T04:00:00Z", "o": 100, "h": 101,
         "l": 99, "c": 200 - i, "v": 1000, "n": 5, "vw": 100}
        for i in range(5)          # newest first, as sort=desc returns
    ]},
}


class TestBarRecency(unittest.TestCase):
    """With sort=asc, `limit` returns the OLDEST N bars in the window.
    Measured live: a 200-bar daily request ended 2026-08-27 while the
    market had traded on 2026-09-30 - over a month stale, presented as
    current. Bars must be the most RECENT N."""

    def test_request_asks_for_newest_first(self):
        http = FakeHTTP()
        http.queue("/bars", BARS_DESC)
        make_provider(http).get_bars("AAPL", "1day", limit=5)
        _url, params = http.calls[0]
        self.assertEqual(
            params.get("sort"), "desc",
            "must request newest-first, or `limit` truncates to the oldest bars",
        )

    def test_returned_bars_are_reordered_oldest_first(self):
        """Indicators consume oldest -> newest, so a desc response must be
        reversed before it is handed on."""
        http = FakeHTTP()
        http.queue("/bars", BARS_DESC)
        bs = make_provider(http).get_bars("AAPL", "1day", limit=5)

        stamps = [b.timestamp for b in bs.bars]
        self.assertEqual(stamps, sorted(stamps), "bars must be oldest -> newest")
        self.assertEqual(bs.bars[-1].timestamp[:10], "2026-09-30",
                         "the last bar must be the most recent one")
        self.assertEqual(bs.bars[-1].close, 200)
