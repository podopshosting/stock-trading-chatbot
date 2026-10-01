"""Split history. Type comes from the ratio, and price history is never
read as a crash without checking for a split on that date."""
from __future__ import annotations

from datetime import date
from typing import Dict, Optional, Sequence

from .models import SplitEvent


def summarise(splits: Sequence[SplitEvent], today: date,
              recent_days: int = 365) -> Dict:
    ordered = sorted(splits, key=lambda s: s.ex_date)
    last = ordered[-1] if ordered else None
    return {
        "count": len(ordered),
        "last": None if not last else {
            "ex_date": last.ex_date, "ratio": last.ratio,
            "type": str(last.type)},
        "recent": bool(last and (today - date.fromisoformat(
            last.ex_date[:10])).days <= recent_days),
    }


def explains_price_drop(splits: Sequence[SplitEvent], day: str,
                        pct_change: float) -> Optional[str]:
    """If a large one-day move coincides with a split, say so rather than
    letting an unadjusted series read as a crash (or a surge)."""
    for s in splits:
        if s.ex_date[:10] == day[:10]:
            expected = (1 / s.ratio - 1) * 100
            if abs(pct_change - expected) < 10 or abs(pct_change) > 40:
                return (f"{s.type} {s.new_rate:g}:{s.old_rate:g} on "
                        f"{s.ex_date}; the move is a split adjustment")
    return None
