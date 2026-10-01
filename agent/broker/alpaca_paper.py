"""
Alpaca PAPER broker adapter.

This talks to Alpaca's paper-trading environment and nothing else. The
host is a module constant, the constructor refuses any other base URL,
and the HTTP layer re-checks the host of every request, so there is no
configuration - environment variable, constructor argument, or secret
content - by which this class can reach a live-money endpoint.

Status of verification. Written against Alpaca's public API reference.
It has NOT been exercised against the real service, because confirming
that the stored credential is a trading credential (rather than data
only) was not permitted. Treat every behaviour below as specified, not
observed, until a credential check is allowed.

Idempotency, which is the reason this file is as long as it is
-------------------------------------------------------------
Alpaca's documentation does not say what a duplicate client_order_id
returns. So this adapter does not depend on the answer. It is safe
whether the broker rejects the duplicate, accepts it as a second order,
or silently returns the first:

1. Before EVERY submission it looks the client_order_id up. If the
   order exists it is returned and nothing is posted. This is what makes
   "submit, response lost, retry" place ONE order, and it works across a
   cold start with no local state, because the broker is the memory.

2. A POST is never retried. If the POST's outcome is uncertain
   (timeout, dropped connection), the adapter looks the order up. Found:
   returned. Not found: it raises UncertainSubmission and QUARANTINES the
   id, because "not found yet" and "never sent" are indistinguishable
   while the request may still be in flight.

3. A quarantined id is never posted again inside the quarantine window.
   After it, step 1 runs first, so a late-arriving original is found
   rather than duplicated.

An uncertain order is treated as a halting condition upstream. The
agent does not carry on trading around an order it cannot account for.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from ..observability import log_event
from .base import BrokerAdapter

# The ONLY host this adapter will ever talk to.
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
PAPER_HOST = "paper-api.alpaca.markets"

# Long enough for an in-flight request to land, short enough not to
# strand a legitimate retry for the rest of the session.
QUARANTINE_SECONDS = 180.0

REQUEST_TIMEOUT_SECONDS = 10.0


class AlpacaPaperError(Exception):
    pass


class NotPaperEndpoint(AlpacaPaperError):
    """A request or configuration pointed somewhere other than paper."""


class UncertainSubmission(AlpacaPaperError):
    """A submission whose outcome cannot be established.

    Carries the client order id so the caller can persist it and the
    next cycle can resolve it by lookup rather than resubmitting.
    """

    def __init__(self, client_order_id: str, detail: str = ""):
        self.client_order_id = client_order_id
        super().__init__(
            f"order {client_order_id} may or may not have been placed: "
            f"{detail}")


class TransportError(Exception):
    """The request may or may not have reached the server."""


class HttpResult:
    def __init__(self, status: int, body=None):
        self.status = status
        self.body = body


def _assert_paper(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host != PAPER_HOST:
        raise NotPaperEndpoint(
            f"refusing to contact {host!r}; this adapter talks only to "
            f"{PAPER_HOST}")


class RequestsTransport:
    """The real transport. Imported lazily so tests need no network."""

    def __init__(self, key_id: str, secret_key: str):
        # Held privately and never logged or included in any exception.
        self._headers = {"APCA-API-KEY-ID": key_id,
                         "APCA-API-SECRET-KEY": secret_key}

    def request(self, method: str, url: str, params=None,
                json_body=None) -> HttpResult:
        _assert_paper(url)
        import requests
        try:
            response = requests.request(
                method, url, headers=self._headers, params=params,
                json=json_body, timeout=REQUEST_TIMEOUT_SECONDS)
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Do not include the exception text: some libraries echo
            # request headers.
            raise TransportError(type(exc).__name__) from None
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        return HttpResult(response.status_code, body)


# Alpaca order status -> this system's status vocabulary.
_STATUS = {
    "new": "SUBMITTED", "accepted": "SUBMITTED", "pending_new": "PENDING",
    "accepted_for_bidding": "SUBMITTED", "partially_filled":
    "PARTIALLY_FILLED", "filled": "FILLED", "done_for_day": "EXPIRED",
    "canceled": "CANCELLED", "expired": "EXPIRED", "replaced": "CANCELLED",
    "pending_cancel": "PENDING", "pending_replace": "PENDING",
    "rejected": "REJECTED", "suspended": "REJECTED", "stopped": "SUBMITTED",
    "calculated": "SUBMITTED",
}


def normalise_order(raw: Dict) -> Dict:
    """Map an Alpaca order onto this system's order shape.

    An unrecognised status maps to PENDING, not to a terminal state: an
    order we cannot classify must not be treated as finished.
    """
    status = _STATUS.get(str(raw.get("status", "")).lower(), "PENDING")
    qty = float(raw.get("qty") or 0.0)
    filled = float(raw.get("filled_qty") or 0.0)
    avg = raw.get("filled_avg_price")
    avg = float(avg) if avg not in (None, "") else None
    fills = []
    if filled > 0 and avg is not None:
        fills.append({"price": avg, "quantity": filled, "value": avg * filled,
                      "slippage_bps": None,
                      "filled_at": raw.get("filled_at")})
    return {
        "order_id": raw.get("id"),
        "client_order_id": raw.get("client_order_id"),
        "symbol": raw.get("symbol"),
        "side": str(raw.get("side", "")).upper(),
        "order_type": str(raw.get("type", "")).upper(),
        "quantity": qty,
        "limit_price": (float(raw["limit_price"])
                        if raw.get("limit_price") else None),
        "time_in_force": str(raw.get("time_in_force", "")).upper(),
        "status": status,
        "filled_quantity": filled,
        "remaining_quantity": max(0.0, qty - filled),
        "average_fill_price": avg,
        "notional": (avg * filled) if avg is not None else None,
        "fills": fills,
        "created_at": raw.get("created_at"),
        "submitted_at": raw.get("submitted_at"),
        "updated_at": raw.get("updated_at"),
        "reject_reason": None if status != "REJECTED" else "BROKER_REJECTED",
        "reject_detail": "",
        "raw_status": raw.get("status"),
    }


class AlpacaPaperBroker(BrokerAdapter):
    name = "alpaca_paper"
    is_paper = True

    def __init__(self, transport=None, base_url: str = PAPER_BASE_URL,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 quarantine_seconds: float = QUARANTINE_SECONDS):
        _assert_paper(base_url)
        if base_url.rstrip("/") != PAPER_BASE_URL:
            raise NotPaperEndpoint(
                f"base_url must be {PAPER_BASE_URL}, got {base_url!r}")
        self.base_url = PAPER_BASE_URL
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self.quarantine_seconds = quarantine_seconds
        # client_order_id -> time it became uncertain.
        self._quarantine: Dict[str, float] = {}
        # Every id this instance has POSTed, so a duplicate within one
        # process is refused even if the lookup path were unavailable.
        self._posted: set = set()
        self.duplicate_attempts: int = 0

    # --- plumbing --------------------------------------------------------

    def _call(self, method, path, params=None, json_body=None):
        if self._transport is None:
            raise AlpacaPaperError("no transport configured")
        url = f"{self.base_url}{path}"
        _assert_paper(url)
        return self._transport.request(method, url, params=params,
                                       json_body=json_body)

    def capabilities(self) -> Dict:
        return {"name": self.name, "is_paper": True,
                "shorting": False, "margin": False, "options": False,
                "models_spread": True, "models_slippage": True,
                "models_partial_fills": True,
                "order_types": ["LIMIT", "MARKETABLE_LIMIT"],
                "authoritative_for_state": True,
                "base_url": self.base_url}

    # --- reads -----------------------------------------------------------

    def get_account(self) -> Dict:
        result = self._call("GET", "/v2/account")
        if result.status != 200 or not isinstance(result.body, dict):
            raise AlpacaPaperError(
                f"account read failed: HTTP {result.status}")
        b = result.body
        cash = float(b.get("cash") or 0.0)
        return {
            "account_id": b.get("id"),
            "cash": cash,
            "buying_power": float(b.get("buying_power") or 0.0),
            "equity": float(b.get("equity") or 0.0),
            "reserved_cash": max(0.0, cash - float(
                b.get("non_marginable_buying_power") or cash)),
            "is_paper": True,
            "currency": b.get("currency", "USD"),
            "trading_blocked": bool(b.get("trading_blocked")),
            "account_blocked": bool(b.get("account_blocked")),
            "pattern_day_trader": bool(b.get("pattern_day_trader")),
            "realized_pnl": None,
        }

    def get_positions(self) -> List[Dict]:
        """Authoritative positions. Raises on failure: an unreadable
        broker is an unknown position state, not an empty one."""
        result = self._call("GET", "/v2/positions")
        if result.status != 200 or not isinstance(result.body, list):
            raise AlpacaPaperError(
                f"positions read failed: HTTP {result.status}")
        out = []
        for p in result.body:
            qty = float(p.get("qty") or 0.0)
            out.append({
                "symbol": p.get("symbol"), "quantity": qty,
                "average_entry_price": float(p.get("avg_entry_price") or 0),
                "current_price": float(p.get("current_price") or 0) or None,
                "market_value": float(p.get("market_value") or 0),
                "unrealized_pnl": float(p.get("unrealized_pl") or 0),
                "cost_basis": float(p.get("cost_basis") or 0),
                "side": p.get("side"),
            })
        return out

    def get_position(self, symbol: str) -> Optional[Dict]:
        for p in self.get_positions():
            if p["symbol"] == symbol:
                return p
        return None

    def get_orders(self, status: Optional[str] = None) -> List[Dict]:
        result = self._call("GET", "/v2/orders", params={
            "status": (status or "all").lower(), "limit": 500,
            "direction": "desc"})
        if result.status != 200 or not isinstance(result.body, list):
            raise AlpacaPaperError(
                f"orders read failed: HTTP {result.status}")
        return [normalise_order(o) for o in result.body]

    def get_order(self, order_id: str) -> Optional[Dict]:
        result = self._call("GET", f"/v2/orders/{order_id}")
        if result.status == 404:
            return None
        if result.status != 200 or not isinstance(result.body, dict):
            raise AlpacaPaperError(f"order read failed: HTTP {result.status}")
        return normalise_order(result.body)

    # --- lookup by client id ----------------------------------------------

    def find_by_client_order_id(self, client_order_id: str) -> Optional[Dict]:
        """Definitive lookup, or raise.

        Returns the order if it exists, None only if absence is
        confirmed. A 404 from the by-id endpoint is cross-checked
        against the order list, because a 404 from an endpoint that does
        not behave as assumed would otherwise read as "never placed" -
        and that misreading is exactly what produces a duplicate.
        """
        result = self._call("GET", "/v2/orders:by_client_order_id",
                            params={"client_order_id": client_order_id})
        if result.status == 200 and isinstance(result.body, dict):
            return normalise_order(result.body)
        if result.status not in (404, 422):
            raise AlpacaPaperError(
                f"lookup failed: HTTP {result.status}")
        for order in self.get_orders("all"):
            if order.get("client_order_id") == client_order_id:
                return order
        return None

    # --- quarantine --------------------------------------------------------

    def quarantined(self) -> Dict[str, float]:
        now = self._clock()
        return {k: t for k, t in self._quarantine.items()
                if now - t < self.quarantine_seconds}

    def export_state(self) -> Dict:
        """For persistence: the quarantine must survive a cold start, or
        the protection is lost exactly when a Lambda dies mid-request."""
        return {"quarantine": dict(self._quarantine),
                "posted": sorted(self._posted)}

    def import_state(self, data: Dict) -> None:
        self._quarantine = {k: float(v) for k, v in
                            (data or {}).get("quarantine", {}).items()}
        self._posted = set((data or {}).get("posted", []))

    # --- submission ---------------------------------------------------------

    def submit_order(self, symbol: str, side: str, quantity: float,
                     order_type: str = "MARKETABLE_LIMIT",
                     limit_price: Optional[float] = None,
                     time_in_force: str = "DAY",
                     client_order_id: Optional[str] = None,
                     hypothesis_id: Optional[str] = None,
                     risk_decision_id: Optional[str] = None,
                     intent: str = "") -> Dict:
        if not client_order_id:
            # A submission without a deterministic id cannot be made
            # idempotent, so it is refused rather than given a random
            # one that would defeat the whole mechanism.
            raise AlpacaPaperError(
                "a client_order_id is required; an order without a "
                "deterministic id cannot be retried safely")
        if len(client_order_id) > 128:
            raise AlpacaPaperError("client_order_id exceeds 128 characters")
        if quantity <= 0:
            raise AlpacaPaperError("quantity must be positive")
        if limit_price is None:
            # Market orders are not offered anywhere in this system.
            raise AlpacaPaperError("a limit price is required")

        # 3. quarantined ids are not posted again until the window ends.
        if client_order_id in self.quarantined():
            self.duplicate_attempts += 1
            log_event("duplicate_order_attempt", symbol=symbol,
                      client_order_id=client_order_id,
                      detail="id is quarantined after an uncertain submission")
            existing = self.find_by_client_order_id(client_order_id)
            if existing is not None:
                self._quarantine.pop(client_order_id, None)
                return existing
            raise UncertainSubmission(
                client_order_id,
                "an earlier submission is unresolved and the id is "
                "quarantined")

        # 1. look up before posting.
        existing = self.find_by_client_order_id(client_order_id)
        if existing is not None:
            self.duplicate_attempts += 1
            log_event("order_duplicate_suppressed", symbol=symbol,
                      client_order_id=client_order_id,
                      existing_order_id=existing.get("order_id"))
            return existing

        if client_order_id in self._posted:
            # Posted by this instance, yet lookup says absent. That is
            # contradictory, so refuse rather than trust the lookup.
            self.duplicate_attempts += 1
            raise UncertainSubmission(
                client_order_id,
                "posted earlier by this process but not visible at the "
                "broker")

        payload = {
            "symbol": symbol, "side": side.lower(),
            "type": "limit", "time_in_force": "day",
            "qty": f"{quantity:.9f}".rstrip("0").rstrip("."),
            "limit_price": f"{limit_price:.2f}",
            "client_order_id": client_order_id,
        }
        self._posted.add(client_order_id)

        try:
            result = self._call("POST", "/v2/orders", json_body=payload)
        except TransportError as exc:
            return self._resolve_uncertain(client_order_id, symbol, str(exc))

        if 200 <= result.status < 300 and isinstance(result.body, dict):
            order = normalise_order(result.body)
            log_event("order_submitted", symbol=symbol, side=side,
                      order_id=order.get("order_id"), quantity=quantity,
                      order_type=order_type, intent=intent,
                      venue="alpaca_paper")
            order["hypothesis_id"] = hypothesis_id
            order["risk_decision_id"] = risk_decision_id
            order["intent"] = intent
            return order

        if result.status in (408, 429, 500, 502, 503, 504):
            # The server may have processed it before failing.
            return self._resolve_uncertain(
                client_order_id, symbol, f"HTTP {result.status}")

        # A definite rejection (4xx other than the above).
        self._posted.discard(client_order_id)
        message = ""
        if isinstance(result.body, dict):
            message = str(result.body.get("message", ""))[:160]
        log_event("order_rejected", symbol=symbol, side=side,
                  reason="BROKER_REJECTED", detail=message)
        return {"order_id": None, "client_order_id": client_order_id,
                "symbol": symbol, "side": side.upper(),
                "quantity": quantity, "status": "REJECTED",
                "filled_quantity": 0.0, "remaining_quantity": quantity,
                "fills": [], "reject_reason": "BROKER_REJECTED",
                "reject_detail": f"HTTP {result.status}: {message}"}

    def _resolve_uncertain(self, client_order_id: str, symbol: str,
                           why: str) -> Dict:
        """Establish what happened to a POST whose outcome is unknown.

        NEVER resubmits. Looks the order up; if it cannot be found it
        quarantines the id and raises, because absence at this moment is
        not proof the request was never sent.
        """
        found = None
        try:
            found = self.find_by_client_order_id(client_order_id)
            if found is None:
                self._sleep(1.0)
                found = self.find_by_client_order_id(client_order_id)
        except (TransportError, AlpacaPaperError):
            found = None

        if found is not None:
            log_event("order_duplicate_suppressed", symbol=symbol,
                      client_order_id=client_order_id,
                      existing_order_id=found.get("order_id"),
                      detail="resolved an uncertain submission by lookup")
            return found

        self._quarantine[client_order_id] = self._clock()
        log_event("uncertain_order_state", symbol=symbol,
                  client_order_id=client_order_id, reason=why[:80])
        raise UncertainSubmission(client_order_id, why)

    # --- cancel / replace / close -----------------------------------------

    def cancel_order(self, order_id: str) -> Dict:
        result = self._call("DELETE", f"/v2/orders/{order_id}")
        if result.status in (200, 204):
            # A cancel request is not a cancellation: confirm.
            current = self.get_order(order_id)
            return current or {"order_id": order_id, "status": "CANCELLED"}
        if result.status in (404, 422):
            current = self.get_order(order_id)
            if current is not None:
                # Already terminal (e.g. filled). Report what it is, not
                # what we asked for.
                return current
        raise AlpacaPaperError(f"cancel failed: HTTP {result.status}")

    def replace_order(self, order_id: str, quantity: Optional[float] = None,
                      limit_price: Optional[float] = None) -> Dict:
        # Not supported: replace-in-place makes the audit trail
        # ambiguous, and notional orders cannot be replaced at all.
        raise AlpacaPaperError(
            "replace is not supported; cancel and submit a new order")

    def close_position(self, symbol: str, intent: str = "EXIT") -> Dict:
        result = self._call("DELETE", f"/v2/positions/{symbol}")
        if result.status in (200, 207) and isinstance(result.body, dict):
            order = normalise_order(result.body)
            order["intent"] = intent
            return order
        raise AlpacaPaperError(
            f"close failed for {symbol}: HTTP {result.status}")
