"""
Persistence for shadow comparisons.

One record per external paper order: what the internal simulator expected
and what the venue actually did. It lives under `autonomy`, not `broker`: these are observations about
execution quality rather than a way to execute, and the dev read API is
forbidden from importing any broker module - a read surface that can
reach a broker is one step from being able to submit.

Written by the cycle when an external venue is in use. Until then the
store is simply empty, and an empty store is reported as "no comparisons
recorded" rather than as agreement.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from typing import Dict, List, Optional


def summarise_shadow(rows: List[Dict]) -> Dict:
    """Aggregate the comparison, with n and without a verdict.

    No judgement on the simulator's quality: a handful of fills cannot
    establish that it models the venue, so the sample size is reported
    and the conclusion is left open.
    """
    rows = [r for r in (rows or []) if r]
    paired = [r for r in rows
              if isinstance(r.get("internal_fill_price"), (int, float))
              and isinstance(r.get("broker_fill_price"), (int, float))]
    out = {
        "comparisons": len(rows),
        "both_filled": len(paired),
        "broker_rejections": sum(
            1 for r in rows if r.get("broker_reject_reason")),
        "internal_rejections": sum(
            1 for r in rows if r.get("internal_reject_reason")),
        "partial_fills": sum(
            1 for r in rows
            if isinstance(r.get("broker_fill_quantity"), (int, float))
            and isinstance(r.get("requested_quantity"), (int, float))
            and r["broker_fill_quantity"] < r["requested_quantity"]),
        "duplicate_broker_submissions": sum(
            1 for r in rows if (r.get("submitted_to_broker_count") or 0) > 1),
    }
    if paired:
        diffs = [r["broker_fill_price"] - r["internal_fill_price"]
                 for r in paired]
        out["mean_price_difference"] = round(sum(diffs) / len(diffs), 6)
        out["max_abs_price_difference"] = round(
            max(abs(d) for d in diffs), 6)
    else:
        out["mean_price_difference"] = None
        out["max_abs_price_difference"] = None
    out["verdict"] = (
        "INSUFFICIENT_SAMPLE" if len(paired) < 20 else "SAMPLE_ADEQUATE")
    out["note"] = ("A difference between the simulator and the venue is "
                   "information about the simulator, not about the "
                   "strategy. No conclusion is drawn below 20 paired "
                   "fills.")
    return out


class ShadowStore:
    def put(self, session_date: str, record) -> None:
        raise NotImplementedError

    def for_session(self, session_date: str) -> List[Dict]:
        raise NotImplementedError


def _as_dict(record) -> Dict:
    if is_dataclass(record):
        return asdict(record)
    if hasattr(record, "as_dict"):
        return record.as_dict()
    return dict(record)


class InMemoryShadowStore(ShadowStore):
    def __init__(self):
        self._rows: Dict[str, List[Dict]] = {}

    def put(self, session_date, record):
        self._rows.setdefault(session_date, []).append(_as_dict(record))

    def for_session(self, session_date):
        return list(self._rows.get(session_date, []))


class DynamoDBShadowStore(ShadowStore):
    """PK = SHADOW#<session_date>, SK = the client order id.

    The client order id is the key, so a retry of the same order cannot
    create a second comparison row for one order.
    """

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

    def put(self, session_date, record):
        row = _as_dict(record)
        key = row.get("client_order_id") or row.get("symbol") or "unknown"
        self.client.put_item(
            TableName=self.table_name,
            Item={"PK": {"S": f"SHADOW#{session_date}"},
                  "SK": {"S": str(key)},
                  "payload": {"S": json.dumps(row, default=str)}})

    def for_session(self, session_date):
        out: List[Dict] = []
        token = None
        while True:
            kwargs = dict(
                TableName=self.table_name,
                KeyConditionExpression="PK = :p",
                ExpressionAttributeValues={
                    ":p": {"S": f"SHADOW#{session_date}"}},
                ConsistentRead=True)
            if token:
                kwargs["ExclusiveStartKey"] = token
            response = self.client.query(**kwargs)
            out.extend(json.loads(i["payload"]["S"])
                       for i in response.get("Items", []))
            token = response.get("LastEvaluatedKey")
            if not token:
                return out
