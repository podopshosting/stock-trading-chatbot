"""
SEC EDGAR connector.

The highest-provenance source available, and free: no key, no account,
no licensing restriction on the filings themselves. What a company files
with the SEC is what the company is legally accountable for having said.

Verified live 2026-09-30:

  https://www.sec.gov/files/company_tickers.json
      10,431 ticker->CIK entries, ~780 KB, ~1.0s

  https://data.sec.gov/submissions/CIK##########.json
      up to 1,000 recent filings, ~156 KB, ~0.5s, no auth

The submissions payload carries `items` for 8-K filings - the filer's own
item numbers - which means the catalyst category for the most important
filing type is DETERMINISTIC. Item 2.02 is "Results of Operations and
Financial Condition" because the filer said so, not because a model
guessed it from prose. See `classification.py`.

Two SEC requirements are honoured here:

  * A descriptive User-Agent with contact details. SEC refuses requests
    without one.
  * A request rate at or under 10/second. This paces at 8/second.

Filings are IMMUTABLE once published, so they are cached hard. A filing
fetched once never needs fetching again, which is what makes it
affordable to check many symbols.
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from ...observability import log_event
from ..classification import FinancingStage, classify_form
from ..models import (
    EvidenceItem, EvidenceSource, EvidenceType, SourceClass, utcnow,
)
from .base import EvidenceProvider, EvidenceUnavailable, SymbolNotCovered

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = ("https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
               "{document}")
FILING_INDEX_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action="
                    "getcompany&CIK={cik:010d}&type={form}")

# SEC's published limit is 10 requests/second. Pacing below it leaves
# room for other callers on the same egress IP.
MIN_REQUEST_INTERVAL = 0.125

# Forms worth surfacing, in rough order of catalyst value. Anything else
# is fetched (it comes in the same payload) but not turned into evidence,
# so a 13F-HR from an unrelated filer does not become a catalyst.
PRIORITY_FORMS = (
    "8-K", "10-Q", "10-K", "4", "SC 13D", "SC 13G",
    "S-3", "S-3ASR", "S-1", "424B5", "424B2", "424B3", "424B4",
)

# The ticker map is large and changes rarely.
TICKER_MAP_TTL = 24 * 3600
# A company's filing list changes when it files; an hour is responsive
# enough for an intraday catalyst without hammering EDGAR.
SUBMISSIONS_TTL = 3600
# A filing itself never changes.
FILING_TTL = 365 * 24 * 3600


class SECProvider(EvidenceProvider):
    """Filings for one issuer, normalised into EvidenceItem."""

    name = "sec"
    source_class = SourceClass.PRIMARY

    def __init__(self, user_agent: str, cache=None, http=None,
                 sleep=time.sleep, clock=time.monotonic,
                 wall_clock=time.time):
        if not user_agent or "@" not in user_agent:
            # SEC asks for a contact address. A request without one is
            # refused, and discovering that at runtime in a Lambda is a
            # worse place to find out than here.
            raise ValueError(
                "SEC requires a descriptive User-Agent including a contact "
                "email address"
            )
        self.user_agent = user_agent
        self.cache = cache
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._last_request = 0.0
        self._calls = 0
        self._cache_hits = 0
        self._index: Optional[Dict[str, Tuple[int, str]]] = None

    # --- transport -------------------------------------------------------

    def _requests(self):
        if self._http is not None:
            return self._http
        import requests
        return requests

    def _pace(self) -> None:
        elapsed = self._clock() - self._last_request
        if elapsed < MIN_REQUEST_INTERVAL:
            self._sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = self._clock()

    def _get_json(self, url: str, cache_key: str, ttl: float) -> Dict:
        """Fetch JSON, honouring the cache and the rate limit."""
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self._cache_hits += 1
                return cached.get("payload", cached)

        self._pace()
        self._calls += 1
        try:
            resp = self._requests().get(
                url, headers={"User-Agent": self.user_agent,
                              "Accept-Encoding": "gzip, deflate"},
                timeout=20)
        except Exception as e:
            raise EvidenceUnavailable(f"SEC request failed ({url}): {e}") from e

        if resp.status_code == 404:
            raise SymbolNotCovered(f"SEC has no record at {url}")
        if resp.status_code == 403:
            raise EvidenceUnavailable(
                "SEC refused the request (403). This is usually a missing or "
                "unacceptable User-Agent.")
        if resp.status_code != 200:
            raise EvidenceUnavailable(
                f"SEC returned HTTP {resp.status_code} for {url}")

        try:
            payload = resp.json()
        except Exception as e:
            raise EvidenceUnavailable(f"SEC returned unparseable JSON: {e}") from e

        self._cache_put(cache_key, {"payload": payload}, ttl)
        return payload

    def _cache_put(self, key: str, value: Dict, ttl: float) -> None:
        """Write through to the cache, loudly on failure.

        The backend's method is `put`. An earlier version called `set`,
        which does not exist - and because the failure was swallowed by a
        bare `except: pass`, NOTHING was ever cached and the 780 KB
        ticker map was re-downloaded on every lookup. A 4-second run
        became 33 seconds with no error anywhere.
        
        A cache write failure still must not fail the fetch, but it is
        now reported rather than hidden.
        """
        if self.cache is None:
            return
        try:
            self.cache.put(key, value, ttl)
        except Exception as e:
            log_event("evidence_provider_failed", provider=self.name,
                      operation="cache_put", error=type(e).__name__)

    # --- ticker -> CIK ---------------------------------------------------

    def _ticker_index(self) -> Dict[str, Tuple[int, str]]:
        """ticker -> (cik, name), built once per process.

        The raw payload is ~10,400 rows. Scanning it linearly per lookup
        turned peer-name resolution into the slowest thing in the
        pipeline; an index makes repeated lookups free.
        """
        if getattr(self, "_index", None) is not None:
            return self._index
        payload = self._get_json(TICKER_MAP_URL, "sec:ticker_map",
                                 TICKER_MAP_TTL)
        index: Dict[str, Tuple[int, str]] = {}
        for row in payload.values():
            ticker = str(row.get("ticker", "")).upper()
            if ticker:
                index[ticker] = (int(row["cik_str"]), row.get("title", ""))
        self._index = index
        return index

    def cik_for(self, symbol: str) -> Tuple[int, str]:
        """Resolve a ticker to its CIK and registered name."""
        symbol = (symbol or "").upper()
        found = self._ticker_index().get(symbol)
        if found is None:
            raise SymbolNotCovered(f"{symbol} is not in the SEC ticker map")
        return found

    # --- filings ---------------------------------------------------------

    def _submissions(self, cik: int) -> Dict:
        return self._get_json(SUBMISSIONS_URL.format(cik=cik),
                              f"sec:submissions:{cik}", SUBMISSIONS_TTL)

    @staticmethod
    def _filed_epoch(filing_date: str, acceptance: str) -> Optional[float]:
        """Prefer acceptanceDateTime: it is when the filing actually
        became public, to the second. filingDate is a date only, and
        using it would put an 8-K accepted at 16:05 at midnight."""
        from datetime import datetime, timezone
        for raw, fmt in ((acceptance, "%Y-%m-%dT%H:%M:%S.%f%z"),
                         (acceptance, "%Y-%m-%dT%H:%M:%S%z"),
                         (acceptance, "%Y-%m-%dT%H:%M:%S.%fZ"),
                         (acceptance, "%Y-%m-%dT%H:%M:%SZ")):
            if not raw:
                continue
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except (ValueError, TypeError):
                continue
        if filing_date:
            try:
                dt = datetime.strptime(filing_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
                return dt.timestamp()
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _published_iso(epoch: Optional[float]) -> Optional[str]:
        from datetime import datetime, timezone
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
            timespec="seconds")

    def fetch(self, symbol: str, since: Optional[float] = None,
              limit: int = 25) -> List[EvidenceItem]:
        symbol = (symbol or "").upper()
        cik, issuer = self.cik_for(symbol)
        payload = self._submissions(cik)
        recent = (payload.get("filings") or {}).get("recent") or {}

        forms = recent.get("form") or []
        if not forms:
            return []

        accessions = recent.get("accessionNumber") or []
        dates = recent.get("filingDate") or []
        acceptances = recent.get("acceptanceDateTime") or []
        items_col = recent.get("items") or []
        documents = recent.get("primaryDocument") or []
        descriptions = recent.get("primaryDocDescription") or []

        out: List[EvidenceItem] = []
        now = self._wall_clock()

        for i, form in enumerate(forms):
            if len(out) >= limit:
                break
            base = str(form).split("/")[0].strip().upper()
            if base not in PRIORITY_FORMS and form not in PRIORITY_FORMS:
                continue

            filed = self._filed_epoch(
                dates[i] if i < len(dates) else "",
                acceptances[i] if i < len(acceptances) else "")
            if since is not None and filed is not None and filed < since:
                # The list is newest-first, so once we are past the
                # window every remaining filing is older too.
                break

            accession = accessions[i] if i < len(accessions) else ""
            item_codes = items_col[i] if i < len(items_col) else ""
            document = documents[i] if i < len(documents) else ""
            description = descriptions[i] if i < len(descriptions) else ""

            etype, direction, materiality, stage, reason = classify_form(
                str(form), str(item_codes))

            url = ""
            if accession and document:
                url = ARCHIVE_URL.format(
                    cik=cik, accession=accession.replace("-", ""),
                    document=document)

            headline = f"{issuer or symbol}: {form}"
            if description:
                headline += f" - {description}"

            item = EvidenceItem(
                evidence_id=EvidenceItem.make_id(self.name, accession, symbol),
                symbol=symbol,
                source=EvidenceSource(
                    provider=self.name, publisher="SEC EDGAR",
                    source_class=SourceClass.PRIMARY, url=url,
                    document_id=accession),
                evidence_type=etype,
                published_at=self._published_iso(filed),
                retrieved_at=utcnow(),
                headline=headline,
                summary=reason,
                direction=direction,
                materiality=materiality,
                catalyst_category=etype,
                classification_reason=reason,
                raw_metadata={
                    "form": str(form),
                    "items": str(item_codes),
                    "cik": cik,
                    "issuer": issuer,
                    "accession_number": accession,
                    "primary_document": document,
                    "filing_date": dates[i] if i < len(dates) else None,
                    "acceptance_datetime": (acceptances[i]
                                            if i < len(acceptances) else None),
                    "financing_stage": str(stage),
                },
            )
            if filed is not None:
                item.age_hours = max(0.0, (now - filed) / 3600.0)

            # The distinction that matters most for short-duration
            # trading, stated on the item rather than left implicit.
            if stage is FinancingStage.ABILITY_TO_ISSUE:
                item.warnings.append(
                    "shelf registration: this is capacity to issue "
                    "securities later, NOT an offering being sold now")
            elif stage is FinancingStage.ACTUAL_OFFERING:
                item.warnings.append(
                    "priced offering: securities are being sold")

            out.append(item)

        log_event("evidence_item_normalized", provider=self.name,
                  symbol=symbol, count=len(out), cik=cik)
        return out

    def capabilities(self) -> Dict:
        return {
            "name": self.name,
            "source_class": str(self.source_class),
            "requires_key": False,
            "requires_user_agent": True,
            "rate_limit_per_second": 8,
            "forms": list(PRIORITY_FORMS),
            "filings_immutable": True,
            "licensing": ("US government works; SEC filings are public "
                          "domain and may be stored and displayed"),
            "full_text_available": True,
            "historical_coverage": "1,000 most recent filings per issuer in "
                                   "the primary payload; older in paged files",
        }
