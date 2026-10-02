"""
A READ-ONLY view of the external order ledger, for the read API.

The read API may not import anything from `agent/broker/` at all. That
is not a stylistic rule: two tests enforce it textually, because the
submission path lives there and an API that cannot reach it cannot
submit. Importing even a harmless record store would have put the whole
package within reach, and a per-module exception would have turned an
absolute boundary into a judgement call about which modules are safe.

So this reads the same rows and does its own arithmetic. That is
duplication, and duplication drifts - so
`tests/test_order_view.py::TestTheTwoViewsCannotDrift` compares this
against `agent/broker/order_ledger.py` on shared fixtures, the same way
the two client-order-id derivations are pinned against each other.

Reads RAISE. An unreadable ledger must not look like an empty one.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

# The partition key written by agent/broker/order_ledger.py. Pinned by a
# test against that module's source, because a reader looking in the
# wrong partition would report zero outstanding orders rather than
# failing - and zero is the one answer that would be acted on.
PARTITION_PREFIX = "EXTORDERS#"

TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED",
                      "NEVER_PLACED"})


class OrderViewError(Exception):
    """The ledger could not be read. Exposure is UNKNOWN, not zero."""


def is_terminal(row: Dict) -> bool:
    """An unrecognised status is NOT terminal: a live order is exposure."""
    return str(row.get("status") or "") in TERMINAL


def is_settled(row: Dict) -> bool:
    return is_terminal(row) and bool(row.get("submission_outcome_known"))


def potential_exposure(row: Dict) -> Optional[float]:
    """The most this order could still cost, conservatively.

    None means it cannot be established, which a caller must treat as a
    reason to refuse new exposure rather than as zero.
    """
    if is_settled(row):
        return 0.0
    notional = row.get("requested_notional")
    filled = float(row.get("filled_quantity") or 0.0)
    avg = row.get("average_fill_price")
    if notional is not None:
        return max(0.0, float(notional) - filled * float(avg or 0.0))
    quantity = row.get("requested_quantity")
    if quantity is not None and avg is not None:
        return max(0.0, float(quantity) - filled) * float(avg)
    return None


def _age_seconds(row: Dict, now: Optional[datetime] = None) -> Optional[float]:
    stamp = row.get("submitted_at") or row.get("intent_at")
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - when).total_seconds()


def summarise(rows: List[Dict], now: Optional[datetime] = None) -> Dict:
    """Counts and exposure, with the unknowns kept as unknowns."""
    rows = list(rows or [])
    outstanding = [r for r in rows if not is_settled(r)]
    unestablished, total = [], 0.0
    for row in outstanding:
        value = potential_exposure(row)
        if value is None:
            unestablished.append(row.get("client_order_id"))
            continue
        total += value
    ages = [a for a in (_age_seconds(r, now) for r in outstanding)
            if a is not None]
    known = not unestablished
    return {
        "total_recorded": len(rows),
        "outstanding": len(outstanding),
        "outstanding_ids": [r.get("client_order_id") for r in outstanding],
        "unresolved_submissions": len(
            [r for r in rows if not r.get("submission_outcome_known")]),
        "unresolved_ids": [r.get("client_order_id") for r in rows
                           if not r.get("submission_outcome_known")],
        "partial_fills": len(
            [r for r in rows
             if float(r.get("filled_quantity") or 0) > 0
             and not is_terminal(r)]),
        "partial_fill_ids": [
            r.get("client_order_id") for r in rows
            if float(r.get("filled_quantity") or 0) > 0
            and not is_terminal(r)],
        "oldest_pending_age_seconds": round(max(ages), 1) if ages else None,
        # null, never 0.0, when it cannot be established.
        "committed_exposure": round(total, 6) if known else None,
        "committed_exposure_known": known,
        "exposure_unestablished_for": unestablished,
        "by_intent": {
            intent: len([r for r in rows if r.get("intent") == intent])
            for intent in sorted({r.get("intent") for r in rows
                                  if r.get("intent")})},
    }


class OrderLedgerView:
    """Reads the ledger's partition for one session. Never writes."""

    def __init__(self, table_name: Optional[str] = None, client=None):
        self.table_name = table_name or os.environ.get(
            "AGENT_JOURNAL_TABLE", "stock-agent-dev-journal")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def for_session(self, session_date: str) -> List[Dict]:
        try:
            paged = self.client.query(
                TableName=self.table_name,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={
                    ":pk": {"S": f"{PARTITION_PREFIX}{session_date}"}})
        except Exception as exc:                          # noqa: BLE001
            raise OrderViewError(
                f"order ledger unreadable: {type(exc).__name__}") from None
        rows = []
        for item in paged.get("Items", []):
            payload = (item.get("payload") or {}).get("S")
            if not payload:
                # A row that cannot be parsed makes the whole read
                # untrustworthy: a partial ledger cannot establish
                # exposure at all.
                raise OrderViewError(
                    "an order ledger row has no payload; a partial read "
                    "cannot establish exposure")
            try:
                rows.append(json.loads(payload))
            except (TypeError, ValueError) as exc:
                raise OrderViewError(
                    f"an order ledger row is malformed: {exc}") from None
        return rows
