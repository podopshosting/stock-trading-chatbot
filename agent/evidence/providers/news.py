"""
News evidence providers.

Verified live 2026-09-30 against our own Alpaca credentials:

    GET https://data.alpaca.markets/v1beta1/news?symbols=NVDA&limit=3
    -> HTTP 200, items timestamped to the current minute

    fields: author, content, created_at, headline, id, images, source,
            summary, symbols, updated_at, url

**`content` came back EMPTY (0 characters).** Our entitlement covers the
headline, a short summary (~120 chars) and the URL — not full article
text. That is a licensing fact, not a bug, and it shapes the design:
there is no full article to store even if we wanted to, so
`EvidenceItem.summary` holds only the provider-supplied excerpt and the
URL carries the reader to the publisher. See docs/EVIDENCE-SOURCES.md.

Articles are tagged with EVERY symbol they mention — one observed item
carried nine tickers. A story about the semiconductor sector is not nine
company-specific catalysts, so relevance to the requested symbol is
scored rather than assumed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ...observability import log_event
from ..classification import classify_headline
from ..models import (
    EvidenceItem, EvidenceSource, EvidenceType, SourceClass, utcnow,
)
from .base import (
    EvidenceProvider, EvidenceRateLimited, EvidenceUnavailable,
    SymbolNotCovered,
)

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

# News is not immutable, but a given article id is. Cache the RESULT SET
# briefly and individual articles for longer.
NEWS_LIST_TTL = 300.0

# An article tagged with more symbols than this is a sector or market
# round-up rather than company news. Observed: a Bank of America
# semiconductor note tagged with 9 tickers.
BROAD_COVERAGE_SYMBOLS = 5


def _parse_iso(raw: str) -> Optional[float]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            continue
    return None


class AlpacaNewsProvider(EvidenceProvider):
    """Timely ticker-tagged news.

    Classified STRUCTURED_NEWS, not PRIMARY: this is a licensed feed
    carrying a publisher's account of an event. When the same event also
    appears as an SEC filing or an issuer press release, that copy is the
    primary one and deduplication prefers it.
    """

    name = "alpaca_news"
    source_class = SourceClass.STRUCTURED_NEWS

    def __init__(self, api_key_id: str, api_secret_key: str, cache=None,
                 http=None, wall_clock=time.time):
        self._key_id = api_key_id
        self._secret = api_secret_key
        self.cache = cache
        self._http = http
        self._wall_clock = wall_clock
        self._calls = 0
        self._cache_hits = 0

    def _requests(self):
        if self._http is not None:
            return self._http
        import requests
        return requests

    def _headers(self) -> Dict[str, str]:
        # Credentials go in headers, never in the URL: a query string
        # ends up in access logs and error messages.
        return {"APCA-API-KEY-ID": self._key_id,
                "APCA-API-SECRET-KEY": self._secret}

    def _get(self, params: Dict) -> Dict:
        cache_key = ("news:alpaca:" + ":".join(
            f"{k}={v}" for k, v in sorted(params.items())))
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self._cache_hits += 1
                return cached.get("payload", cached)

        self._calls += 1
        try:
            resp = self._requests().get(ALPACA_NEWS_URL,
                                        headers=self._headers(),
                                        params=params, timeout=20)
        except Exception as e:
            raise EvidenceUnavailable(f"alpaca news request failed: {e}") from e

        if resp.status_code == 429:
            raise EvidenceRateLimited("alpaca news rate limit reached")
        if resp.status_code in (401, 403):
            raise EvidenceUnavailable(
                "alpaca news refused the credentials; the account may not be "
                "entitled to the news feed")
        if resp.status_code != 200:
            raise EvidenceUnavailable(
                f"alpaca news returned HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except Exception as e:
            raise EvidenceUnavailable(
                f"alpaca news returned unparseable JSON: {e}") from e

        if self.cache is not None:
            try:
                # `put`, not `set` - the backend has no `set`, and a
                # swallowed AttributeError here meant nothing was cached.
                self.cache.put(cache_key, {"payload": payload}, NEWS_LIST_TTL)
            except Exception as e:
                log_event("evidence_provider_failed", provider=self.name,
                          operation="cache_put", error=type(e).__name__)
        return payload

    def fetch(self, symbol: str, since: Optional[float] = None,
              limit: int = 25) -> List[EvidenceItem]:
        symbol = (symbol or "").upper()
        params: Dict = {"symbols": symbol, "limit": min(max(limit, 1), 50),
                        "sort": "desc"}
        if since is not None:
            params["start"] = datetime.fromtimestamp(
                since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = self._get(params)
        raw_items = payload.get("news") or []
        if not raw_items:
            raise SymbolNotCovered(f"no recent news for {symbol}")

        now = self._wall_clock()
        out: List[EvidenceItem] = []

        for raw in raw_items:
            article_id = str(raw.get("id") or "")
            headline = (raw.get("headline") or "").strip()
            if not article_id or not headline:
                # Without an id there is nothing to deduplicate against,
                # and without a headline there is nothing to classify.
                continue

            published = _parse_iso(raw.get("created_at") or "")
            symbols = [str(s).upper() for s in (raw.get("symbols") or [])]
            summary = (raw.get("summary") or "").strip()

            etype, direction, materiality, reason = classify_headline(
                headline, summary)

            item = EvidenceItem(
                evidence_id=EvidenceItem.make_id(self.name, article_id,
                                                 symbol),
                symbol=symbol,
                source=EvidenceSource(
                    provider=self.name,
                    publisher=str(raw.get("source") or "unknown"),
                    source_class=SourceClass.STRUCTURED_NEWS,
                    url=str(raw.get("url") or ""),
                    document_id=article_id),
                evidence_type=etype,
                published_at=raw.get("created_at"),
                retrieved_at=utcnow(),
                headline=headline,
                # Only the provider's own excerpt. Full article text is
                # not licensed to us and is not returned.
                summary=summary,
                direction=direction,
                materiality=materiality,
                catalyst_category=etype,
                symbols=symbols or [symbol],
                classification_reason=reason,
                raw_metadata={
                    "article_id": article_id,
                    "author": raw.get("author"),
                    "updated_at": raw.get("updated_at"),
                    "symbol_count": len(symbols),
                    "content_licensed": bool(raw.get("content")),
                },
            )
            if published is not None:
                item.age_hours = max(0.0, (now - published) / 3600.0)
            else:
                item.warnings.append(
                    "no publication timestamp; age and novelty cannot be "
                    "assessed")

            # Breadth discount: an article tagged with many tickers is
            # about a sector, and counting it as company-specific news
            # would let one round-up become a catalyst for every name
            # in it.
            if len(symbols) > BROAD_COVERAGE_SYMBOLS:
                item.materiality *= 0.4
                item.warnings.append(
                    f"tagged with {len(symbols)} symbols; treated as sector "
                    f"coverage rather than company-specific news")
                item.classification_reason += (
                    f"; materiality reduced for breadth "
                    f"({len(symbols)} symbols)")

            out.append(item)

        log_event("evidence_item_normalized", provider=self.name,
                  symbol=symbol, count=len(out))
        return out

    def capabilities(self) -> Dict:
        return {
            "name": self.name,
            "source_class": str(self.source_class),
            "requires_key": True,
            "full_text_available": False,
            "licensing": ("headline, short summary and URL only; article "
                          "`content` is not returned on this entitlement, "
                          "so full text is neither stored nor storable"),
            "realtime": True,
            "historical_coverage": "paged via next_page_token",
            "multi_symbol_tagging": True,
        }
