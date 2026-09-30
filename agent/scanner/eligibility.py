"""
Eligibility filters.

Two stages, split by what they cost:

* **static** — reference data only, no market data, so it can run over the
  whole 14k-asset universe for free.
* **dynamic** — needs a live snapshot (price, volume, spread, freshness),
  so it runs after the cheap stage has cut the list down.

Every rejection returns machine-readable reasons. A symbol is never
silently dropped: without the reason counts there is no way to tell a
scanner that is being appropriately selective from one that is quietly
broken.

All checks are collected rather than short-circuited. Knowing a symbol
failed on *both* price and spread is more useful than knowing it failed
the first test.
"""
from __future__ import annotations

from typing import List, Tuple

from ..config import UniverseConfig
from .models import (
    DataFreshness, RejectionReason, ScannerSnapshot, SecurityType,
    UniverseSecurity,
)

# A price floor is not the same rule as "no penny stocks": some venues
# treat anything under $5 as a penny stock regardless of the configured
# floor, so the two are reported separately.
PENNY_STOCK_CEILING = 5.00


def static_eligibility(security: UniverseSecurity, policy: UniverseConfig
                       ) -> Tuple[bool, List[str]]:
    """Reference-data checks. No market data, no provider calls."""
    reasons: List[str] = []

    if security.status != "active":
        reasons.append(RejectionReason.INACTIVE.value)

    if not security.tradable:
        reasons.append(RejectionReason.NOT_TRADABLE.value)

    if security.asset_class not in policy.allowed_asset_classes:
        reasons.append(RejectionReason.UNSUPPORTED_ASSET_CLASS.value)

    if security.exchange == "OTC":
        if not policy.allow_otc:
            reasons.append(RejectionReason.OTC_EXCLUDED.value)
    elif security.exchange not in policy.allowed_exchanges:
        reasons.append(RejectionReason.UNSUPPORTED_EXCHANGE.value)

    if security.security_type is SecurityType.ETF and not policy.allow_etfs:
        reasons.append(RejectionReason.ETF_EXCLUDED.value)
    if security.security_type is SecurityType.EQUITY and not policy.allow_equities:
        reasons.append(RejectionReason.EQUITY_EXCLUDED.value)
    if security.security_type is SecurityType.UNKNOWN:
        # Unknown is not benign: a security we cannot classify cannot be
        # checked against the type policy at all.
        reasons.append(RejectionReason.UNKNOWN_SECURITY_TYPE.value)

    if security.leveraged and not policy.allow_leveraged_etfs:
        reasons.append(RejectionReason.LEVERAGED_ETF_EXCLUDED.value)

    if policy.require_fractionable and not security.fractionable:
        reasons.append(RejectionReason.NOT_FRACTIONABLE.value)

    return (not reasons), reasons


def dynamic_eligibility(snapshot: ScannerSnapshot, policy: UniverseConfig,
                        spread_multiplier: float = 1.0,
                        dollar_volume_multiplier: float = 1.0,
                        allow_stale: bool = False) -> Tuple[bool, List[str]]:
    """Market-data checks against a live snapshot.

    The multipliers let the regime gate tighten limits without this
    function knowing anything about regimes.
    """
    reasons: List[str] = []

    if snapshot.error or snapshot.price is None:
        reasons.append(RejectionReason.MISSING_QUOTE.value)
        return False, reasons          # nothing else can be evaluated

    if snapshot.freshness is DataFreshness.MISSING:
        reasons.append(RejectionReason.MISSING_QUOTE.value)
    elif snapshot.freshness is DataFreshness.STALE and not allow_stale:
        reasons.append(RejectionReason.STALE_QUOTE.value)

    price = snapshot.price
    if price < policy.min_price:
        reasons.append(RejectionReason.PRICE_BELOW_MINIMUM.value)
    if price > policy.max_price:
        reasons.append(RejectionReason.PRICE_ABOVE_MAXIMUM.value)
    if price < PENNY_STOCK_CEILING and not policy.allow_penny_stocks:
        reasons.append(RejectionReason.PENNY_STOCK_EXCLUDED.value)

    if snapshot.volume is None:
        reasons.append(RejectionReason.MISSING_BAR_DATA.value)

    dollar_volume = snapshot.dollar_volume
    if dollar_volume is None:
        reasons.append(RejectionReason.MISSING_BAR_DATA.value)
    elif dollar_volume < policy.min_dollar_volume * dollar_volume_multiplier:
        reasons.append(RejectionReason.DOLLAR_VOLUME_BELOW_MINIMUM.value)

    # Average daily volume is optional at this stage: it needs multi-day
    # bars, which only the shortlist pays for. When present, enforce it.
    if snapshot.avg_daily_volume is not None and \
            snapshot.avg_daily_volume < policy.min_avg_daily_volume:
        reasons.append(RejectionReason.VOLUME_BELOW_MINIMUM.value)

    spread_pct = snapshot.spread_pct
    if spread_pct is None:
        # An unknown spread is not a tight one. Rejecting is the
        # conservative reading, and it is reported distinctly from a
        # spread that is known and too wide.
        reasons.append(RejectionReason.SPREAD_UNKNOWN.value)
    elif spread_pct > policy.max_spread_pct * spread_multiplier:
        reasons.append(RejectionReason.SPREAD_TOO_WIDE.value)

    return (not reasons), sorted(set(reasons))


def classify_freshness(age_seconds, max_age_seconds: float,
                       fresh_max_age: float = 300.0) -> DataFreshness:
    """Age is measured from the PROVIDER's event time, never from the
    cache read."""
    if age_seconds is None:
        return DataFreshness.MISSING
    if age_seconds > max_age_seconds:
        return DataFreshness.MISSING       # too old to be usable at all
    if age_seconds > fresh_max_age:
        return DataFreshness.STALE
    return DataFreshness.FRESH
