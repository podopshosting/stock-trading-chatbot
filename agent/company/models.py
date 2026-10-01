"""
Company intelligence models.

Every fact carries provenance (provider, source, period, retrieved_at).
A value that is not known is UNKNOWN / None, never a guess. An LLM is
never a source. Nothing here is a trading signal and nothing here
composes a score.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class DividendStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DISCONTINUED = "DISCONTINUED"
    IRREGULAR = "IRREGULAR"
    NO_DIVIDEND = "NO_DIVIDEND"
    UNKNOWN = "UNKNOWN"

    def __str__(self):
        return self.value


class SplitType(str, enum.Enum):
    FORWARD_SPLIT = "FORWARD_SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    UNIT_SPLIT = "UNIT_SPLIT"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"

    def __str__(self):
        return self.value


class HoldingContextLabel(str, enum.Enum):
    FAVORABLE = "FAVORABLE"
    NEUTRAL = "NEUTRAL"
    CAUTIOUS = "CAUTIOUS"
    UNFAVORABLE = "UNFAVORABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class Provenance:
    provider: str
    source: str                      # API endpoint or filing id
    retrieved_at: str                # ISO timestamp
    period: Optional[str] = None     # fiscal period / as-of date

    def as_dict(self) -> Dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DividendEvent:
    ex_date: str
    amount: float
    currency: str = "USD"
    pay_date: Optional[str] = None
    record_date: Optional[str] = None
    declaration_date: Optional[str] = None
    kind: str = "REGULAR"            # REGULAR | SPECIAL
    provenance: Optional[Provenance] = None


@dataclass(frozen=True)
class SplitEvent:
    ex_date: str
    new_rate: float                  # shares after
    old_rate: float                  # shares before
    provenance: Optional[Provenance] = None

    @property
    def ratio(self) -> float:
        return self.new_rate / self.old_rate

    @property
    def type(self) -> SplitType:
        # Derived from the ratio, never from a label: a reverse split has
        # ratio < 1 and must not be reported as a forward split.
        return SplitType.REVERSE_SPLIT if self.ratio < 1 \
            else SplitType.FORWARD_SPLIT


@dataclass
class DividendProfile:
    status: DividendStatus
    pays_dividend: Optional[bool]    # UI Yes/No; None = unknown
    reason: str
    last_regular: Optional[DividendEvent] = None
    next_ex_date: Optional[str] = None
    days_until_ex: Optional[int] = None
    # Why there is no date, when there is none. A bare null cannot
    # distinguish "none has been declared" from "we never looked", and
    # the two warrant different responses from a reader.
    next_ex_note: Optional[str] = None
    trailing_12m_amount: Optional[float] = None
    trailing_yield_pct: Optional[float] = None
    yield_price_basis: Optional[str] = None   # which price, as of when
    years_paid: Optional[int] = None
    provenance: List[Provenance] = field(default_factory=list)

    def as_dict(self) -> Dict:
        d = dict(self.__dict__)
        d["status"] = str(self.status)
        d["last_regular"] = (self.last_regular.__dict__
                             if self.last_regular else None)
        d["provenance"] = [p.as_dict() for p in self.provenance]
        return d


# ---------------------------------------------------------------------------
# Corporate actions (canonical; no provider payload semantics leak out)
# ---------------------------------------------------------------------------

class ActionType(str, enum.Enum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    FORWARD_SPLIT = "FORWARD_SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    UNIT_SPLIT = "UNIT_SPLIT"
    MERGER = "MERGER"
    SPIN_OFF = "SPIN_OFF"
    NAME_CHANGE = "NAME_CHANGE"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    REDEMPTION = "REDEMPTION"
    RIGHTS_DISTRIBUTION = "RIGHTS_DISTRIBUTION"
    WORTHLESS_REMOVAL = "WORTHLESS_REMOVAL"
    OTHER = "OTHER"

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class CorporateAction:
    """Unknown dates stay None; nothing here is inferred."""
    type: ActionType
    symbol: str
    event_id: Optional[str] = None
    announcement_date: Optional[str] = None
    ex_date: Optional[str] = None
    record_date: Optional[str] = None
    payable_date: Optional[str] = None
    effective_date: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    ratio_new: Optional[float] = None
    ratio_old: Optional[float] = None
    related_symbol: Optional[str] = None     # merger/spin-off/rename target
    special: bool = False
    detail: Dict = field(default_factory=dict)
    provenance: Optional[Provenance] = None

    def as_dict(self) -> Dict:
        d = {k: v for k, v in self.__dict__.items()
             if k not in ("provenance", "type")}
        d["type"] = str(self.type)
        d["provenance"] = self.provenance.as_dict() if self.provenance else None
        return d


# ---------------------------------------------------------------------------
# Financials and earnings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FinancialPeriod:
    """One concept value for one explicit fiscal period."""
    concept: str                  # canonical name, e.g. "revenue"
    value: float
    unit: str
    period_start: Optional[str]   # None for instants (balance sheet)
    period_end: str
    fiscal_year: Optional[int]
    fiscal_period: Optional[str]  # FY, Q1..Q4
    form: Optional[str]           # 10-K, 10-Q, ...
    filed: Optional[str]
    accession: Optional[str] = None
    provenance: Optional[Provenance] = None

    @property
    def duration_days(self) -> Optional[int]:
        if not self.period_start:
            return None
        from datetime import date
        return (date.fromisoformat(self.period_end)
                - date.fromisoformat(self.period_start)).days


@dataclass(frozen=True)
class EarningsRecord:
    """Reported fact, consensus estimate and derived surprise are kept
    in separate fields and never exchanged."""
    period_end: str
    fiscal_period: Optional[str] = None
    report_date: Optional[str] = None
    report_timing: Optional[str] = None      # BMO / AMC / UNKNOWN
    eps_actual: Optional[float] = None       # REPORTED_VALUE
    eps_estimate: Optional[float] = None     # CONSENSUS_ESTIMATE
    revenue_actual: Optional[float] = None
    revenue_estimate: Optional[float] = None
    provenance: Optional[Provenance] = None

    @property
    def eps_surprise(self) -> Optional[float]:       # DERIVED_SURPRISE
        if self.eps_actual is None or self.eps_estimate is None:
            return None
        return round(self.eps_actual - self.eps_estimate, 6)

    @property
    def revenue_surprise(self) -> Optional[float]:
        if self.revenue_actual is None or self.revenue_estimate is None:
            return None
        return round(self.revenue_actual - self.revenue_estimate, 2)


@dataclass
class CompanyProfile:
    """Identity and classification, every field with its source. SIC comes
    from the SEC; sector/industry are derived from SIC by a fixed table, so
    classification is structured data and not an opinion."""
    symbol: str
    name: Optional[str] = None
    cik: Optional[int] = None
    sic: Optional[str] = None
    sic_description: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None
    active: Optional[bool] = None
    shares_outstanding: Optional[float] = None
    price: Optional[float] = None
    price_asof: Optional[str] = None
    market_cap: Optional[float] = None
    market_cap_basis: Optional[str] = None   # "shares X as of A x price Y as of B"
    provenance: List[Provenance] = field(default_factory=list)

    def as_dict(self) -> Dict:
        d = dict(self.__dict__)
        d["provenance"] = [p.as_dict() for p in self.provenance]
        return d
