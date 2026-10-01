"""
Alpaca corporate-actions provider (data API, /v1/corporate-actions).

Uses the existing AlpacaProvider plumbing (headers, error mapping,
pacing counters). The deprecated "announcements" endpoint is NOT used.

Raw payload keys are mapped into CorporateAction here and nowhere else.
Dates the provider did not supply stay None. Unknown action buckets are
kept as OTHER with their raw fields in `detail` rather than dropped.
"""
from __future__ import annotations

import time
from typing import Dict, Iterable, List, Optional

from agent.providers.alpaca import AlpacaProvider
from agent.providers.base import DataUnavailable
from ..models import ActionType, CorporateAction, Provenance

PATH = "/v1/corporate-actions"
MAX_PAGES = 20

_BUCKETS = {
    "cash_dividends": ActionType.CASH_DIVIDEND,
    "stock_dividends": ActionType.STOCK_DIVIDEND,
    "forward_splits": ActionType.FORWARD_SPLIT,
    "reverse_splits": ActionType.REVERSE_SPLIT,
    "unit_splits": ActionType.UNIT_SPLIT,
    "mergers": ActionType.MERGER,
    "spin_offs": ActionType.SPIN_OFF,
    "name_changes": ActionType.NAME_CHANGE,
    "symbol_changes": ActionType.SYMBOL_CHANGE,
    "redemptions": ActionType.REDEMPTION,
    "rights_distributions": ActionType.RIGHTS_DISTRIBUTION,
    "worthless_removals": ActionType.WORTHLESS_REMOVAL,
}


def _s(v) -> Optional[str]:
    """Empty strings and None are 'not supplied'."""
    if v is None or v == "":
        return None
    return str(v)


def _f(v) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def normalise(bucket: str, row: Dict, symbol_hint: Optional[str],
              prov: Provenance) -> CorporateAction:
    typ = _BUCKETS.get(bucket, ActionType.OTHER)
    symbol = (_s(row.get("symbol")) or _s(row.get("source_symbol"))
              or _s(row.get("old_symbol")) or symbol_hint or "")
    detail = {}
    if typ is ActionType.OTHER:
        detail = {"bucket": bucket, "raw": dict(row)}
    common = dict(
        symbol=symbol, event_id=_s(row.get("id")),
        announcement_date=_s(row.get("declaration_date")),
        ex_date=_s(row.get("ex_date")),
        record_date=_s(row.get("record_date")),
        payable_date=_s(row.get("payable_date")),
        effective_date=_s(row.get("effective_date")),
        detail=detail, provenance=prov)
    if typ is ActionType.CASH_DIVIDEND:
        return CorporateAction(
            type=typ, amount=_f(row.get("rate")),
            currency=_s(row.get("currency")) or "USD",
            special=bool(row.get("special")), **common)
    if typ is ActionType.STOCK_DIVIDEND:
        return CorporateAction(type=typ, amount=_f(row.get("rate")),
                               **common)
    if typ in (ActionType.FORWARD_SPLIT, ActionType.REVERSE_SPLIT,
               ActionType.UNIT_SPLIT):
        new, old = _f(row.get("new_rate")), _f(row.get("old_rate"))
        # Direction follows the ratio, not the bucket name, when both are
        # present; a mislabelled bucket must not flip the meaning.
        if new and old and typ is not ActionType.UNIT_SPLIT:
            typ = (ActionType.REVERSE_SPLIT if new < old
                   else ActionType.FORWARD_SPLIT)
        return CorporateAction(type=typ, ratio_new=new, ratio_old=old,
                               **common)
    if typ is ActionType.SPIN_OFF:
        return CorporateAction(
            type=typ, related_symbol=_s(row.get("new_symbol")),
            ratio_new=_f(row.get("new_rate")),
            ratio_old=_f(row.get("source_rate")), **common)
    if typ is ActionType.MERGER:
        return CorporateAction(
            type=typ, related_symbol=_s(row.get("acquirer_symbol")),
            ratio_new=_f(row.get("acquirer_rate")),
            ratio_old=_f(row.get("acquiree_rate")),
            amount=_f(row.get("cash_rate")), **common)
    if typ in (ActionType.NAME_CHANGE, ActionType.SYMBOL_CHANGE):
        return CorporateAction(type=typ,
                               related_symbol=_s(row.get("new_symbol")),
                               **common)
    return CorporateAction(type=typ, amount=_f(row.get("rate")), **common)


class AlpacaCorporateActions:
    name = "alpaca_corporate_actions"

    def __init__(self, alpaca: AlpacaProvider, wall_clock=time.time):
        self._alpaca = alpaca
        self._clock = wall_clock

    def _prov(self, start: str, end: str) -> Provenance:
        return Provenance(
            provider="alpaca", source=f"{PATH}",
            retrieved_at=_iso(self._clock()), period=f"{start}..{end}")

    def fetch(self, symbol: str, start: str, end: str,
              types: Optional[Iterable[str]] = None) -> List[CorporateAction]:
        """All actions for one symbol in [start, end], following pages.

        A page cap that is hit raises rather than returning a silently
        truncated history.
        """
        prov = self._prov(start, end)
        params = {"symbols": symbol, "start": start, "end": end,
                  "limit": 1000}
        if types:
            params["types"] = ",".join(types)
        out: List[CorporateAction] = []
        token = None
        for _ in range(MAX_PAGES):
            if token:
                params["page_token"] = token
            body = self._alpaca._get(self._alpaca.data_base, PATH, params)
            if not isinstance(body, dict):
                raise DataUnavailable("corporate-actions: unexpected body")
            actions = body.get("corporate_actions") or {}
            for bucket, rows in actions.items():
                for row in rows or []:
                    out.append(normalise(bucket, row, symbol, prov))
            token = body.get("next_page_token")
            if not token:
                return out
        raise DataUnavailable(
            f"corporate-actions for {symbol}: page cap hit; history would "
            "be truncated")


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds")


# -- bridges into the first-slice dividend/split models ----------------------

def to_dividend_events(actions: Iterable[CorporateAction]):
    from ..models import DividendEvent
    return [DividendEvent(
        ex_date=a.ex_date, amount=a.amount or 0.0,
        currency=a.currency or "USD", pay_date=a.payable_date,
        record_date=a.record_date, declaration_date=a.announcement_date,
        kind="SPECIAL" if a.special else "REGULAR", provenance=a.provenance)
        for a in actions
        if a.type is ActionType.CASH_DIVIDEND and a.ex_date
        and a.amount is not None]


def to_split_events(actions: Iterable[CorporateAction]):
    from ..models import SplitEvent
    return [SplitEvent(ex_date=a.ex_date, new_rate=a.ratio_new,
                       old_rate=a.ratio_old, provenance=a.provenance)
            for a in actions
            if a.type in (ActionType.FORWARD_SPLIT, ActionType.REVERSE_SPLIT)
            and a.ex_date and a.ratio_new and a.ratio_old]
