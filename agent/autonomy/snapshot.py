"""
The latest cycle, kept where the dashboard and chat can read it.

The cycle Lambda is stateless, so "what is the agent doing right now"
has to be something it WRITES, not something a reader reconstructs.
Persisting a trimmed copy of the last cycle result lets the dashboard
and the chat answer from a stored fact instead of from CloudWatch.

Best effort on the write side: failing to record this must never be a
reason a cycle fails.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional

# Fields that make the snapshot large and are not needed to answer
# "what is it doing". Steps are kept - they say which half of the cycle
# ran - but the heavy payloads are dropped.
_DROP = {"traceback", "account", "daily", "market", "state_sync"}


def trim(payload: Dict) -> Dict:
    keep = {k: v for k, v in payload.items() if k not in _DROP}
    keep["account_cash"] = (payload.get("account") or {}).get("cash")
    keep["market_session"] = (payload.get("market") or {}).get("session")
    keep["minutes_to_close"] = (payload.get("market") or {}).get(
        "minutes_to_close")
    keep["daily"] = payload.get("daily")
    return keep


class SnapshotStore:
    def put(self, payload: Dict) -> None:
        raise NotImplementedError

    def get(self) -> Optional[Dict]:
        raise NotImplementedError


class InMemorySnapshotStore(SnapshotStore):
    def __init__(self):
        self._value: Optional[Dict] = None

    def put(self, payload):
        self._value = json.loads(json.dumps(trim(payload), default=str))

    def get(self):
        return self._value


class DynamoDBSnapshotStore(SnapshotStore):
    """PK = CONTROL#LAST_CYCLE, SK = LATEST."""

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

    def put(self, payload):
        self.client.put_item(
            TableName=self.table_name,
            Item={"PK": {"S": "CONTROL#LAST_CYCLE"}, "SK": {"S": "LATEST"},
                  "payload": {"S": json.dumps(trim(payload), default=str)}})

    def get(self):
        r = self.client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": "CONTROL#LAST_CYCLE"}, "SK": {"S": "LATEST"}},
            ConsistentRead=True)
        item = r.get("Item")
        return json.loads(item["payload"]["S"]) if item else None
