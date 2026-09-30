"""
A cycle lock.

EventBridge delivers at-least-once and Lambdas overlap, so two cycles
can run simultaneously. Without a lock both would pass the risk checks
against the same capital and both would enter, producing double the
intended exposure from a single day's signal.

The DynamoDB implementation uses a conditional put with a TTL: the
condition makes acquisition atomic, and the TTL means a Lambda killed
mid-cycle releases the lock by expiry rather than deadlocking the agent
until someone notices.
"""
from __future__ import annotations

import os
import time
from typing import Dict, Optional

from ..observability import log_event

# Long enough for a slow cycle, short enough that a crashed Lambda does
# not block trading for the rest of the session.
DEFAULT_TTL_SECONDS = 300


class CycleLock:
    def acquire(self, cycle_id: str) -> bool:
        raise NotImplementedError

    def release(self, cycle_id: str) -> None:
        raise NotImplementedError


class InMemoryCycleLock(CycleLock):
    """For tests and single-process runs."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._holder: Optional[str] = None
        self._acquired_at: float = 0.0

    def acquire(self, cycle_id: str) -> bool:
        now = time.time()
        if (self._holder is not None
                and now - self._acquired_at < self.ttl_seconds):
            return False
        self._holder = cycle_id
        self._acquired_at = now
        return True

    def release(self, cycle_id: str) -> None:
        # Only the holder may release. A late cycle releasing a lock it
        # does not own would let a third cycle in alongside the holder.
        if self._holder == cycle_id:
            self._holder = None
            self._acquired_at = 0.0

    @property
    def held_by(self) -> Optional[str]:
        return self._holder


class DynamoDBCycleLock(CycleLock):
    """PK = CONTROL#CYCLE_LOCK, SK = LOCK. One lock for the whole agent."""

    def __init__(self, table_name: Optional[str] = None, client=None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.table_name = table_name or os.environ.get(
            "AGENT_STATE_TABLE", "stock-agent-dev-state")
        self.ttl_seconds = ttl_seconds
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def acquire(self, cycle_id: str) -> bool:
        now = int(time.time())
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "PK": {"S": "CONTROL#CYCLE_LOCK"},
                    "SK": {"S": "LOCK"},
                    "cycle_id": {"S": cycle_id},
                    "acquired_at": {"N": str(now)},
                    "ttl": {"N": str(now + self.ttl_seconds)},
                },
                # Acquire only if nobody holds it, or the holder's lease
                # has expired. The TTL attribute is the lease; DynamoDB's
                # own TTL deletion is best-effort and too slow to rely on.
                ConditionExpression=(
                    "attribute_not_exists(SK) OR #ttl < :now"),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": {"N": str(now)}})
            return True
        except Exception as exc:                          # noqa: BLE001
            if "ConditionalCheckFailed" in str(exc):
                log_event("cycle_lock_contended", cycle_id=cycle_id)
                return False
            raise

    def release(self, cycle_id: str) -> None:
        try:
            self.client.delete_item(
                TableName=self.table_name,
                Key={"PK": {"S": "CONTROL#CYCLE_LOCK"},
                     "SK": {"S": "LOCK"}},
                # Only the holder may release.
                ConditionExpression="cycle_id = :id",
                ExpressionAttributeValues={":id": {"S": cycle_id}})
        except Exception as exc:                          # noqa: BLE001
            if "ConditionalCheckFailed" not in str(exc):
                raise
