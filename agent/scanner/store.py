"""
Persistence for scanner runs and candidates.

DynamoDB single-table design:

    PK = RUN#<scanner_run_id>   SK = META                      run metadata
    PK = RUN#<scanner_run_id>   SK = CANDIDATE#<rank>#<symbol>  one candidate
    PK = SESSION#<date>         SK = RUN#<started_at>#<run_id>  run index

Rank is zero-padded in the sort key so `CANDIDATE#002#...` orders before
`CANDIDATE#010#...`; lexicographic ordering on an unpadded number would
put rank 10 ahead of rank 2 and silently corrupt the ranking on read.

The session index exists because "the latest scan" is the common query
and scanning a whole table to find it would get slower every day.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from .models import Candidate, ScannerRun, candidate_from_dict

RANK_WIDTH = 4


def candidate_sk(rank: int, symbol: str) -> str:
    return f"CANDIDATE#{rank:0{RANK_WIDTH}d}#{symbol}"


class ScannerStoreError(Exception):
    pass


class ScannerStore(ABC):
    @abstractmethod
    def save_run(self, run: ScannerRun) -> None:
        ...

    @abstractmethod
    def get_run(self, scanner_run_id: str) -> Optional[ScannerRun]:
        ...

    @abstractmethod
    def latest_run(self, session_date: Optional[str] = None) -> Optional[ScannerRun]:
        ...

    @abstractmethod
    def recent_runs(self, session_date: str, limit: int = 10) -> List[Dict]:
        ...

    @abstractmethod
    def candidates_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]:
        ...


class InMemoryScannerStore(ScannerStore):
    """For tests and local runs. Loses everything between invocations."""

    def __init__(self):
        self._runs: Dict[str, Dict] = {}
        self._order: List[str] = []

    def save_run(self, run: ScannerRun) -> None:
        self._runs[run.scanner_run_id] = json.loads(
            json.dumps(run.as_dict(include_candidates=True))
        )
        if run.scanner_run_id in self._order:
            self._order.remove(run.scanner_run_id)
        self._order.append(run.scanner_run_id)

    def get_run(self, scanner_run_id: str) -> Optional[ScannerRun]:
        row = self._runs.get(scanner_run_id)
        return ScannerRun.from_dict(row) if row else None

    def latest_run(self, session_date: Optional[str] = None) -> Optional[ScannerRun]:
        for run_id in reversed(self._order):
            row = self._runs[run_id]
            if session_date is None or row.get("session_date") == session_date:
                return ScannerRun.from_dict(row)
        return None

    def recent_runs(self, session_date: str, limit: int = 10) -> List[Dict]:
        out = []
        for run_id in reversed(self._order):
            row = self._runs[run_id]
            if row.get("session_date") == session_date:
                out.append({k: v for k, v in row.items() if k != "candidates"})
            if len(out) >= limit:
                break
        return out

    def candidates_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]:
        symbol = symbol.upper()
        out = []
        for run_id in reversed(self._order):
            for cand in self._runs[run_id].get("candidates", []):
                if cand.get("symbol") == symbol:
                    out.append(cand)
                    if len(out) >= limit:
                        return out
        return out


class DynamoDBScannerStore(ScannerStore):

    def __init__(self, table_name: str = "stock-agent-dev-scanner",
                 region: str = "us-east-2", client=None):
        self.table_name = table_name
        self.region = region
        self._client = client

    def _c(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb", region_name=self.region)
        return self._client

    @staticmethod
    def _doc(payload: Dict) -> Dict:
        return {"S": json.dumps(payload)}

    def save_run(self, run: ScannerRun) -> None:
        """Write metadata, the session index and every candidate.

        Batched in 25s, DynamoDB's per-request limit. The metadata item is
        written LAST: if the write is interrupted partway, a run without
        metadata is invisible to `latest_run` rather than appearing as a
        complete scan with candidates missing.
        """
        client = self._c()
        pk = f"RUN#{run.scanner_run_id}"
        meta = run.as_dict(include_candidates=False)

        items: List[Dict] = []
        for cand in run.candidates:
            items.append({"PutRequest": {"Item": {
                "PK": {"S": pk},
                "SK": {"S": candidate_sk(cand.rank, cand.symbol)},
                "symbol": {"S": cand.symbol},
                "rank": {"N": str(cand.rank)},
                "scanner_score": {"N": str(cand.scanner_score)},
                "session_date": {"S": run.session_date},
                "scanner_run_id": {"S": run.scanner_run_id},
                "payload": self._doc(cand.as_dict()),
            }}})

        try:
            for i in range(0, len(items), 25):
                batch = items[i:i + 25]
                resp = client.batch_write_item(
                    RequestItems={self.table_name: batch}
                )
                unprocessed = (resp.get("UnprocessedItems") or {}).get(
                    self.table_name, [])
                if unprocessed:
                    client.batch_write_item(
                        RequestItems={self.table_name: unprocessed}
                    )

            client.put_item(TableName=self.table_name, Item={
                "PK": {"S": f"SESSION#{run.session_date}"},
                "SK": {"S": f"RUN#{run.started_at}#{run.scanner_run_id}"},
                "scanner_run_id": {"S": run.scanner_run_id},
                "status": {"S": str(run.status)},
                "candidate_count": {"N": str(run.candidate_count)},
                "payload": self._doc(meta),
            })

            client.put_item(TableName=self.table_name, Item={
                "PK": {"S": pk},
                "SK": {"S": "META"},
                "session_date": {"S": run.session_date},
                "status": {"S": str(run.status)},
                "payload": self._doc(meta),
            })
        except Exception as e:
            raise ScannerStoreError(
                f"failed saving scanner run {run.scanner_run_id}: {e}"
            ) from e

    def get_run(self, scanner_run_id: str) -> Optional[ScannerRun]:
        client = self._c()
        pk = f"RUN#{scanner_run_id}"
        try:
            resp = client.query(
                TableName=self.table_name,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": pk}},
                ConsistentRead=True,
            )
        except Exception as e:
            raise ScannerStoreError(f"failed reading run {scanner_run_id}: {e}") from e

        meta: Optional[Dict] = None
        candidates: List[Dict] = []
        for item in resp.get("Items", []):
            sk = item["SK"]["S"]
            try:
                payload = json.loads(item["payload"]["S"])
            except Exception:
                continue
            if sk == "META":
                meta = payload
            elif sk.startswith("CANDIDATE#"):
                candidates.append(payload)

        if meta is None:
            return None

        run = ScannerRun.from_dict(meta)
        # Query returns items in sort-key order, which the zero-padded rank
        # makes the true ranking order.
        run.candidates = [candidate_from_dict(c) for c in candidates]
        return run

    def recent_runs(self, session_date: str, limit: int = 10) -> List[Dict]:
        client = self._c()
        try:
            resp = client.query(
                TableName=self.table_name,
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": {"S": f"SESSION#{session_date}"},
                    ":sk": {"S": "RUN#"},
                },
                ScanIndexForward=False,        # newest first
                Limit=limit,
            )
        except Exception as e:
            raise ScannerStoreError(
                f"failed listing runs for {session_date}: {e}") from e

        out = []
        for item in resp.get("Items", []):
            try:
                out.append(json.loads(item["payload"]["S"]))
            except Exception:
                continue
        return out

    def latest_run(self, session_date: Optional[str] = None) -> Optional[ScannerRun]:
        if session_date is None:
            from ..state.store import today_market_date
            session_date = today_market_date()
        runs = self.recent_runs(session_date, limit=1)
        if not runs:
            return None
        return self.get_run(runs[0]["scanner_run_id"])

    def candidates_for_symbol(self, symbol: str, limit: int = 20) -> List[Dict]:
        """Per-symbol history.

        Served by walking recent runs rather than a GSI: a secondary index
        would cost write capacity on every candidate for a query that is
        diagnostic, not hot. Revisit if it becomes a real access pattern.
        """
        from ..state.store import today_market_date
        session_date = today_market_date()
        symbol = symbol.upper()
        out: List[Dict] = []
        for meta in self.recent_runs(session_date, limit=10):
            run = self.get_run(meta.get("scanner_run_id", ""))
            if run is None:
                continue
            for cand in run.candidates:
                if cand.symbol == symbol:
                    out.append(cand.as_dict())
                    if len(out) >= limit:
                        return out
        return out
