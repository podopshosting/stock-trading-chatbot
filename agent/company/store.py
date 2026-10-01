"""
Company intelligence persistence.

Historical facts (dividend events, splits, corporate actions, financial
periods, earnings records) are IMMUTABLE: written once under a key that
includes the fact's own identity, and an attempt to overwrite one with a
different value is refused rather than silently replacing history.
Derived results (profile, peer set, comparison, holding context) are
VERSIONED snapshots: each write is a new record stamped with the time, so
earlier versions remain and a result can be reproduced.

Table: the PK/SK journal table (NOT the session-keyed state table).
PK = COMPANY#<SYMBOL>.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from .peers import PeerSet, validate as validate_peers

HISTORY_KINDS = {"DIVIDEND", "SPLIT", "ACTION", "FINPERIOD", "EARNINGS"}
SNAPSHOT_KINDS = {"PROFILE", "DIVPROFILE", "PEERSET", "PEERCOMPARE",
                  "HOLDING", "FUNDSUMMARY", "EARNTRENDS", "ACTIONSYNC"}


class HistoryConflict(Exception):
    """An immutable historical fact was re-written with different content."""


class CompanyStore:
    def put_fact(self, symbol: str, kind: str, identity: str,
                 payload: Dict) -> bool:
        """Returns True if newly written, False if identical already
        stored. Raises HistoryConflict if different content collides."""
        raise NotImplementedError

    def put_snapshot(self, symbol: str, kind: str, payload: Dict,
                     stamp: str) -> None:
        raise NotImplementedError

    def facts(self, symbol: str, kind: str) -> List[Dict]:
        raise NotImplementedError

    def latest(self, symbol: str, kind: str) -> Optional[Dict]:
        raise NotImplementedError

    def versions(self, symbol: str, kind: str) -> List[Dict]:
        raise NotImplementedError


def _check_kind(kind, allowed):
    if kind not in allowed:
        raise ValueError(f"unknown record kind {kind!r}")


def _canon(payload: Dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _strip_volatile(payload: Dict) -> Dict:
    """Retrieval time differs on every fetch of the same fact; it is
    provenance, not content, so it must not make a re-fetch a 'conflict'."""
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k != "retrieved_at"}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return strip(payload)


class InMemoryCompanyStore(CompanyStore):
    def __init__(self):
        self._facts: Dict[tuple, Dict] = {}
        self._snaps: Dict[tuple, List[Dict]] = {}

    def put_fact(self, symbol, kind, identity, payload):
        _check_kind(kind, HISTORY_KINDS)
        key = (symbol.upper(), kind, identity)
        old = self._facts.get(key)
        if old is not None:
            if _canon(_strip_volatile(old)) == _canon(_strip_volatile(payload)):
                return False
            raise HistoryConflict(f"{key} already stored with different content")
        self._facts[key] = json.loads(_canon(payload))
        return True

    def put_snapshot(self, symbol, kind, payload, stamp):
        _check_kind(kind, SNAPSHOT_KINDS)
        if kind == "PEERSET":
            validate_peers(PeerSet(**{k: v for k, v in payload.items()
                                      if k in PeerSet.__dataclass_fields__}))
        self._snaps.setdefault((symbol.upper(), kind), []).append(
            {"stamp": stamp, "payload": json.loads(_canon(payload))})

    def facts(self, symbol, kind):
        return [v for (s, k, _i), v in sorted(self._facts.items(),
                                               key=lambda kv: kv[0][2])
                if s == symbol.upper() and k == kind]

    def latest(self, symbol, kind):
        v = self._snaps.get((symbol.upper(), kind))
        return v[-1] if v else None

    def versions(self, symbol, kind):
        return list(self._snaps.get((symbol.upper(), kind), []))


class DynamoDBCompanyStore(CompanyStore):
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

    def _pk(self, symbol):
        return {"S": f"COMPANY#{symbol.upper()}"}

    def put_fact(self, symbol, kind, identity, payload):
        _check_kind(kind, HISTORY_KINDS)
        sk = {"S": f"{kind}#{identity}"}
        body = _canon(payload)
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={"PK": self._pk(symbol), "SK": sk,
                      "payload": {"S": body}},
                ConditionExpression="attribute_not_exists(SK)")
            return True
        except self.client.exceptions.ConditionalCheckFailedException:
            old = self.client.get_item(
                TableName=self.table_name,
                Key={"PK": self._pk(symbol), "SK": sk},
                ConsistentRead=True)["Item"]["payload"]["S"]
            if _canon(_strip_volatile(json.loads(old))) == \
                    _canon(_strip_volatile(json.loads(body))):
                return False
            raise HistoryConflict(f"{symbol} {kind}#{identity} differs")

    def put_snapshot(self, symbol, kind, payload, stamp):
        _check_kind(kind, SNAPSHOT_KINDS)
        if kind == "PEERSET":
            validate_peers(PeerSet(**{k: v for k, v in payload.items()
                                      if k in PeerSet.__dataclass_fields__}))
        self.client.put_item(
            TableName=self.table_name,
            Item={"PK": self._pk(symbol), "SK": {"S": f"SNAP#{kind}#{stamp}"},
                  "payload": {"S": _canon(payload)}},
            ConditionExpression="attribute_not_exists(SK)")

    def _query(self, symbol, prefix, forward=True, limit=None):
        kw = dict(TableName=self.table_name,
                  KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
                  ExpressionAttributeValues={":p": self._pk(symbol),
                                             ":s": {"S": prefix}},
                  ScanIndexForward=forward, ConsistentRead=True)
        if limit:
            kw["Limit"] = limit
        items, token = [], None
        while True:
            if token:
                kw["ExclusiveStartKey"] = token
            r = self.client.query(**kw)
            items += r.get("Items", [])
            token = r.get("LastEvaluatedKey")
            if not token or (limit and len(items) >= limit):
                return items

    def facts(self, symbol, kind):
        return [json.loads(i["payload"]["S"])
                for i in self._query(symbol, f"{kind}#")]

    def latest(self, symbol, kind):
        items = self._query(symbol, f"SNAP#{kind}#", forward=False, limit=1)
        if not items:
            return None
        return {"stamp": items[0]["SK"]["S"].rsplit("#", 1)[1],
                "payload": json.loads(items[0]["payload"]["S"])}

    def versions(self, symbol, kind):
        return [{"stamp": i["SK"]["S"].rsplit("#", 1)[1],
                 "payload": json.loads(i["payload"]["S"])}
                for i in self._query(symbol, f"SNAP#{kind}#")]
