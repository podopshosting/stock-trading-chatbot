"""
Daily counters, derived from the journal rather than from memory.

The cycle Lambda used to pass `capital_deployed` as the cost basis of
whatever was open RIGHT NOW and let `positions_opened_today` default to
zero. Both reset the moment a position closed, so the daily capital
ceiling and the daily new-position cap could be bypassed simply by
closing one position and opening another across cycles. And
`realized_pnl` came from the broker account, which is cumulative since
the account was created, so the daily loss limit would have been
measured against all-time P&L.

None of that was visible because the pilot never traded. These counters
are derived from the day's journal plus what is open now, so they
survive a cold start and cannot be reset by closing a position.

Capital deployed is cumulative gross: every dollar committed to an entry
today counts, even if the position has since closed. That is the
conservative reading of a daily allocation - recycling the same dollars
through several round trips is more activity, not less risk.
"""
from __future__ import annotations

from typing import Dict


def daily_counters(journal, positions, session_date: str) -> Dict:
    """What has been committed and lost TODAY."""
    trades = journal.list_trades(session_date=session_date)
    open_positions = positions.open_positions()

    closed_cost = sum(t.quantity * t.entry_price for t in trades)
    open_cost = sum(p.cost_basis for p in open_positions)

    return {
        "capital_deployed_today": closed_cost + open_cost,
        "positions_opened_today": len(trades) + len(open_positions),
        "realized_pnl_today": sum(t.net_pnl for t in trades),
        "unrealized_pnl": positions.total_unrealized_pnl() or 0.0,
        "trades_closed_today": len(trades),
        "open_positions": len(open_positions),
    }
