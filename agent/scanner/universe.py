"""
Universe providers: the list of securities the scanner may consider.

Reference data is normalised into `UniverseSecurity` here, so eligibility
rules are never written against a raw provider payload. If the provider
changes, this file changes and the policy does not.

Measured against the live API on 2026-09-30: `/v2/assets` returns **14,388
active us_equity assets in a single request** (~3s), carrying `exchange`,
`tradable`, `fractionable`, `shortable`, `status`, `name` and
`attributes`. It carries **no volume or price**, so liquidity is a
market-data question, not a reference-data one.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..observability import log_event
from .models import SecurityType, UniverseSecurity

# ETFs list mainly on ARCA, BATS and AMEX, but the exchange alone is not
# decisive, so the name is used as well.
_ETF_EXCHANGES = {"ARCA", "BATS", "AMEX"}

_ETF_NAME_PATTERNS = (
    " etf", "exchange traded", "exchange-traded", " etn", " fund",
    " trust", " index", " shares", " portfolio",
)

# Leveraged and inverse products, matched on the fund name.
#
# Name matching is a HEURISTIC with a real precision ceiling, so two
# things keep it honest:
#
# 1. It is applied ONLY to securities classified as ETFs. The pattern hits
#    12 operating companies in the live universe - "Ultra Clean Holdings",
#    "10x Genomics", "Ultragenyx" - and excluding those would be wrong.
#    Leveraged and inverse products are funds, so gating on ETF removes
#    that entire class of false positive.
#
# 2. Where it is still ambiguous it errs toward exclusion. Admitting a 3x
#    inverse product into a long-only funnel is a real problem; dropping a
#    short-duration bond fund that would never rank anyway is not.
#
# The hard case is "Short". "ProShares Short S&P500" is inverse;
# "Schwab Short-Term U.S. Treasury ETF" and "PIMCO Enhanced Short
# Maturity" are ordinary bond funds, and a naive \bshort\b match would
# have excluded 217 legitimate funds. The negative lookahead separates
# them.
#
# "Ultra" needs a PREFIX match, not \bultra\b: the word boundary fails
# on "UltraShort" and "UltraPro", which let SDS, QID, TQQQ, UPRO and UDOW
# through on the first attempt.
#
# Validated against the live universe: 0 false negatives across 22 known
# leveraged/inverse products, 0 false positives across 20 ordinary ones.
_LEVERAGED_PATTERNS = (
    r"\bultra\w*",                        # Ultra, UltraShort, UltraPro
    r"\b\d+(?:\.\d+)?\s*x\b",            # 2x, 3X, 1.5x
    r"-\s*\d+(?:\.\d+)?\s*x\b",          # -1x
    r"\binverse\b",
    r"\bbear\b",
    r"\bleveraged\b",
    r"\b(?:2|3)\s*times\b",
    r"\bshort\b(?!\s*(?:-?\s*term|duration|maturity|dated))",
)
_LEVERAGED_RE = re.compile("|".join(_LEVERAGED_PATTERNS), re.IGNORECASE)


def classify_security_type(name: str, exchange: str) -> SecurityType:
    lowered = (name or "").lower()
    if any(pattern in lowered for pattern in _ETF_NAME_PATTERNS):
        return SecurityType.ETF
    if exchange in _ETF_EXCHANGES and "common stock" not in lowered:
        return SecurityType.ETF
    if "common stock" in lowered or "ordinary share" in lowered:
        return SecurityType.EQUITY
    if exchange in ("NASDAQ", "NYSE"):
        return SecurityType.EQUITY
    return SecurityType.UNKNOWN


def looks_leveraged(name: str, security_type: Optional[SecurityType] = None) -> bool:
    """Does this look like a leveraged or inverse product?

    When `security_type` is supplied, only ETFs are tested: the pattern
    matches real operating companies ("Ultra Clean Holdings") that must
    not be excluded.
    """
    if security_type is not None and security_type is not SecurityType.ETF:
        return False
    return bool(_LEVERAGED_RE.search(name or ""))


class UniverseProvider(ABC):
    @abstractmethod
    def list_symbols(self) -> List[UniverseSecurity]:
        ...

    def request_count(self) -> int:
        return 0


class StaticUniverseProvider(UniverseProvider):
    """Fixed list, for tests and for pinning a universe during development."""

    def __init__(self, securities: List[UniverseSecurity]):
        self._securities = list(securities)

    def list_symbols(self) -> List[UniverseSecurity]:
        return list(self._securities)


class AlpacaUniverseProvider(UniverseProvider):
    """Normalises Alpaca `/v2/assets` into `UniverseSecurity` records."""

    def __init__(self, provider, asset_class: str = "us_equity"):
        self.provider = provider
        self.asset_class = asset_class
        self._calls = 0

    def request_count(self) -> int:
        return self._calls

    def list_symbols(self) -> List[UniverseSecurity]:
        try:
            raw = self.provider.get_assets(status="active",
                                           asset_class=self.asset_class)
            self._calls += 1
        except Exception as e:
            log_event("provider_error", operation="get_assets",
                      error=str(e)[:200])
            raise

        out: List[UniverseSecurity] = []
        for row in raw or []:
            security = self._normalise(row)
            if security is not None:
                out.append(security)

        log_event("universe_loaded", source="alpaca",
                  raw_count=len(raw or []), normalised_count=len(out))
        return out

    @staticmethod
    def _normalise(row: Dict) -> Optional[UniverseSecurity]:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        # Pair/compound symbols are not ordinary single securities.
        if "/" in symbol or " " in symbol:
            return None

        name = row.get("name") or ""
        exchange = (row.get("exchange") or "").strip().upper()

        return UniverseSecurity(
            symbol=symbol,
            name=name,
            exchange=exchange,
            asset_class=row.get("class") or row.get("asset_class") or "",
            security_type=classify_security_type(name, exchange),
            tradable=bool(row.get("tradable")),
            fractionable=bool(row.get("fractionable")),
            shortable=bool(row.get("shortable")),
            status=(row.get("status") or "").strip().lower() or "unknown",
            leveraged=looks_leveraged(
                name, classify_security_type(name, exchange)),
            attributes=tuple(row.get("attributes") or ()),
        )
