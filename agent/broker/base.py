"""
Broker abstraction.

The strategy and risk layers must never know which broker is underneath.
That is not tidiness: it is what makes the paper record comparable with
the live one, because both go through the same call sites.

The broker is AUTHORITATIVE for orders, fills, positions and cash. The
agent's own view is a cache and must be reconciled against this.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class BrokerAdapter(ABC):
    name: str = "unknown"
    is_paper: bool = True

    # Does submit_order reach a venue outside this process?
    #
    # The distinction is not cosmetic. The in-process simulator's orders
    # are serialised with its own state, so an order it accepted is
    # recoverable after a crash. An order sent over the network exists
    # only at the venue, and nothing in this system would know it had
    # been placed. That is the condition that produces an orphan, so an
    # external submit requires a durable intent record first and the
    # default here is False precisely so that a new adapter has to say
    # so deliberately rather than inherit the laxer path.
    is_external_venue: bool = False

    @abstractmethod
    def get_account(self) -> Dict: ...

    @abstractmethod
    def get_positions(self) -> List[Dict]: ...

    @abstractmethod
    def get_orders(self, status: Optional[str] = None) -> List[Dict]: ...

    @abstractmethod
    def submit_order(self, symbol: str, side: str, quantity: float,
                     order_type: str = "MARKETABLE_LIMIT",
                     limit_price: Optional[float] = None,
                     time_in_force: str = "DAY",
                     client_order_id: Optional[str] = None,
                     hypothesis_id: Optional[str] = None,
                     risk_decision_id: Optional[str] = None,
                     intent: str = "") -> Dict: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> Dict: ...

    @abstractmethod
    def replace_order(self, order_id: str, quantity: Optional[float] = None,
                      limit_price: Optional[float] = None) -> Dict: ...

    @abstractmethod
    def close_position(self, symbol: str, intent: str = "EXIT") -> Dict: ...

    def capabilities(self) -> Dict:
        return {"name": self.name, "is_paper": self.is_paper}
