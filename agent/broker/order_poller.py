"""
Follow up every external order whose outcome is not yet known.

An order recorded and never followed up is only half a record. The
ledger says what the agent asked for and what it last observed; this
module is what makes the second half true over time.

Three outcomes, and the difference between them matters more than the
mechanism:

  OBSERVED          the venue answered; fold it in
  CONFIRMED ABSENT  the venue says there is no such order, and enough
                    time has passed that absence is conclusive
  UNRESOLVED        we could not find out

UNRESOLVED is not "no exposure". A row that could not be polled keeps
its conservative reservation, because an order that may be live is
exposure whether or not we managed to ask about it.

`NEVER_PLACED` is this system's own conclusion, not a venue status, and
it is reachable ONLY from a confirmed absence after a grace period. The
grace period exists because an order list can lag its own writes by a
moment, and concluding "never placed" from a list that had not caught
up yet would release a reservation against a live order.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..observability import log_event
from .order_ledger import (
    NEVER_PLACED, OrderLedgerError, committed_exposure,
)

# How old an unobserved intent must be before a confirmed absence is
# taken as proof the order never reached the venue.
ABSENCE_GRACE_SECONDS = 60.0

# A bound, so one cycle cannot spend its whole budget polling.
DEFAULT_MAX_ORDERS = 100

INTEGRITY_COMPLETE = "COMPLETE"
INTEGRITY_PARTIAL = "PARTIAL"
INTEGRITY_UNKNOWN = "UNKNOWN"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(row, now: datetime) -> Optional[float]:
    stamp = _parse(row.submitted_at) or _parse(row.intent_at)
    if stamp is None:
        return None
    return (now - stamp).total_seconds()


def _lookup(broker, row):
    """Ask the venue about one order.

    Returns (order_or_None, confirmed). `confirmed` says whether a None
    means "the venue told us there is no such order" as opposed to "we
    could not ask", and the caller must not conflate them.
    """
    # Prefer the broker's own id where the venue has acknowledged one:
    # it is the venue's handle and cannot collide with a client id.
    order_id = getattr(row, "broker_order_id", None)
    if order_id:
        getter = getattr(broker, "get_order", None)
        if callable(getter):
            found = getter(order_id)
            if found is not None:
                return found, True
            # An acknowledged order that the venue no longer returns is
            # NOT a confirmed absence. Something is wrong, and guessing
            # here would release a reservation on an unknown.
            return None, False

    finder = getattr(broker, "find_by_client_order_id", None)
    if callable(finder):
        # The adapter returns None only where absence is CONFIRMED - it
        # cross-checks a 404 against the order list for this reason.
        return finder(row.client_order_id), True

    return None, False


def poll_outstanding(broker, ledger, session_date: str, *,
                     now: Optional[datetime] = None,
                     max_orders: int = DEFAULT_MAX_ORDERS) -> Dict:
    """Resolve what can be resolved, and report what cannot."""
    moment = now or _utcnow()
    stamp = moment.isoformat(timespec="seconds")
    result: Dict = {
        "session_date": session_date,
        "polled": 0, "observed": 0, "became_terminal": 0,
        "still_open": 0, "never_placed": 0,
        "unresolved": 0, "vanished": 0,
        "integrity": INTEGRITY_COMPLETE,
        "exposure": None,
        "exposure_known": False,
        "oldest_pending_age_seconds": None,
        "errors": [],
        "transitions": [],
    }

    try:
        rows = list(ledger.non_terminal(session_date))
    except Exception as exc:                              # noqa: BLE001
        # An unreadable ledger is the one case where exposure itself is
        # unknown: we do not know what is outstanding, so we cannot
        # reserve for it.
        result["integrity"] = INTEGRITY_UNKNOWN
        result["errors"].append(f"ledger unreadable: {exc}")
        log_event("order_poll_ledger_unreadable", session_date=session_date,
                  error=str(exc)[:200])
        return result

    if not any(callable(getattr(broker, name, None))
               for name in ("get_order", "find_by_client_order_id")):
        # Nothing to poll WITH. Reported as unknown integrity rather
        # than as a clean sweep of zero orders.
        result["integrity"] = INTEGRITY_UNKNOWN
        result["errors"].append(
            "this broker offers no order lookup, so outstanding orders "
            "cannot be followed up")
        return result

    truncated = rows[max_orders:]
    rows = rows[:max_orders]
    if truncated:
        # Deliberately PARTIAL. A bounded sweep that reported COMPLETE
        # would say "nothing outstanding" while holding a backlog.
        result["integrity"] = INTEGRITY_PARTIAL
        result["errors"].append(
            f"{len(truncated)} order(s) not polled this pass (bound of "
            f"{max_orders}); they remain outstanding")

    for row in rows:
        result["polled"] += 1
        before = row.status
        try:
            order, confirmed = _lookup(broker, row)
        except Exception as exc:                          # noqa: BLE001
            # Degrade per row. The row stays non-terminal, so its
            # conservative reservation stands.
            result["unresolved"] += 1
            result["integrity"] = (INTEGRITY_PARTIAL
                                   if result["integrity"] != INTEGRITY_UNKNOWN
                                   else result["integrity"])
            result["errors"].append(f"{row.client_order_id}: {exc}")
            log_event("order_poll_failed",
                      client_order_id=row.client_order_id,
                      symbol=row.symbol, error=str(exc)[:200])
            continue

        if order is not None:
            try:
                updated = ledger.record_observation(
                    row.client_order_id, order, stamp)
            except Exception as exc:                      # noqa: BLE001
                result["unresolved"] += 1
                result["integrity"] = (
                    INTEGRITY_PARTIAL
                    if result["integrity"] != INTEGRITY_UNKNOWN
                    else result["integrity"])
                result["errors"].append(
                    f"{row.client_order_id}: observation not stored: {exc}")
                continue
            result["observed"] += 1
            after = getattr(updated, "status", None) or order.get("status")
            if before != after:
                result["transitions"].append({
                    "client_order_id": row.client_order_id,
                    "symbol": row.symbol,
                    "from": before, "to": after,
                    "filled_quantity": getattr(updated, "filled_quantity",
                                               None),
                    "raw_status": order.get("raw_status"),
                })
                log_event("order_status_changed",
                          client_order_id=row.client_order_id,
                          symbol=row.symbol, previous=before, status=after,
                          raw_status=order.get("raw_status"),
                          filled_quantity=getattr(updated,
                                                  "filled_quantity", None))
            if getattr(updated, "is_terminal", False):
                result["became_terminal"] += 1
            else:
                result["still_open"] += 1
            continue

        if not confirmed:
            # The venue acknowledged this order once and will not return
            # it now. Not a confirmed absence, so exposure stands.
            result["vanished"] += 1
            result["unresolved"] += 1
            result["integrity"] = (INTEGRITY_PARTIAL
                                   if result["integrity"] != INTEGRITY_UNKNOWN
                                   else result["integrity"])
            result["errors"].append(
                f"{row.client_order_id}: the venue no longer returns an "
                f"order it had acknowledged; exposure is still reserved")
            log_event("order_vanished_from_venue",
                      client_order_id=row.client_order_id,
                      symbol=row.symbol,
                      broker_order_id=row.broker_order_id)
            continue

        age = _age_seconds(row, moment)
        if row.submission_outcome_known:
            # We have seen this order before and now the venue denies
            # it. That is a contradiction, not an absence.
            result["vanished"] += 1
            result["unresolved"] += 1
            result["integrity"] = (INTEGRITY_PARTIAL
                                   if result["integrity"] != INTEGRITY_UNKNOWN
                                   else result["integrity"])
            result["errors"].append(
                f"{row.client_order_id}: previously observed at the venue "
                f"and now reported absent")
            log_event("order_vanished_from_venue",
                      client_order_id=row.client_order_id,
                      symbol=row.symbol, detail="previously observed")
            continue

        if age is None or age < ABSENCE_GRACE_SECONDS:
            # Too soon. An order list that has not caught up looks
            # exactly like an order that was never placed.
            result["still_open"] += 1
            continue

        try:
            ledger.record_never_placed(row.client_order_id, stamp)
        except Exception as exc:                          # noqa: BLE001
            result["unresolved"] += 1
            result["integrity"] = (INTEGRITY_PARTIAL
                                   if result["integrity"] != INTEGRITY_UNKNOWN
                                   else result["integrity"])
            result["errors"].append(f"{row.client_order_id}: {exc}")
            continue
        result["never_placed"] += 1
        result["became_terminal"] += 1
        result["transitions"].append({
            "client_order_id": row.client_order_id, "symbol": row.symbol,
            "from": before, "to": NEVER_PLACED,
            "detail": "the venue confirms no such order exists",
        })
        log_event("order_never_placed",
                  client_order_id=row.client_order_id, symbol=row.symbol,
                  age_seconds=round(age, 1),
                  detail="absence confirmed by the venue after the grace "
                         "period; the reservation is released")

    # --- what is still outstanding, and what it reserves --------------
    try:
        outstanding = list(ledger.non_terminal(session_date))
    except Exception as exc:                              # noqa: BLE001
        result["integrity"] = INTEGRITY_UNKNOWN
        result["errors"].append(f"ledger unreadable after polling: {exc}")
        return result

    exposure = committed_exposure(outstanding)
    result["exposure"] = exposure
    result["exposure_known"] = bool(exposure.get("known"))
    ages = [a for a in (_age_seconds(r, moment) for r in outstanding)
            if a is not None]
    result["oldest_pending_age_seconds"] = round(max(ages), 1) if ages else None
    result["outstanding"] = len(outstanding)
    return result
