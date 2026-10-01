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
}


def normalise(payload: Dict, retrieved_at: str,
              source: str) -> Dict[str, List[FinancialPeriod]]:
    """Every observation of every preferred concept, as FinancialPeriod.

    A concept that exists under a higher-preference name wins whole: we
    never splice two XBRL concepts into one series.
    """
    facts = (payload or {}).get("facts") or {}
    out: Dict[str, List[FinancialPeriod]] = {}
    for canon, (tax, names, unit) in CONCEPTS.items():
        for name in names:
            node = (facts.get(tax) or {}).get(name)
            rows = ((node or {}).get("units") or {}).get(unit) or []
            if not rows:
                continue
            periods = []
            for r in rows:
                if r.get("val") is None or not r.get("end"):
                    continue
                fy = r.get("fy")
                periods.append(FinancialPeriod(
                    concept=canon, value=float(r["val"]), unit=unit,
                    period_start=r.get("start"), period_end=r["end"],
                    fiscal_year=int(fy) if fy is not None else None,
                    fiscal_period=r.get("fp"), form=r.get("form"),
                    filed=r.get("filed"), accession=r.get("accn"),
                    provenance=Provenance(
                        "sec_companyfacts", f"{source}#{name}", retrieved_at,
                        period=r["end"])))
            if periods:
                out[canon] = periods
                break
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

    def _pace(self):
        now = self._clock()
        if self._last is not None and now - self._last < MIN_INTERVAL:
            self._sleep(MIN_INTERVAL - (now - self._last))
        self._last = self._clock()

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
