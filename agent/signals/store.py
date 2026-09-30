"""
Persistence for signal runs and per-symbol results.

DynamoDB single-table design:

    PK = SIGRUN#<signal_run_id>  SK = META                run metadata
    PK = SIGRUN#<signal_run_id>  SK = RESULT#<symbol>     one symbol
    PK = SYMBOL#<symbol>         SK = SIGNAL#<ts>#<run>   per-symbol history
    PK = SESSION#<date>          SK = SIGRUN#<ts>#<run>   run index

The per-symbol partition exists because "what does the engine currently
say about AAPL" is the most common read, and finding it by scanning
every run would get slower every day.

Raw bars are deliberately NOT stored. They are large, they are
reproducible from the provider, and a copy here would go stale while
looking authoritative. What is stored is the reading and the numbers it
was derived from, which is what a later audit actually needs.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from .models import QuantitativeSignalResult, SignalRun


class SignalStoreError(Exception):
    pass


class SignalStore(ABC):
    @abstractmethod
    def save_run(self, run: SignalRun) -> None:
        ...

    @abstractmethod
    def get_run(self, signal_run_id: str) -> Optional[Dict]:
        ...

    @abstractmethod
    def latest_for_symbol(self, symbol: str) -> Optional[Dict]:
        ...

    @abstractmethod
    def history_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]:
        ...

    @abstractmethod
    def latest_run(self, session_date: Optional[str] = None) -> Optional[Dict]:
        ...


class InMemorySignalStore(SignalStore):
    """For tests and local runs. Loses everything between invocations."""

    def __init__(self):
        self._runs: Dict[str, Dict] = {}
        self._order: List[str] = []
        self._by_symbol: Dict[str, List[Dict]] = {}

    def save_run(self, run: SignalRun) -> None:
        # Round-trip through JSON so an in-memory store cannot accidentally
        # hand back a live object that a caller then mutates.
        payload = json.loads(json.dumps(run.as_dict(include_results=True)))
        self._runs[run.signal_run_id] = payload
        if run.signal_run_id in self._order:
            self._order.remove(run.signal_run_id)
        self._order.append(run.signal_run_id)

        for result in payload.get("results", []):
            symbol = result.get("symbol")
            if not symbol:
                continue
            entry = dict(result)
            entry["signal_run_id"] = run.signal_run_id
            self._by_symbol.setdefault(symbol, []).append(entry)

    @staticmethod
    def _copy(row):
        """Reads are copies.

        Handing back the stored object let a caller mutate the store by
        editing what looked like a result. The DynamoDB store cannot do
        this - every read is deserialised - so an in-memory store that
        did would make tests pass against behaviour production does not
        have.
        """
        return None if row is None else json.loads(json.dumps(row))

    def get_run(self, signal_run_id: str) -> Optional[Dict]:
        return self._copy(self._runs.get(signal_run_id))

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


class DynamoDBSignalStore(SignalStore):
    """Production store. Imports boto3 lazily so tests never need it."""

    def __init__(self, table_name: str, region: str = "us-east-2",
                 client=None):
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
        # Stored as one JSON blob rather than mapped attribute-by-attribute.
        # The shape is nested and evolving; a partial attribute mapping
        # would silently drop new fields as they are added.
        return {
            "PK": {"S": pk},
            "SK": {"S": sk},
            "payload": {"S": json.dumps(payload, default=str)},
        }

    def save_run(self, run: SignalRun) -> None:
        items = [self._item(f"SIGRUN#{run.signal_run_id}", "META",
                            run.as_dict(include_results=False))]
        items.append(self._item(
            f"SESSION#{run.session_date}",
            f"SIGRUN#{run.started_at}#{run.signal_run_id}",
            {"signal_run_id": run.signal_run_id,
             "started_at": run.started_at,
             "scanner_run_id": run.scanner_run_id,
             "evaluated_count": run.evaluated_count}))

        for result in run.results:
            payload = result.as_dict()
            payload["signal_run_id"] = run.signal_run_id
            items.append(self._item(f"SIGRUN#{run.signal_run_id}",
                                    f"RESULT#{result.symbol}", payload))
            items.append(self._item(
                f"SYMBOL#{result.symbol}",
                f"SIGNAL#{result.timestamp}#{run.signal_run_id}", payload))

        for start in range(0, len(items), 25):        # BatchWriteItem cap
            batch = items[start:start + 25]
            self.client.batch_write_item(RequestItems={
                self.table_name: [{"PutRequest": {"Item": i}} for i in batch]
            })

    def _query(self, pk: str, prefix: str = "", limit: int = 50,
               newest_first: bool = True) -> List[Dict]:
        kwargs = {
            "TableName": self.table_name,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            "ScanIndexForward": not newest_first,
            "Limit": limit,
        }
        if prefix:
            kwargs["KeyConditionExpression"] += " AND begins_with(SK, :sk)"
            kwargs["ExpressionAttributeValues"][":sk"] = {"S": prefix}
        rows = self.client.query(**kwargs).get("Items", [])
        return [json.loads(r["payload"]["S"]) for r in rows if "payload" in r]

    def get_run(self, signal_run_id: str) -> Optional[Dict]:
        meta = self._query(f"SIGRUN#{signal_run_id}", "META", limit=1)
        if not meta:
            return None
        run = meta[0]
        run["results"] = self._query(f"SIGRUN#{signal_run_id}", "RESULT#",
                                     limit=200, newest_first=False)
        return run

    def latest_for_symbol(self, symbol: str) -> Optional[Dict]:
        rows = self._query(f"SYMBOL#{symbol.upper()}", "SIGNAL#", limit=1)
        return rows[0] if rows else None

    def history_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]:
        return self._query(f"SYMBOL#{symbol.upper()}", "SIGNAL#", limit=limit)

    def latest_run(self, session_date: Optional[str] = None) -> Optional[Dict]:
        if session_date is None:
            return None
        index = self._query(f"SESSION#{session_date}", "SIGRUN#", limit=1)
        if not index:
            return None
        return self.get_run(index[0]["signal_run_id"])
