"""
Persistence for evidence and catalyst results.

DynamoDB single-table, `stock-agent-dev-evidence`:

    PK = EVIDENCE#<evidence_id>   SK = META           one item
    PK = SYMBOL#<symbol>          SK = EV#<ts>#<id>   per-symbol history
    PK = SYMBOL#<symbol>          SK = CATALYST#<ts>  catalyst results
    PK = DUPGROUP#<group_id>      SK = EV#<id>        duplicate groups
    PK = EVRUN#<run_id>           SK = META           run metadata
    PK = EVRUN#<run_id>           SK = RESULT#<sym>   per-symbol result

CONTENT POLICY. Full article text is never stored. On our Alpaca
entitlement it is not even returned - `content` came back empty on every
observed item - but the rule stands regardless of what a provider
happens to send. What is stored is:

    headline, publisher, URL, the provider's own short excerpt,
    structured extraction, and generated summaries

SEC filings are US government works and may be stored and displayed
freely; the metadata kept here is in any case only what is needed to
find the filing again.

`_strip_unlicensed` enforces this on the way in, so a future provider
that starts returning full text cannot quietly fill the table with it.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from .models import CatalystResult, EvidenceItem, EvidenceRun

# Headline and excerpt caps. A provider excerpt is short by design; a
# long one is a sign we are being handed article body.
MAX_HEADLINE_CHARS = 400
MAX_SUMMARY_CHARS = 1200


class EvidenceStoreError(Exception):
    pass


def _strip_unlicensed(payload: Dict) -> Dict:
    """Remove anything that looks like full article body.

    Applied to every write. The policy is not enforced by asking
    providers to behave - it is enforced here, on the way into storage.
    """
    out = dict(payload)
    if isinstance(out.get("headline"), str):
        out["headline"] = out["headline"][:MAX_HEADLINE_CHARS]
    if isinstance(out.get("summary"), str) and \
            len(out["summary"]) > MAX_SUMMARY_CHARS:
        out["summary"] = out["summary"][:MAX_SUMMARY_CHARS] + " [truncated]"

    raw = out.get("raw_metadata")
    if isinstance(raw, dict):
        # Never persist a body field even if a provider supplies one.
        for banned in ("content", "body", "article_text", "full_text", "text"):
            raw.pop(banned, None)
    return out


class EvidenceStore(ABC):
    @abstractmethod
    def save_catalyst(self, result: CatalystResult) -> None:
        ...

    @abstractmethod
    def latest_catalyst(self, symbol: str) -> Optional[Dict]:
        ...

    @abstractmethod
    def evidence_for_symbol(self, symbol: str, limit: int = 50) -> List[Dict]:
        ...

    @abstractmethod
    def get_evidence(self, evidence_id: str) -> Optional[Dict]:
        ...

    @abstractmethod
    def duplicate_group(self, group_id: str) -> List[Dict]:
        ...

    @abstractmethod
    def save_run(self, run: EvidenceRun) -> None:
        ...

    @abstractmethod
    def get_run(self, run_id: str) -> Optional[Dict]:
        ...


class InMemoryEvidenceStore(EvidenceStore):
    """For tests and local runs. Loses everything between invocations."""

    def __init__(self):
        self._evidence: Dict[str, Dict] = {}
        self._by_symbol: Dict[str, List[Dict]] = {}
        self._catalysts: Dict[str, List[Dict]] = {}
        self._groups: Dict[str, List[str]] = {}
        self._runs: Dict[str, Dict] = {}
        self._run_order: List[str] = []

    @staticmethod
    def _copy(row):
        # Reads are copies. Handing back the stored object would let a
        # caller mutate the store by editing what looks like a result,
        # and the DynamoDB store cannot behave that way.
        return None if row is None else json.loads(json.dumps(row))

    def save_catalyst(self, result: CatalystResult) -> None:
        symbol = result.symbol.upper()
        payload = json.loads(json.dumps(result.as_dict(include_items=True)))
        payload["items"] = [_strip_unlicensed(i) for i in payload.get("items", [])]
        self._catalysts.setdefault(symbol, []).append(payload)

        for item in payload.get("items", []):
            eid = item.get("evidence_id")
            if not eid:
                continue
            self._evidence[eid] = item
            self._by_symbol.setdefault(symbol, []).append(item)
            gid = item.get("duplicate_group_id")
            if gid:
                members = self._groups.setdefault(gid, [])
                if eid not in members:
                    members.append(eid)

    def latest_catalyst(self, symbol: str) -> Optional[Dict]:
        rows = self._catalysts.get(symbol.upper())
        return self._copy(rows[-1]) if rows else None

    def evidence_for_symbol(self, symbol: str, limit: int = 50) -> List[Dict]:
        rows = self._by_symbol.get(symbol.upper(), [])
        # Newest first, de-duplicated by id so repeated runs do not
        # return the same article several times.
        seen, out = set(), []
        for row in reversed(rows):
            eid = row.get("evidence_id")
            if eid in seen:
                continue
            seen.add(eid)
            out.append(self._copy(row))
            if len(out) >= limit:
                break
        return out

    def get_evidence(self, evidence_id: str) -> Optional[Dict]:
        return self._copy(self._evidence.get(evidence_id))

    def duplicate_group(self, group_id: str) -> List[Dict]:
        return [self._copy(self._evidence[e])
                for e in self._groups.get(group_id, [])
                if e in self._evidence]

    def save_run(self, run: EvidenceRun) -> None:
        payload = json.loads(json.dumps(run.as_dict(include_results=True)))
        self._runs[run.evidence_run_id] = payload
        if run.evidence_run_id in self._run_order:
            self._run_order.remove(run.evidence_run_id)
        self._run_order.append(run.evidence_run_id)
        for result in run.results:
            self.save_catalyst(result)

    def get_run(self, run_id: str) -> Optional[Dict]:
        return self._copy(self._runs.get(run_id))

    def latest_run(self, session_date: Optional[str] = None) -> Optional[Dict]:
        for run_id in reversed(self._run_order):
            row = self._runs[run_id]
            if session_date is None or row.get("session_date") == session_date:
                return self._copy(row)
        return None


class DynamoDBEvidenceStore(EvidenceStore):
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
        return {"PK": {"S": pk}, "SK": {"S": sk},
                "payload": {"S": json.dumps(payload, default=str)}}

    def _write(self, items: List[Dict]) -> None:
        for start in range(0, len(items), 25):       # BatchWriteItem cap
            batch = items[start:start + 25]
            self.client.batch_write_item(RequestItems={
                self.table_name: [{"PutRequest": {"Item": i}} for i in batch]})

    def save_catalyst(self, result: CatalystResult) -> None:
        symbol = result.symbol.upper()
        payload = result.as_dict(include_items=True)
        payload["items"] = [_strip_unlicensed(i) for i in payload.get("items", [])]

        items = [self._item(f"SYMBOL#{symbol}",
                            f"CATALYST#{result.evaluated_at}",
                            {k: v for k, v in payload.items() if k != "items"})]
        for entry in payload.get("items", []):
            eid = entry.get("evidence_id")
            if not eid:
                continue
            published = entry.get("published_at") or entry.get("retrieved_at")
            items.append(self._item(f"EVIDENCE#{eid}", "META", entry))
            items.append(self._item(f"SYMBOL#{symbol}",
                                    f"EV#{published}#{eid}", entry))
            gid = entry.get("duplicate_group_id")
            if gid:
                items.append(self._item(f"DUPGROUP#{gid}", f"EV#{eid}", entry))
        self._write(items)

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

    def latest_catalyst(self, symbol: str) -> Optional[Dict]:
        rows = self._query(f"SYMBOL#{symbol.upper()}", "CATALYST#", limit=1)
        return rows[0] if rows else None

    def evidence_for_symbol(self, symbol: str, limit: int = 50) -> List[Dict]:
        return self._query(f"SYMBOL#{symbol.upper()}", "EV#", limit=limit)

    def get_evidence(self, evidence_id: str) -> Optional[Dict]:
        rows = self._query(f"EVIDENCE#{evidence_id}", "META", limit=1)
        return rows[0] if rows else None

    def duplicate_group(self, group_id: str) -> List[Dict]:
        return self._query(f"DUPGROUP#{group_id}", "EV#", limit=50,
                           newest_first=False)

    def save_run(self, run: EvidenceRun) -> None:
        items = [self._item(f"EVRUN#{run.evidence_run_id}", "META",
                            run.as_dict(include_results=False))]
        items.append(self._item(
            f"SESSION#{run.session_date}",
            f"EVRUN#{run.started_at}#{run.evidence_run_id}",
            {"evidence_run_id": run.evidence_run_id,
             "started_at": run.started_at,
             "scanner_run_id": run.scanner_run_id,
             "evaluated_count": run.evaluated_count}))
        for result in run.results:
            summary = result.as_dict(include_items=False)
            items.append(self._item(f"EVRUN#{run.evidence_run_id}",
                                    f"RESULT#{result.symbol}", summary))
        self._write(items)
        for result in run.results:
            self.save_catalyst(result)

    def get_run(self, run_id: str) -> Optional[Dict]:
        meta = self._query(f"EVRUN#{run_id}", "META", limit=1)
        if not meta:
            return None
        run = meta[0]
        run["results"] = self._query(f"EVRUN#{run_id}", "RESULT#", limit=100,
                                     newest_first=False)
        return run

    def latest_run(self, session_date: Optional[str] = None) -> Optional[Dict]:
        if session_date is None:
            return None
        index = self._query(f"SESSION#{session_date}", "EVRUN#", limit=1)
        if not index:
            return None
        return self.get_run(index[0]["evidence_run_id"])
