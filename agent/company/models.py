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
