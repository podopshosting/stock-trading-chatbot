"""
Taking back a position the agent created and did not record.

On 2026-10-02 the agent bought DRAM, the fill arrived asynchronously,
and the position existed only at the venue. The agent halted correctly
and then could not close what it had bought, because `flatten_all`
iterates the internal store and the position was not in it. The remedy
is to put it in the store - not to clear the latches, which were doing
their job.

Adoption is deliberately narrow. Three origins, three different
responses, and the difference is the whole point:

  PROVEN AGENT_CREATED   adopt it, manage it, exit it - even while
                         halted, because exits are always permitted
  PREEXISTING_EXTERNAL   leave it entirely alone; it is not ours
  UNKNOWN_ORIGIN         do not adopt, do not close, block new
                         exposure, and say so loudly

`proven` is the gate, not `adoptable`. A client order id that merely
carries this agent's prefix sets `adoptable` while leaving `proven`
False, and a prefix is not proof: any client could choose the same one.
Adopting on a prefix would mean adopting somebody else's position on the
strength of a naming convention.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..broker.provenance import (
    EVIDENCE_LEDGER, EVIDENCE_RECONSTRUCTED_ID, ORIGIN_AGENT_CREATED,
    ORIGIN_PREEXISTING_EXTERNAL, ORIGIN_UNKNOWN, classify_position,
)
from ..observability import log_event
from .models import ExitPlan, StopMechanism

INTEGRITY_COMPLETE = "COMPLETE"
INTEGRITY_PARTIAL = "PARTIAL"
INTEGRITY_UNKNOWN = "UNKNOWN"

# How the stop on an adopted position was arrived at. Recorded because
# a reconstructed stop is not the stop the position was opened under,
# and a later review must be able to tell the difference.
PLAN_ORIGINAL = "RECOVERED_FROM_DECISION"
PLAN_RECONSTRUCTED = "RECONSTRUCTED_FROM_RISK_LIMIT"


def _recover_stop_distance_pct(risk_decision_id: Optional[str],
                               decisions: Optional[List[Dict]]) -> Optional[float]:
    """The stop distance the position was actually opened under.

    Only a stored decision can answer this. Nothing is inferred: if the
    decision row does not carry it, this returns None and the caller
    reconstructs a stop from the risk limit instead, under a different
    label.
    """
    if not risk_decision_id:
        return None
    for row in (decisions or []):
        risk = row.get("risk") or {}
        if risk.get("decision_id") != risk_decision_id:
            continue
        pct = risk.get("stop_distance_pct")
        if pct is not None:
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                return None
            return pct if pct > 0 else None
    return None


def build_adoption_plan(entry_price: float, quantity: float,
                        max_trade_risk: float,
                        stop_distance_pct: Optional[float] = None) -> Dict:
    """An exit plan for a position being taken back into management.

    Where the original stop distance is known, it is used, because that
    is the plan the position was opened under.

    Where it is not, the stop is placed where the configured per-trade
    risk limit says the loss must stop: entry minus
    max_trade_risk/quantity. That is a derivation from a configured
    limit rather than an invented percentage, and it is labelled
    RECONSTRUCTED so nobody later mistakes it for the original.
    """
    if entry_price is None or entry_price <= 0:
        raise ValueError("an entry price is required to place a stop")
    if quantity is None or quantity <= 0:
        raise ValueError("a quantity is required to place a stop")

    if stop_distance_pct:
        stop = entry_price * (1 - stop_distance_pct / 100.0)
        origin, pct = PLAN_ORIGINAL, stop_distance_pct
    else:
        stop = entry_price - (max_trade_risk / quantity)
        origin = PLAN_RECONSTRUCTED
        pct = ((entry_price - stop) / entry_price) * 100.0

    if stop <= 0:
        # The position is large enough relative to the risk limit that
        # no positive stop bounds the loss to it. Inventing a wider one
        # would quietly exceed the configured limit, so this refuses and
        # the position is surfaced instead of silently mismanaged.
        raise ValueError(
            f"no positive stop bounds a loss of {max_trade_risk} on "
            f"{quantity} units at {entry_price}; this position cannot be "
            f"adopted under the configured per-trade risk limit")

    plan = ExitPlan(
        stop_price=round(stop, 4),
        target_price=round(entry_price * (1 + (pct * 2) / 100.0), 4),
        trailing_stop_pct=round(pct, 4),
        stop_mechanism=StopMechanism.ENGINE_POLLED)
    return {"plan": plan, "stop_origin": origin,
            "stop_distance_pct": round(pct, 4)}


def _synthetic_entry_order(position: Dict, client_order_id: Optional[str],
                           broker_order_id: Optional[str]) -> Dict:
    """The filled-entry shape the position manager expects.

    `remaining_quantity` is 0 and no order id is passed through as
    something to cancel: adoption records what is already held and must
    not reach out and cancel anything as a side effect.
    """
    return {
        "order_id": None,
        "broker_order_id": broker_order_id,
        "client_order_id": client_order_id,
        "symbol": position.get("symbol"),
        "side": "BUY",
        "status": "FILLED",
        "filled_quantity": position.get("quantity"),
        "remaining_quantity": 0.0,
        "average_fill_price": position.get("average_entry_price"),
    }


def adopt_external_positions(broker, position_manager, *, session_date: str,
                             ledger=None, decisions=None,
                             max_trade_risk: float = 2.0,
                             config_version: str = "") -> Dict:
    """Reconcile the venue's positions into the agent's own store.

    Never raises. Everything that could not be established is reported,
    and anything unestablished blocks new exposure rather than being
    treated as absent.
    """
    out: Dict = {
        "session_date": session_date,
        "discovered": 0, "adopted": 0, "already_managed": 0,
        "preexisting": [], "unknown_origin": [], "refused": [],
        "adopted_symbols": [], "classifications": [],
        "integrity": INTEGRITY_COMPLETE,
        "blocks_new_exposure": False,
        "errors": [],
    }

    try:
        positions = broker.get_positions()
    except Exception as exc:                              # noqa: BLE001
        # Not an empty list. We do not know what is held, which is a
        # reason to open nothing.
        out["integrity"] = INTEGRITY_UNKNOWN
        out["blocks_new_exposure"] = True
        out["errors"].append(f"broker positions unreadable: {exc}")
        log_event("adoption_positions_unreadable", session_date=session_date,
                  error=str(exc)[:200])
        return out

    if positions is None:
        out["integrity"] = INTEGRITY_UNKNOWN
        out["blocks_new_exposure"] = True
        out["errors"].append("broker returned no position list")
        return out

    try:
        managed = {p.symbol for p in position_manager.open_positions()}
    except Exception as exc:                              # noqa: BLE001
        out["integrity"] = INTEGRITY_UNKNOWN
        out["blocks_new_exposure"] = True
        out["errors"].append(f"managed positions unreadable: {exc}")
        return out

    # Evidence. Each source may be unreadable, and None means
    # "unreadable", which classify_position treats as UNKNOWN rather
    # than as absence of evidence.
    broker_orders: Optional[List[Dict]]
    try:
        broker_orders = list(broker.get_orders() or [])
    except Exception as exc:                              # noqa: BLE001
        broker_orders = None
        out["integrity"] = INTEGRITY_PARTIAL
        out["errors"].append(f"broker order history unreadable: {exc}")

    ledger_rows = []
    if ledger is not None:
        try:
            ledger_rows = list(ledger.for_session(session_date))
        except Exception as exc:                          # noqa: BLE001
            out["integrity"] = INTEGRITY_PARTIAL
            out["errors"].append(f"order ledger unreadable: {exc}")

    decision_rows = []
    if decisions is not None:
        try:
            decision_rows = list(decisions.for_session(session_date) or [])
        except Exception as exc:                          # noqa: BLE001
            out["integrity"] = INTEGRITY_PARTIAL
            out["errors"].append(f"decision log unreadable: {exc}")

    for position in positions:
        symbol = position.get("symbol")
        if not symbol:
            continue
        out["discovered"] += 1
        if symbol in managed:
            # Idempotent by construction: a position already in the
            # store is not adopted again, so repeated discovery cannot
            # create a second managed record.
            out["already_managed"] += 1
            continue

        verdict = classify_position(symbol, broker_orders,
                                    ledger_rows=ledger_rows,
                                    decisions=decision_rows)
        out["classifications"].append(verdict)

        if verdict["origin"] == ORIGIN_PREEXISTING_EXTERNAL:
            # Somebody else's. Not adopted and not closed: closing a
            # position whose origin is not ours would be the second
            # mistake, and a worse one.
            out["preexisting"].append(symbol)
            log_event("position_preexisting_external", symbol=symbol,
                      detail=verdict["detail"][:200])
            continue

        if verdict["origin"] == ORIGIN_UNKNOWN or not verdict.get("proven"):
            # Includes the prefix-only case, where `adoptable` is True
            # and `proven` is False. A prefix is a naming convention,
            # not evidence of ownership.
            out["unknown_origin"].append(symbol)
            out["blocks_new_exposure"] = True
            log_event("position_unknown_origin", symbol=symbol,
                      evidence=verdict["evidence"],
                      detail=verdict["detail"][:200])
            continue

        # Proven the agent's own. Adopt it.
        if verdict["evidence"] not in (EVIDENCE_LEDGER,
                                       EVIDENCE_RECONSTRUCTED_ID):
            # Belt and braces: `proven` already means this, and a second
            # explicit check costs nothing and cannot be weakened by a
            # change to the flag's definition elsewhere.
            out["unknown_origin"].append(symbol)
            out["blocks_new_exposure"] = True
            continue

        entry = position.get("average_entry_price")
        quantity = position.get("quantity")
        risk_decision_id = verdict.get("risk_decision_id")
        if risk_decision_id is None:
            for row in ledger_rows:
                if (str(getattr(row, "symbol", "")).upper()
                        == str(symbol).upper()):
                    risk_decision_id = getattr(row, "risk_decision_id", None)
                    break
        try:
            built = build_adoption_plan(
                entry, quantity, max_trade_risk,
                _recover_stop_distance_pct(risk_decision_id, decision_rows))
        except Exception as exc:                          # noqa: BLE001
            out["refused"].append({"symbol": symbol, "reason": str(exc)[:200]})
            out["blocks_new_exposure"] = True
            out["integrity"] = (INTEGRITY_PARTIAL
                                if out["integrity"] == INTEGRITY_COMPLETE
                                else out["integrity"])
            log_event("position_adoption_refused", symbol=symbol,
                      error=str(exc)[:200])
            continue

        order = _synthetic_entry_order(
            position, verdict.get("client_order_id"),
            verdict.get("broker_order_id"))
        try:
            position_manager.open_from_order(
                order, built["plan"],
                hypothesis_id=verdict.get("hypothesis_id"),
                risk_decision_id=risk_decision_id,
                config_version=config_version)
        except Exception as exc:                          # noqa: BLE001
            out["refused"].append({"symbol": symbol, "reason": str(exc)[:200]})
            out["blocks_new_exposure"] = True
            out["integrity"] = (INTEGRITY_PARTIAL
                                if out["integrity"] == INTEGRITY_COMPLETE
                                else out["integrity"])
            log_event("position_adoption_refused", symbol=symbol,
                      error=str(exc)[:200])
            continue

        out["adopted"] += 1
        out["adopted_symbols"].append(symbol)
        log_event("position_adopted", symbol=symbol,
                  quantity=quantity, entry_price=entry,
                  evidence=verdict["evidence"],
                  client_order_id=verdict.get("client_order_id"),
                  risk_decision_id=risk_decision_id,
                  stop_price=built["plan"].stop_price,
                  stop_origin=built["stop_origin"],
                  detail="provenance proved this position is the agent's "
                         "own; it is now managed and can be exited")
    return out
