"""
SEC Company Facts (XBRL) provider.

https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json

One concept can hold annual, quarterly, YTD, amended and overlapping
observations in several units. This module does NOT pick "the latest
number": it normalises every observation into a FinancialPeriod with its
explicit period, fiscal labels, form and filing date, and leaves
selection to fundamentals.py's deterministic rules.

Respects the SEC contract: descriptive User-Agent, <= 8 requests/second.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from agent.providers.base import DataUnavailable
from ..models import FinancialPeriod, Provenance

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# One concept instead of the whole taxonomy. The share count behind a
# market cap is ~10 KB here against ~4.5 MB for companyfacts, and peer
# selection needs nothing else from XBRL - so a nine-company comparison
# does not download 67 MB to read nine numbers.
CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
               "CIK{cik:010d}/{taxonomy}/{concept}.json")
MIN_INTERVAL = 0.125

# canonical name -> (taxonomy, [XBRL concepts in preference order], unit)
CONCEPTS: Dict[str, tuple] = {
    "revenue": ("us-gaap", ["Revenues",
                            "RevenueFromContractWithCustomerExcludingAssessedTax",
                            "SalesRevenueNet"], "USD"),
    "gross_profit": ("us-gaap", ["GrossProfit"], "USD"),
    "operating_income": ("us-gaap", ["OperatingIncomeLoss"], "USD"),
    "net_income": ("us-gaap", ["NetIncomeLoss"], "USD"),
    "eps_diluted": ("us-gaap", ["EarningsPerShareDiluted"], "USD/shares"),
    "cash": ("us-gaap", ["CashAndCashEquivalentsAtCarryingValue"], "USD"),
    "total_debt": ("us-gaap", ["DebtLongtermAndShorttermCombinedAmount",
                               "LongTermDebt"], "USD"),
    "current_assets": ("us-gaap", ["AssetsCurrent"], "USD"),
    "current_liabilities": ("us-gaap", ["LiabilitiesCurrent"], "USD"),
    "total_assets": ("us-gaap", ["Assets"], "USD"),
    "total_liabilities": ("us-gaap", ["Liabilities"], "USD"),
    "equity": ("us-gaap", ["StockholdersEquity"], "USD"),
    "operating_cash_flow": ("us-gaap",
                            ["NetCashProvidedByUsedInOperatingActivities"],
                            "USD"),
    "capex": ("us-gaap", ["PaymentsToAcquirePropertyPlantAndEquipment"],
              "USD"),
    # Cover-page share count (dei). Used only for a market-cap band.
    "shares_outstanding": ("dei", ["EntityCommonStockSharesOutstanding"],
                           "shares"),
}


# Only forms that carry audited/reviewed financial statements. Facts a
# company tags in a proxy (DEF 14A pay-versus-performance), a registration
# statement or an 8-K are NOT the statements, and a later-filed one would
# otherwise win the "latest filed" tie-break and replace the real figure.
STATEMENT_FORMS = {"10-K", "10-K/A", "10-K405", "10-KT", "10-Q", "10-Q/A",
                   "20-F", "20-F/A", "40-F", "40-F/A"}


def normalise(payload: Dict, retrieved_at: str,
              source: str) -> Dict[str, List[FinancialPeriod]]:
    """Every observation of every preferred concept, as FinancialPeriod.

    One XBRL concept is chosen per canonical name and used whole: we never
    splice two concepts into one series. The choice is the concept whose
    series reaches the most recent period (ties: more observations, then
    the listed preference). "First concept that has any rows" is wrong: a
    company can tag a small side item under the preferred name and its
    real revenue under another, and live GIS data did exactly that.
    """
    facts = (payload or {}).get("facts") or {}
    out: Dict[str, List[FinancialPeriod]] = {}
    for canon, (tax, names, unit) in CONCEPTS.items():
        best = None
        for rank, name in enumerate(names):
            node = (facts.get(tax) or {}).get(name)
            rows = ((node or {}).get("units") or {}).get(unit) or []
            periods = []
            for r in rows:
                if r.get("val") is None or not r.get("end"):
                    continue
                if r.get("form") not in STATEMENT_FORMS:
                    continue
                fy = r.get("fy")
                label = r.get("fp")
                start = r.get("start")
                # fp/fy describe the FILING, not the period: a quarter
                # reported inside a 10-K carries fp="FY". A label that
                # contradicts the period's own length is dropped.
                if start and label == "FY":
                    from datetime import date
                    n = (date.fromisoformat(r["end"])
                         - date.fromisoformat(start)).days
                    if not 350 <= n <= 380:
                        label = None
                periods.append(FinancialPeriod(
                    concept=canon, value=float(r["val"]), unit=unit,
                    period_start=start, period_end=r["end"],
                    fiscal_year=int(fy) if fy is not None else None,
                    fiscal_period=label, form=r.get("form"),
                    filed=r.get("filed"), accession=r.get("accn"),
                    provenance=Provenance(
                        "sec_companyfacts", f"{source}#{name}", retrieved_at,
                        period=r["end"])))
            if not periods:
                continue
            key = (max(p.period_end for p in periods), len(periods), -rank)
            if best is None or key > best[0]:
                best = (key, periods)
        if best:
            out[canon] = best[1]
    return out


class SECCompanyFacts:
    name = "sec_companyfacts"

    def __init__(self, user_agent: str, cik_for: Callable[[str], tuple],
                 http=None, sleep=time.sleep, clock=time.monotonic,
                 wall_clock=time.time):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC requires a User-Agent with contact details")
        self._ua, self._cik_for, self._http = user_agent, cik_for, http
        self._sleep, self._clock, self._wall = sleep, clock, wall_clock
        self._last = None
        # Peer fan-out calls this from several threads. Without the lock
        # the pacing check reads and writes `_last` unsynchronised, so the
        # rate it enforces is not the rate it claims.
        import threading
        self._pace_lock = threading.Lock()

    def _pace(self):
        with self._pace_lock:
            now = self._clock()
            wait = 0.0
            if self._last is not None and now - self._last < MIN_INTERVAL:
                wait = MIN_INTERVAL - (now - self._last)
            self._last = now + wait
        if wait:
            self._sleep(wait)

    def _get(self, url: str, what: str):
        http = self._http
        if http is None:
            import requests
            http = requests
        self._pace()
        try:
            r = http.get(url, headers={"User-Agent": self._ua,
                                       "Accept": "application/json"},
                         timeout=30)
        except Exception as e:
            raise DataUnavailable(f"SEC {what} failed: {e}") from e
        if r.status_code == 404:
            raise DataUnavailable(f"SEC has no {what} at {url}")
        if r.status_code != 200:
            raise DataUnavailable(f"SEC {what} HTTP {r.status_code}")
        return r.json()

    def shares_outstanding(self, symbol: str) -> Optional[FinancialPeriod]:
        """Latest reported common shares outstanding, as one small call.

        Returns None when the company does not report the cover-page
        concept, rather than guessing a share count.
        """
        cik, _name = self._cik_for(symbol)
        payload = self._get(
            CONCEPT_URL.format(cik=cik, taxonomy="dei",
                               concept="EntityCommonStockSharesOutstanding"),
            "companyconcept")
        rows = [r for r in ((payload or {}).get("units") or {}).get("shares")
                or [] if r.get("val") is not None and r.get("end")]
        if not rows:
            return None
        row = max(rows, key=lambda r: (r.get("end"), r.get("filed") or ""))
        retrieved = datetime.fromtimestamp(
            self._wall(), timezone.utc).isoformat(timespec="seconds")
        return FinancialPeriod(
            concept="shares_outstanding", value=float(row["val"]),
            unit="shares", period_start=None, period_end=row["end"],
            fiscal_year=row.get("fy"), fiscal_period=row.get("fp"),
            form=row.get("form"), filed=row.get("filed"),
            accession=row.get("accn"),
            provenance=Provenance(
                "sec_companyfacts",
                "companyconcept#dei:EntityCommonStockSharesOutstanding",
                retrieved, period=row["end"]))

    def fetch(self, symbol: str) -> Dict[str, List[FinancialPeriod]]:
        cik, _name = self._cik_for(symbol)
        url = URL.format(cik=cik)
        http = self._http
        if http is None:
            import requests
            http = requests
        self._pace()
        try:
            r = http.get(url, headers={"User-Agent": self._ua,
                                       "Accept": "application/json"},
                         timeout=30)
        except Exception as e:
            raise DataUnavailable(f"SEC companyfacts failed: {e}") from e
        if r.status_code == 404:
            raise DataUnavailable(f"no XBRL company facts for {symbol}")
        if r.status_code != 200:
            raise DataUnavailable(f"SEC companyfacts HTTP {r.status_code}")
        retrieved = datetime.fromtimestamp(
            self._wall(), timezone.utc).isoformat(timespec="seconds")
        return normalise(r.json(), retrieved, url)
