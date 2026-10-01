"""
A fake Alpaca paper venue, for failure injection.

It models the venue closely enough to exercise the adapter's
idempotency logic, and it is deliberately hostile: it can lose the
response to a request it already processed, drop a request before
processing it, and handle a duplicate client_order_id in three
different ways.

Three duplicate policies, because Alpaca's documentation does not say
which one is real:

  REJECT      the duplicate is refused with HTTP 422
  ACCEPT      the duplicate is accepted as a SECOND order (the worst case)
  RETURN      the first order is silently returned

An adapter that is only safe under REJECT is not safe. These tests run
the critical scenario under all three.
"""
from __future__ import annotations

import itertools
from typing import Dict, List, Optional

from agent.broker.alpaca_paper import HttpResult, TransportError

REJECT, ACCEPT, RETURN = "REJECT", "ACCEPT", "RETURN"


class FakeAlpaca:
    def __init__(self, duplicate_policy: str = REJECT, cash: float = 100000.0,
                 price: float = 100.0):
        self.duplicate_policy = duplicate_policy
        self.cash = cash
        self.price = price
        self.orders: Dict[str, Dict] = {}
        self.positions: Dict[str, Dict] = {}
        self._ids = itertools.count(1)

        # Hostility switches. Each is consumed by ONE request unless
        # noted, so a test can say "lose exactly this response".
        self.lose_response_after_processing = 0
        self.drop_before_processing = 0
        self.fail_with_status: Optional[int] = None
        self.fail_with_status_after_processing: Optional[int] = None
        self.reject_next_order: Optional[str] = None
        self.fill_fraction = 1.0           # <1 gives partial fills
        self.leave_orders_open = False
        self.unreadable = False            # every call raises
        self.lookup_endpoint_missing = False
        # Eventual consistency: the next N order lookups see
        # nothing, as if the order had not propagated yet.
        self.hidden_lookups = 0
        self.requests: List[tuple] = []    # (method, path)

    # --- helpers --------------------------------------------------------

    @property
    def post_count(self) -> int:
        return sum(1 for m, p in self.requests
                   if m == "POST" and p == "/v2/orders")

    def order_count(self) -> int:
        return len(self.orders)

    def _path(self, url: str) -> str:
        return "/" + url.split("//", 1)[1].split("/", 1)[1]

    def _apply_fill(self, order: Dict) -> None:
        qty = float(order["qty"])
        if self.leave_orders_open:
            order["status"] = "new"
            return
        filled = qty * self.fill_fraction
        order["filled_qty"] = f"{filled:.9f}"
        order["filled_avg_price"] = f"{self.price:.4f}"
        order["status"] = "filled" if self.fill_fraction >= 1 \
            else "partially_filled"
        order["filled_at"] = "2026-10-01T14:00:00Z"
        sym = order["symbol"]
        sign = 1 if order["side"] == "buy" else -1
        pos = self.positions.setdefault(
            sym, {"symbol": sym, "qty": "0", "avg_entry_price":
                  f"{self.price}", "current_price": f"{self.price}",
                  "market_value": "0", "unrealized_pl": "0",
                  "cost_basis": "0", "side": "long"})
        new_qty = float(pos["qty"]) + sign * filled
        pos["qty"] = f"{new_qty:.9f}"
        pos["market_value"] = f"{new_qty * self.price:.2f}"
        pos["cost_basis"] = f"{new_qty * self.price:.2f}"
        if abs(new_qty) < 1e-9:
            del self.positions[sym]

    def _create(self, body: Dict) -> Dict:
        oid = f"ord-{next(self._ids)}"
        order = {"id": oid, "client_order_id": body["client_order_id"],
                 "symbol": body["symbol"], "side": body["side"],
                 "type": body["type"], "time_in_force": "day",
                 "qty": body["qty"], "limit_price": body.get("limit_price"),
                 "filled_qty": "0", "filled_avg_price": None,
                 "status": "new", "created_at": "2026-10-01T14:00:00Z",
                 "submitted_at": "2026-10-01T14:00:00Z"}
        self.orders[oid] = order
        if self.reject_next_order:
            order["status"] = "rejected"
            self.reject_next_order = None
        else:
            self._apply_fill(order)
        return order

    def _by_client(self, cid: str) -> List[Dict]:
        return [o for o in self.orders.values()
                if o["client_order_id"] == cid]

    # --- the transport interface ---------------------------------------

    def request(self, method: str, url: str, params=None, json_body=None):
        path = self._path(url)
        self.requests.append((method, path))

        if self.unreadable:
            raise TransportError("ConnectionError")

        if method == "GET" and path == "/v2/account":
            return HttpResult(200, {
                "id": "acct-1", "cash": f"{self.cash}",
                "buying_power": f"{self.cash}", "equity": f"{self.cash}",
                "currency": "USD", "trading_blocked": False,
                "account_blocked": False, "pattern_day_trader": False})

        if method == "GET" and path == "/v2/positions":
            return HttpResult(200, list(self.positions.values()))

        if method == "GET" and path == "/v2/orders":
            if self.hidden_lookups > 0:
                self.hidden_lookups -= 1
                return HttpResult(200, [])
            return HttpResult(200, list(self.orders.values()))

        if method == "GET" and path == "/v2/orders:by_client_order_id":
            if self.lookup_endpoint_missing:
                return HttpResult(404, {"message": "not found"})
            if self.hidden_lookups > 0:
                self.hidden_lookups -= 1
                return HttpResult(404, {"message": "order not found"})
            found = self._by_client(params["client_order_id"])
            return (HttpResult(200, found[0]) if found
                    else HttpResult(404, {"message": "order not found"}))

        if method == "GET" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            o = self.orders.get(oid)
            return HttpResult(200, o) if o else HttpResult(404, {})

        if method == "DELETE" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            o = self.orders.get(oid)
            if not o:
                return HttpResult(404, {})
            if o["status"] in ("filled", "canceled", "expired"):
                return HttpResult(422, {"message": "order not cancelable"})
            o["status"] = "canceled"
            return HttpResult(204, None)

        if method == "DELETE" and path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[1]
            pos = self.positions.get(sym)
            if not pos:
                return HttpResult(404, {})
            order = self._create({
                "client_order_id": f"close-{next(self._ids)}",
                "symbol": sym, "side": "sell", "type": "market",
                "qty": pos["qty"]})
            return HttpResult(200, order)

        if method == "POST" and path == "/v2/orders":
            return self._post_order(json_body)

        return HttpResult(404, {"message": f"unhandled {method} {path}"})

    def _post_order(self, body: Dict):
        if self.drop_before_processing > 0:
            self.drop_before_processing -= 1
            raise TransportError("Timeout")        # never processed

        if self.fail_with_status is not None:
            status, self.fail_with_status = self.fail_with_status, None
            return HttpResult(status, {"message": "injected"})

        existing = self._by_client(body["client_order_id"])
        if existing:
            if self.duplicate_policy == REJECT:
                return HttpResult(422, {"message":
                                        "client_order_id must be unique"})
            if self.duplicate_policy == RETURN:
                return HttpResult(200, existing[0])
            # ACCEPT falls through and creates a second order.

        order = self._create(body)

        if self.fail_with_status_after_processing is not None:
            status = self.fail_with_status_after_processing
            self.fail_with_status_after_processing = None
            return HttpResult(status, {"message": "injected after"})

        if self.lose_response_after_processing > 0:
            self.lose_response_after_processing -= 1
            raise TransportError("Timeout")        # processed, reply lost

        return HttpResult(200, order)
