"""
A broker for replay.

Wraps the paper broker so fills happen at the NEXT bar's open rather
than the current bar's close, and models stop fills against the next
bar's actual range.

Why that matters more than it sounds: a backtest that decides on bar N's
close and fills at bar N's close has used the fill price as an input to
the decision. Every strategy becomes profitable under that rule, because
the system is effectively buying at a price it has already seen. The
error is invisible in the output - the equity curve just looks good.

The stop modelling is the other half. A polled stop in live trading is
checked once a cycle; in replay the honest equivalent is to ask whether
the next bar's LOW went through the stop, and if so to fill at the worse
of the stop price and that bar's open. Gapping through a stop is normal
and a backtest that fills every stop exactly at its level understates
losses systematically.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..broker.models import Quote
from ..broker.paper import PaperBroker, PaperBrokerConfig
from ..observability import log_event
from .clock import LookaheadError, ReplayClock
from .data import PointInTimeSeries


class ReplayBroker:
    """Executes against historical bars with realistic timing."""

    def __init__(self, clock: ReplayClock,
                 series: Dict[str, PointInTimeSeries],
                 config: Optional[PaperBrokerConfig] = None,
                 spread_pct: float = 0.05):
        # The quoted spread, as a PERCENT of price. Default 0.05%
        # matches what this used to hardcode. Configurable so a
        # scenario can test the spread gate against a spread that
        # is actually wide - otherwise 'the gate works' is a claim
        # about a number the harness could never vary.
        self.spread_fraction = spread_pct / 100.0
        self.clock = clock
        self.series = series
        self.paper = PaperBroker(config or PaperBrokerConfig())
        self._deferred: List[Dict] = []
        self.unfillable_orders: int = 0

    # --- quotes ----------------------------------------------------------

    def sync_quotes(self) -> None:
        """Publish the current bar's close as the prevailing quote.

        The close is legitimate information at the close. It is used for
        valuation and for evaluating exits, never as a fill price.
        """
        for symbol, series in self.series.items():
            if self.clock.index >= len(series):
                continue
            bar = series.current()
            spread = max(0.01, bar.close * self.spread_fraction)
            self.paper.set_quote(Quote(
                symbol=symbol, bid=bar.close - spread / 2,
                ask=bar.close + spread / 2, last=bar.close,
                as_of=bar.timestamp))

    # --- entries ---------------------------------------------------------

    def submit_entry(self, symbol: str, notional: float,
                     **provenance) -> Dict:
        """Buy at the next bar's open.

        Refuses rather than inventing a fill when there is no next bar.
        A backtest that fills on the final bar has added a trade that
        could not have happened, and it will be one of the largest
        trades in the sample if the run ends on a spike.
        """
        series = self.series.get(symbol)
        if series is None:
            raise LookaheadError(f"no bar series for {symbol}")

        fill_price = series.next_open()
        if fill_price is None:
            self.unfillable_orders += 1
            log_event("replay_order_unfillable", symbol=symbol,
                      detail=("decided on the final bar; there is no "
                              "subsequent open to fill against"))
            return {"status": "REJECTED", "symbol": symbol,
                    "reject_reason": "NO_NEXT_BAR", "filled_quantity": 0.0,
                    "fills": [], "order_id": None}

        quantity = notional / fill_price
        # Quote the broker at the fill price so the paper broker's own
        # spread and slippage model applies to the correct reference.
        self.paper.set_quote(Quote(symbol=symbol, bid=fill_price,
                                   ask=fill_price, last=fill_price))
        order = self.paper.submit_order(symbol, "BUY", quantity,
                                        **provenance)
        log_event("replay_entry_filled", symbol=symbol,
                  bar_index=self.clock.index + 1,
                  fill_price=round(fill_price, 4),
                  quantity=round(quantity, 6),
                  detail="filled at the next bar's open, not this close")
        return order

    # --- exits -----------------------------------------------------------

    def resolve_exit(self, symbol: str, stop_price: Optional[float],
                     intent: str = "EXIT") -> Dict:
        """Close a position on the next bar.

        If a stop is supplied and the next bar traded through it, the
        fill is the WORSE of the stop and that bar's open - which is how
        a gap actually resolves. Otherwise the exit is at the open,
        because that is the first price available after the decision.
        """
        series = self.series.get(symbol)
        if series is None:
            raise LookaheadError(f"no bar series for {symbol}")

        nxt = series.next_bar_range()
        if nxt is None:
            # End of data. Value the position at the last close rather
            # than pretending it was closed at a price of our choosing.
            final = series.final_close()
            if final is None:
                raise LookaheadError(f"no prices at all for {symbol}")
            fill_price = final
            gapped = False
        else:
            fill_price = nxt["open"]
            gapped = False
            if stop_price is not None and nxt["low"] <= stop_price:
                # The stop was reached during the bar. If the bar opened
                # below the stop the gap is the fill; otherwise the stop
                # level is.
                fill_price = min(stop_price, nxt["open"])
                gapped = nxt["open"] < stop_price

        self.paper.set_quote(Quote(symbol=symbol, bid=fill_price,
                                   ask=fill_price, last=fill_price))
        order = self.paper.close_position(symbol, intent=intent)
        log_event("replay_exit_filled", symbol=symbol,
                  fill_price=round(fill_price, 4), gapped_through_stop=gapped,
                  stop_price=(None if stop_price is None
                              else round(stop_price, 4)),
                  detail=("gapped through the stop; the fill is worse than "
                          "the stop level" if gapped else
                          "filled at the next bar's open"))
        return order

    # --- passthrough -----------------------------------------------------

    def get_positions(self) -> List[Dict]:
        return self.paper.get_positions()

    def get_position(self, symbol: str) -> Optional[Dict]:
        return self.paper.get_position(symbol)

    def get_account(self) -> Dict:
        return self.paper.get_account()

    def cancel_order(self, order_id: str) -> Dict:
        return self.paper.cancel_order(order_id)

    def get_order(self, order_id: str) -> Optional[Dict]:
        return self.paper.get_order(order_id)

    def close_position(self, symbol: str, intent: str = "EXIT") -> Dict:
        """Present for interface compatibility with PositionManager.

        Routed through resolve_exit without a stop, so it still fills on
        the next bar rather than the current close.
        """
        return self.resolve_exit(symbol, None, intent=intent)
