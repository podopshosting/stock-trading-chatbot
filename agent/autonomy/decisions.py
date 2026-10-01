"""
The decision log.

Every candidate the agent evaluates leaves a row, including the ones it
declined. "Why did you not trade AAPL" has to be answerable from the
record, and an agent that logs only what it did cannot explain what it
chose not to do.

Chat grounds itself in these rows. The LLM explains stored decisions;
it does not rewrite them, so the row - not a regenerated rationale - is
the authority on what happened and why.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DecisionLog:
    def record(self, row: Dict) -> Dict:
        raise NotImplementedError

    def for_session(self, session_date: str, symbol: Optional[str] = None,
                    limit: int = 500) -> List[Dict]:
        raise NotImplementedError


class InMemoryDecisionLog(DecisionLog):
    def __init__(self):
        self._rows: List[Dict] = []

    def record(self, row: Dict) -> Dict:
        row = dict(row)
        row.setdefault("decided_at", _now())
        row.setdefault("decision_row_id", uuid.uuid4().hex[:12])
        self._rows.append(row)
        return row

    def for_session(self, session_date: str, symbol: Optional[str] = None,
                    limit: int = 500) -> List[Dict]:
        rows = [r for r in self._rows
                if r.get("session_date") == session_date
                and (symbol is None or r.get("symbol") == symbol)]
        return rows[-limit:]


class DynamoDBDecisionLog(DecisionLog):
    """PK = DECISIONS#<session_date>, SK = <time>#<symbol>#<id>."""

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

    def record(self, row: Dict) -> Dict:
        row = dict(row)
        row.setdefault("decided_at", _now())
        row.setdefault("decision_row_id", uuid.uuid4().hex[:12])
        self.client.put_item(
            TableName=self.table_name,
            Item={"PK": {"S": f"DECISIONS#{row['session_date']}"},
                  "SK": {"S": f"{row['decided_at']}#{row['symbol']}#"
                              f"{row['decision_row_id']}"},
                  "symbol": {"S": row["symbol"]},
                  "outcome": {"S": row["outcome"]},
                  "payload": {"S": json.dumps(row, default=str)}})
        return row

    def for_session(self, session_date: str, symbol: Optional[str] = None,
                    limit: int = 500) -> List[Dict]:
        response = self.client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={
                ":pk": {"S": f"DECISIONS#{session_date}"}},
            Limit=limit)
        rows = [json.loads(i["payload"]["S"])
                for i in response.get("Items", [])]
        if symbol:
            rows = [r for r in rows if r.get("symbol") == symbol]
        return rows
