"""Cross-checks between two sources of the same corporate actions.

Agreement is not required for validity. Disagreement is PRESERVED as a
structured warning, never resolved by picking the plausible number.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

from .models import ActionType, CorporateAction

CONFLICT = "CORPORATE_ACTION_SOURCE_CONFLICT"


def _key(a: CorporateAction):
    return (a.type, a.ex_date)


def cross_check(primary: Sequence[CorporateAction],
                other: Sequence[CorporateAction],
                amount_tolerance: float = 0.005) -> List[Dict]:
    """Compare actions present in both sources by (type, ex_date).

    An action present in only one source is NOT a conflict: coverage
    differs between providers. Only same-key disagreement is flagged.
    """
    theirs = {_key(a): a for a in other if a.ex_date}
    warnings: List[Dict] = []
    for a in primary:
        b = theirs.get(_key(a))
        if not a.ex_date or b is None:
            continue
        diffs = {}
        if (a.amount is not None and b.amount is not None
                and abs(a.amount - b.amount) > amount_tolerance):
            diffs["amount"] = (a.amount, b.amount)
        if a.type in (ActionType.FORWARD_SPLIT, ActionType.REVERSE_SPLIT):
            ra = a.ratio_new / a.ratio_old if a.ratio_new and a.ratio_old else None
            rb = b.ratio_new / b.ratio_old if b.ratio_new and b.ratio_old else None
            if ra and rb and abs(ra - rb) > 1e-9:
                diffs["ratio"] = (ra, rb)
        if diffs:
            warnings.append({
                "code": CONFLICT, "type": str(a.type),
                "ex_date": a.ex_date, "differences": diffs,
                "sources": [a.provenance.provider if a.provenance else None,
                            b.provenance.provider if b.provenance else None],
                "resolution": "NONE_PRESERVED_BOTH"})
    return warnings
