"""
Earnings records and trends.

Three kinds of number are kept apart, always:
  REPORTED_VALUE     what the company reported (SEC XBRL, or a provider's
                     'reported' field)
  CONSENSUS_ESTIMATE what analysts expected (only from a provider that
                     actually supplies it)
  DERIVED_SURPRISE   actual minus estimate, computed here

SEC supplies actuals, not consensus. When no estimate source is
configured every estimate is None and every beat/miss is UNKNOWN - an
estimate is never manufactured, and a missing one is never read as zero.
A "trend" needs enough observations; one quarter is not a trend.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .fundamentals import UNKNOWN, collapse, _d
from .models import EarningsRecord, FinancialPeriod, Provenance

MIN_TREND_OBSERVATIONS = 4


def from_sec(facts: Dict[str, List[FinancialPeriod]]) -> List[EarningsRecord]:
    """Reported actuals per quarter. Estimates stay None; report_date stays
    None (the 10-Q filing date is not the earnings-release date)."""
    eps = {p.period_end: p for p in collapse(facts.get("eps_diluted", []), "Q")}
    rev = {p.period_end: p for p in collapse(facts.get("revenue", []), "Q")}
    out = []
    for end in sorted(set(eps) | set(rev)):
        e, r = eps.get(end), rev.get(end)
        src = e or r
        out.append(EarningsRecord(
            period_end=end, fiscal_period=src.fiscal_period,
            eps_actual=e.value if e else None,
            revenue_actual=r.value if r else None,
            provenance=src.provenance))
    return out


def from_alpha_vantage(payload: Dict, prov: Provenance) -> List[EarningsRecord]:
    """Alpha Vantage EARNINGS. 'reportedEPS' is the ACTUAL and
    'estimatedEPS' the ESTIMATE; they are mapped by name, never by
    position, and a 'None' string is a missing value, not zero."""
    def num(v):
        try:
            return None if v in (None, "", "None") else float(v)
        except (TypeError, ValueError):
            return None
    out = []
    for row in (payload or {}).get("quarterlyEarnings") or []:
        end = row.get("fiscalDateEnding")
        if not end:
            continue
        timing = {"pre-market": "BMO", "post-market": "AMC"}.get(
            str(row.get("reportTime") or "").lower(), "UNKNOWN")
        out.append(EarningsRecord(
            period_end=end, report_date=row.get("reportedDate") or None,
            report_timing=timing, eps_actual=num(row.get("reportedEPS")),
            eps_estimate=num(row.get("estimatedEPS")), provenance=prov))
    return sorted(out, key=lambda r: r.period_end)


def trends(records: Sequence[EarningsRecord]) -> Dict:
    recs = sorted(records, key=lambda r: r.period_end)
    out: Dict = {"observations": len(recs)}

    with_est = [r for r in recs if r.eps_surprise is not None]
    if len(with_est) >= MIN_TREND_OBSERVATIONS:
        last = with_est[-MIN_TREND_OBSERVATIONS:]
        out["eps_beat_miss"] = ["BEAT" if r.eps_surprise > 0 else
                                "MISS" if r.eps_surprise < 0 else "MEET"
                                for r in last]
        out["avg_eps_surprise"] = round(
            sum(r.eps_surprise for r in last) / len(last), 6)
    else:
        out["eps_beat_miss"] = UNKNOWN
        out["avg_eps_surprise"] = UNKNOWN
        out["estimates_note"] = (
            f"{len(with_est)} quarter(s) with a consensus estimate; need "
            f"{MIN_TREND_OBSERVATIONS}. Estimates are never inferred.")

    out["eps_yoy"] = _yoy(recs, "eps_actual")
    out["revenue_yoy"] = _yoy(recs, "revenue_actual")

    actual = [r for r in recs if r.eps_actual is not None][-8:]
    if len(actual) >= MIN_TREND_OBSERVATIONS:
        out["profitable_quarters"] = f"{sum(r.eps_actual > 0 for r in actual)}/{len(actual)}"
    else:
        out["profitable_quarters"] = UNKNOWN
    return out


def _yoy(recs, attr) -> Dict:
    vals = [r for r in recs if getattr(r, attr) is not None]
    if len(vals) < 2:
        return {"value": UNKNOWN, "reason": "not enough quarters"}
    cur = vals[-1]
    for r in reversed(vals[:-1]):
        gap = (_d(cur.period_end) - _d(r.period_end)).days
        if 350 <= gap <= 380:
            base = getattr(r, attr)
            if base <= 0:
                return {"value": UNKNOWN,
                        "reason": "prior-year value not positive"}
            return {"value": round(getattr(cur, attr) / base - 1, 6),
                    "current": cur.period_end, "prior": r.period_end}
    return {"value": UNKNOWN, "reason": "no quarter one year earlier"}
