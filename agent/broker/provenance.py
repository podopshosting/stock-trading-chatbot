"""
Who created an external position.

A position the agent finds at the venue but does not hold in its own
store is either its own, left behind by a failure, or somebody else's.
Those demand opposite responses: the first should be adopted and
managed, the second must never be touched. Treating the first as the
second is what left a filled DRAM position unmanaged on 2026-10-02 —
halting correctly, and then being unable to close what it had bought.

The evidence is not a heuristic. A client order id is
`cli_` + sha256(f"{risk_decision_id}:{intent}")[:20], so it can be
RECONSTRUCTED from the agent's own stored risk decision and compared
with what the venue reports. A match is proof: only something holding
that decision id could produce that string. On the DRAM order the
reconstruction matched exactly.

A prefix match alone is weaker and is reported as such, because any
client could choose the same prefix.
"""
from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Optional, Sequence

CLIENT_ID_PREFIX = "cli_"
DIGEST_LENGTH = 20

ORIGIN_AGENT_CREATED = "AGENT_CREATED"
ORIGIN_PREEXISTING_EXTERNAL = "PREEXISTING_EXTERNAL"
ORIGIN_UNKNOWN = "UNKNOWN_ORIGIN"

# How the conclusion was reached, strongest first.
EVIDENCE_LEDGER = "LEDGER_RECORD"
EVIDENCE_RECONSTRUCTED_ID = "RECONSTRUCTED_CLIENT_ID"
EVIDENCE_PREFIX_ONLY = "CLIENT_ID_PREFIX_ONLY"
EVIDENCE_NO_AGENT_ORDER = "NO_AGENT_ORDER_FOR_SYMBOL"
EVIDENCE_UNREADABLE = "EVIDENCE_UNREADABLE"


def client_order_id_for(risk_decision_id: str, intent: str = "ENTRY") -> str:
    """The id the agent would deterministically produce.

    Mirrors agent/broker/execution.py. Kept here so provenance can be
    established without importing the execution path, which the read
    API deliberately cannot reach.
    """
    digest = hashlib.sha256(
        f"{risk_decision_id}:{intent}".encode()).hexdigest()[:DIGEST_LENGTH]
    return f"{CLIENT_ID_PREFIX}{digest}"


# Every intent the agent can derive a client order id for. Exit
# attempts beyond the first carry a suffix, and leaving them out here
# would mean a position closed on a second attempt could not be
# recognised as the agent's own.
DEFAULT_INTENTS = ("ENTRY", "EXIT", "EXIT#2", "EXIT#3")


def classify_position(symbol: str,
                      broker_orders: Optional[Sequence[Dict]],
                      ledger_rows: Optional[Sequence] = None,
                      decisions: Optional[Sequence[Dict]] = None,
                      intents: Iterable[str] = DEFAULT_INTENTS) -> Dict:
    """Decide whether an external position is the agent's own.

    `broker_orders` is None when the order history could not be read. In
    that case the answer is UNKNOWN_ORIGIN, never PREEXISTING: not
    knowing who opened a position is not evidence that somebody else
    did.
    """
    if broker_orders is None:
        return _result(symbol, ORIGIN_UNKNOWN, EVIDENCE_UNREADABLE,
                       "the broker's order history could not be read, so "
                       "origin cannot be established")

    mine = [o for o in broker_orders
            if str(o.get("symbol") or "").upper() == symbol.upper()]

    # 1. The ledger, if the agent recorded the order itself.
    for row in (ledger_rows or []):
        if (str(getattr(row, "symbol", "")).upper() == symbol.upper()
                and (getattr(row, "filled_quantity", 0) or 0) > 0):
            return _result(symbol, ORIGIN_AGENT_CREATED, EVIDENCE_LEDGER,
                           f"ledger record {getattr(row, 'client_order_id', '')} "
                           f"shows a fill for this symbol",
                           client_order_id=getattr(row, "client_order_id",
                                                   None))

    # 2. Reconstruct the deterministic id from the agent's own decisions
    #    and compare. A match is proof rather than inference.
    expected = {}
    for d in (decisions or []):
        rd = ((d.get("risk") or {}).get("decision_id")
              or d.get("risk_decision_id"))
        if not rd:
            continue
        for intent in intents:
            expected[client_order_id_for(rd, intent)] = (rd, intent)
    for o in mine:
        cid = o.get("client_order_id")
        if cid and cid in expected:
            rd, intent = expected[cid]
            return _result(
                symbol, ORIGIN_AGENT_CREATED, EVIDENCE_RECONSTRUCTED_ID,
                f"the venue's client order id {cid} is reproduced exactly by "
                f"sha256('{rd}:{intent}'), which only this agent's own risk "
                f"decision could produce",
                client_order_id=cid, broker_order_id=o.get("id"),
                risk_decision_id=rd)

    # 3. A prefix match is suggestive, not proof: any client could use it.
    for o in mine:
        cid = str(o.get("client_order_id") or "")
        if cid.startswith(CLIENT_ID_PREFIX):
            return _result(
                symbol, ORIGIN_AGENT_CREATED, EVIDENCE_PREFIX_ONLY,
                f"client order id {cid} carries this agent's prefix, but no "
                f"stored decision reproduces it - weaker than a "
                f"reconstruction and worth investigating",
                client_order_id=cid, broker_order_id=o.get("id"))

    # 4. The history was readable and holds no agent order for it.
    if not mine:
        return _result(symbol, ORIGIN_PREEXISTING_EXTERNAL,
                       EVIDENCE_NO_AGENT_ORDER,
                       "the order history was read and contains no order "
                       "from this agent for this symbol")
    return _result(symbol, ORIGIN_UNKNOWN, EVIDENCE_UNREADABLE,
                   "orders exist for this symbol but none can be attributed "
                   "to this agent")


def _result(symbol, origin, evidence, detail, **extra) -> Dict:
    out = {"symbol": symbol, "origin": origin, "evidence": evidence,
           "detail": detail,
           "adoptable": origin == ORIGIN_AGENT_CREATED,
           "proven": evidence in (EVIDENCE_LEDGER,
                                  EVIDENCE_RECONSTRUCTED_ID)}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def blocks_new_exposure(classifications: Sequence[Dict]) -> bool:
    """Unknown origin refuses new exposure.

    A preexisting position does not, on its own: it is somebody else's
    and is left alone. An unattributable one does, because the agent
    cannot reason about exposure it cannot explain.
    """
    return any(c.get("origin") == ORIGIN_UNKNOWN for c in classifications)
