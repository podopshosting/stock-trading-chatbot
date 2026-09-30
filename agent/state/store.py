"""
Persistence for agent session state.

Scheduled Lambdas do not share process memory, and two invocations can
overlap, so state lives in DynamoDB with an optimistic-concurrency check on
`revision`. A write built from a stale read is rejected rather than allowed
to silently overwrite a concurrent update - which is how an emergency stop
set by one invocation gets erased by another.

`InMemoryStateStore` exists so tests and local development need no AWS. It
is not a deployment target: it loses everything between invocations, which
for this system means losing the record of a halt.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Optional

from .models import AgentSession


class StateStoreError(Exception):
    pass


class ConcurrentUpdate(StateStoreError):
    """Another writer advanced the session since it was read."""

    def __init__(self, session_date: str, expected_revision: int):
        super().__init__(
            f"session {session_date} changed underneath us "
            f"(expected revision {expected_revision}); re-read and retry"
        )
        self.session_date = session_date
        self.expected_revision = expected_revision


def today_market_date(tz_name: str = "America/New_York") -> str:
    """Session date in market-local terms.

    Using UTC here would roll the trading day over at 20:00 ET, splitting a
    live session across two records.
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


class StateStore(ABC):
    @abstractmethod
    def get(self, session_date: str) -> Optional[AgentSession]:
        ...

    @abstractmethod
    def put(self, session: AgentSession, expected_revision: Optional[int] = None) -> None:
        ...

    def get_or_create(self, session_date: str,
                      defaults: Optional[Dict] = None) -> AgentSession:
        existing = self.get(session_date)
        if existing is not None:
            return existing
        session = AgentSession(session_date=session_date, **(defaults or {}))
        try:
            self.put(session, expected_revision=None)
        except ConcurrentUpdate:
            # Another invocation created it between our read and write.
            # Theirs wins; ours was never observed by anything.
            existing = self.get(session_date)
            if existing is None:
                raise
            return existing
        return session


class InMemoryStateStore(StateStore):
    """Process-local. For tests and local runs only."""

    def __init__(self):
        self._rows: Dict[str, Dict] = {}

    def get(self, session_date: str) -> Optional[AgentSession]:
        row = self._rows.get(session_date)
        return AgentSession.from_dict(json.loads(json.dumps(row))) if row else None

    def put(self, session: AgentSession,
            expected_revision: Optional[int] = None) -> None:
        current = self._rows.get(session.session_date)
        if expected_revision is None:
            if current is not None:
                raise ConcurrentUpdate(session.session_date, -1)
        else:
            if current is None:
                raise ConcurrentUpdate(session.session_date, expected_revision)
            if current.get("revision") != expected_revision:
                raise ConcurrentUpdate(session.session_date, expected_revision)
        self._rows[session.session_date] = json.loads(
            json.dumps(session.as_dict())
        )


class DynamoDBStateStore(StateStore):
    """DynamoDB-backed store.

    Table: partition key `session_date` (S). Conditional writes on
    `revision` provide optimistic concurrency.
    """

    def __init__(self, table_name: str = "stock-agent-dev-state",
                 region: str = "us-east-2", client=None):
        self.table_name = table_name
        self.region = region
        self._client = client

    def _get_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb", region_name=self.region)
        return self._client

    # DynamoDB's typed attributes are awkward for a nested document, and
    # this record is read and written whole, never queried by inner field.
    # Storing one JSON blob keeps the shape honest and the code small; the
    # queryable fields are duplicated as top-level attributes for
    # operators reading the console.
    def _to_item(self, session: AgentSession) -> Dict:
        d = session.as_dict()
        return {
            "session_date": {"S": session.session_date},
            "revision": {"N": str(session.revision)},
            "agent_state": {"S": d["agent_state"]},
            "market_regime": {"S": d["market_regime"]},
            "market_status": {"S": d["market_status"]},
            "trading_enabled": {"BOOL": bool(d["trading_enabled"])},
            "emergency_stop": {"BOOL": bool(d["emergency_stop"])},
            "updated_at": {"S": d["updated_at"]},
            "payload": {"S": json.dumps(d)},
        }

    def get(self, session_date: str) -> Optional[AgentSession]:
        try:
            resp = self._get_client().get_item(
                TableName=self.table_name,
                Key={"session_date": {"S": session_date}},
                ConsistentRead=True,      # a stale read here loses a halt
            )
        except Exception as e:
            raise StateStoreError(f"failed reading session {session_date}: {e}") from e

        item = resp.get("Item")
        if not item:
            return None
        try:
            return AgentSession.from_dict(json.loads(item["payload"]["S"]))
        except Exception as e:
            raise StateStoreError(
                f"session {session_date} is stored but unreadable: {e}"
            ) from e

    def put(self, session: AgentSession,
            expected_revision: Optional[int] = None) -> None:
        item = self._to_item(session)
        kwargs: Dict = {"TableName": self.table_name, "Item": item}

        if expected_revision is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(session_date)"
        else:
            kwargs["ConditionExpression"] = "revision = :expected"
            kwargs["ExpressionAttributeValues"] = {
                ":expected": {"N": str(expected_revision)}
            }

        try:
            self._get_client().put_item(**kwargs)
        except Exception as e:
            if type(e).__name__ == "ConditionalCheckFailedException" or \
                    "ConditionalCheckFailed" in str(e):
                raise ConcurrentUpdate(
                    session.session_date, expected_revision if
                    expected_revision is not None else -1
                ) from e
            raise StateStoreError(
                f"failed writing session {session.session_date}: {e}"
            ) from e
