"""
Persistence for hypotheses.

    PK = HYP#<id>            SK = META
    PK = SYMBOL#<symbol>     SK = HYP#<ts>#<id>
    PK = HYPRUN#<run_id>     SK = META | ITEM#<symbol>
    PK = SESSION#<date>      SK = HYPRUN#<ts>#<id>

Rejected hypotheses are stored too. A journal that only records trades
cannot answer "why didn't you take NVDA", which is the question that
makes a decision log worth keeping.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from .models import HypothesisRun, TradeHypothesis


class HypothesisStore(ABC):
    @abstractmethod
    def save_run(self, run: HypothesisRun) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> Optional[Dict]: ...

    @abstractmethod
    def latest_for_symbol(self, symbol: str) -> Optional[Dict]: ...

    @abstractmethod
    def history_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]: ...

    @abstractmethod
    def latest_run(self, session_date: Optional[str] = None) -> Optional[Dict]: ...


class InMemoryHypothesisStore(HypothesisStore):
    def __init__(self):
        self._runs: Dict[str, Dict] = {}
        self._order: List[str] = []
        self._by_symbol: Dict[str, List[Dict]] = {}

    @staticmethod
    def _copy(row):
        # Reads are copies; the DynamoDB store deserialises every read
        # and an in-memory store that handed back live objects would let
        # tests pass against behaviour production does not have.
        return None if row is None else json.loads(json.dumps(row))

    def save_run(self, run: HypothesisRun) -> None:
        payload = json.loads(json.dumps(run.as_dict(include_hypotheses=True)))
        self._runs[run.hypothesis_run_id] = payload
        if run.hypothesis_run_id in self._order:
            self._order.remove(run.hypothesis_run_id)
        self._order.append(run.hypothesis_run_id)
        for item in payload.get("hypotheses", []):
            self._by_symbol.setdefault(item["symbol"], []).append(item)

    def get_run(self, run_id: str) -> Optional[Dict]:
        return self._copy(self._runs.get(run_id))

    def latest_for_symbol(self, symbol: str) -> Optional[Dict]:
        rows = self._by_symbol.get(symbol.upper())
        return self._copy(rows[-1]) if rows else None

    def history_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]:
        rows = self._by_symbol.get(symbol.upper(), [])
        return [self._copy(r) for r in reversed(rows)][:limit]

    def latest_run(self, session_date: Optional[str] = None) -> Optional[Dict]:
        for run_id in reversed(self._order):
            row = self._runs[run_id]
            if session_date is None or row.get("session_date") == session_date:
                return self._copy(row)
        return None


class DynamoDBHypothesisStore(HypothesisStore):
    def __init__(self, table_name: str, region: str = "us-east-2", client=None):
        self.table_name = table_name
        self.region = region
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb", region_name=self.region)
        return self._client

    @staticmethod
    def _item(pk: str, sk: str, payload: Dict) -> Dict:
        return {"PK": {"S": pk}, "SK": {"S": sk},
                "payload": {"S": json.dumps(payload, default=str)}}

    def _write(self, items: List[Dict]) -> None:
        for start in range(0, len(items), 25):
            batch = items[start:start + 25]
            self.client.batch_write_item(RequestItems={
                self.table_name: [{"PutRequest": {"Item": i}} for i in batch]})

    def save_run(self, run: HypothesisRun) -> None:
        items = [self._item(f"HYPRUN#{run.hypothesis_run_id}", "META",
                            run.as_dict(include_hypotheses=False)),
                 self._item(f"SESSION#{run.session_date}",
                            f"HYPRUN#{run.started_at}#{run.hypothesis_run_id}",
                            {"hypothesis_run_id": run.hypothesis_run_id,
                             "started_at": run.started_at,
                             "generated_count": run.generated_count})]
        for h in run.hypotheses:
            payload = h.as_dict()
            payload["hypothesis_run_id"] = run.hypothesis_run_id
            items.append(self._item(f"HYP#{h.hypothesis_id}", "META", payload))
            items.append(self._item(f"HYPRUN#{run.hypothesis_run_id}",
                                    f"ITEM#{h.symbol}", payload))
            items.append(self._item(f"SYMBOL#{h.symbol}",
                                    f"HYP#{h.generated_at}#{h.hypothesis_id}",
                                    payload))
        self._write(items)

    def _query(self, pk: str, prefix: str = "", limit: int = 50,
               newest_first: bool = True) -> List[Dict]:
        kwargs = {"TableName": self.table_name,
                  "KeyConditionExpression": "PK = :pk",
                  "ExpressionAttributeValues": {":pk": {"S": pk}},
                  "ScanIndexForward": not newest_first, "Limit": limit}
        if prefix:
            kwargs["KeyConditionExpression"] += " AND begins_with(SK, :sk)"
            kwargs["ExpressionAttributeValues"][":sk"] = {"S": prefix}
        rows = self.client.query(**kwargs).get("Items", [])
        return [json.loads(r["payload"]["S"]) for r in rows if "payload" in r]

    def get_run(self, run_id: str) -> Optional[Dict]:
        meta = self._query(f"HYPRUN#{run_id}", "META", limit=1)
        if not meta:
            return None
        run = meta[0]
        run["hypotheses"] = self._query(f"HYPRUN#{run_id}", "ITEM#", limit=100,
                                        newest_first=False)
        return run

    def latest_for_symbol(self, symbol: str) -> Optional[Dict]:
        rows = self._query(f"SYMBOL#{symbol.upper()}", "HYP#", limit=1)
        return rows[0] if rows else None

    def history_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]:
        return self._query(f"SYMBOL#{symbol.upper()}", "HYP#", limit=limit)

    def latest_run(self, session_date: Optional[str] = None) -> Optional[Dict]:
        if session_date is None:
            return None
        index = self._query(f"SESSION#{session_date}", "HYPRUN#", limit=1)
        if not index:
            return None
        return self.get_run(index[0]["hypothesis_run_id"])
