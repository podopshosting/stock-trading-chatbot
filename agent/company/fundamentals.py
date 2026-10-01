"""
Deterministic period selection, TTM, freshness and derived metrics.

Rules (each is a test):
  * A quarter is a 80-100 day duration, a year 350-380 days. Anything
    else (6- and 9-month YTD) is never treated as a quarter.
  * Duplicate observations of the same period collapse to the LATEST
    FILED value (restatements win), and the choice is recorded.
  * TTM needs four sequential reported quarters: no duplicates, no
    overlaps, no gaps. It is never built from three quarters plus an
    annual. When it cannot be built it says why.
  * Two companies' periods are comparable only if their period_end dates
    are within ALIGN_DAYS; otherwise PERIOD_MISMATCH. Fiscal labels
    ("Q3") are never trusted on their own.
  * Freshness comes from the period end date, not the retrieval time.
  * A ratio with a missing/zero denominator, or whose terms come from
    different periods, is UNKNOWN. Nothing is imputed.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .models import FinancialPeriod

UNKNOWN = "UNKNOWN"
PERIOD_MISMATCH = "PERIOD_MISMATCH"
ALIGN_DAYS = 20

FLOW = {"revenue", "gross_profit", "operating_income", "net_income",
        "eps_diluted", "operating_cash_flow", "capex"}


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def kind(p: FinancialPeriod) -> Optional[str]:
    """'Q', 'FY', 'INSTANT' or None (YTD/other). Flow facts only count
    when their duration is a clean quarter or year."""
    if p.period_start is None:
        return "INSTANT"
    n = p.duration_days
    if 80 <= n <= 100:
        return "Q"
    if 350 <= n <= 380:
        return "FY"
    return None


def collapse(periods: Sequence[FinancialPeriod],
             want: str) -> List[FinancialPeriod]:
    """One value per (start,end) for periods of kind `want`; latest filed
    wins. Sorted by period end."""
    best: Dict[Tuple, FinancialPeriod] = {}
    for p in periods:
        if kind(p) != want:
            continue
        k = (p.period_start, p.period_end)
        cur = best.get(k)
        if cur is None or (p.filed or "") > (cur.filed or ""):
            best[k] = p
    return sorted(best.values(), key=lambda p: p.period_end)


def ttm(periods: Sequence[FinancialPeriod]) -> Dict:
    """Four sequential reported quarters ending at the latest quarter."""
    qs = collapse(periods, "Q")
    if len(qs) < 4:
        return {"value": UNKNOWN, "reason": f"only {len(qs)} reported "
                "quarter(s); TTM needs four", "contributing": []}
    last4 = qs[-4:]
    for a, b in zip(last4, last4[1:]):
        gap = (_d(b.period_start) - _d(a.period_end)).days
        if gap < 0:
            return {"value": UNKNOWN, "reason": "overlapping quarters",
                    "contributing": _contrib(last4)}
        if gap > 5:
            return {"value": UNKNOWN,
                    "reason": f"missing quarter before {b.period_end}",
                    "contributing": _contrib(last4)}
    return {"value": round(sum(p.value for p in last4), 6),
            "reason": "four sequential reported quarters",
            "contributing": _contrib(last4)}


def _contrib(ps):
    return [{"period_start": p.period_start, "period_end": p.period_end,
             "fiscal_year": p.fiscal_year, "fiscal_period": p.fiscal_period,
             "filed": p.filed, "accession": p.accession, "value": p.value}
            for p in ps]


def latest(periods: Sequence[FinancialPeriod],
           want: str) -> Optional[FinancialPeriod]:
    c = collapse(periods, want)
    return c[-1] if c else None


def freshness(period_end: Optional[str], today: date) -> str:
    """Based on the period the numbers describe. A statement retrieved
    today for a period three years ago is STALE."""
    if not period_end:
        return "UNKNOWN"
    age = (today - _d(period_end)).days
    if age < 0:
        return "UNKNOWN"
    if age <= 135:
        return "CURRENT"
    if age <= 270:
        return "AGING"
    return "STALE"


def aligned(a: FinancialPeriod, b: FinancialPeriod) -> bool:
    return abs((_d(a.period_end) - _d(b.period_end)).days) <= ALIGN_DAYS \
        and kind(a) == kind(b)


def compare(a: FinancialPeriod, b: FinancialPeriod) -> Dict:
    if not aligned(a, b):
        return {"status": PERIOD_MISMATCH,
                "a_period_end": a.period_end, "b_period_end": b.period_end,
                "a_label": f"{a.fiscal_year}{a.fiscal_period}",
                "b_label": f"{b.fiscal_year}{b.fiscal_period}"}
    return {"status": "ALIGNED"}


def _same_period(a: Optional[FinancialPeriod],
                 b: Optional[FinancialPeriod]) -> bool:
    return bool(a and b and a.period_end == b.period_end)


def ratio(num: Optional[FinancialPeriod], den: Optional[FinancialPeriod],
          name: str) -> Dict:
    if num is None or den is None:
        return {"metric": name, "value": UNKNOWN, "reason": "missing input"}
    if not _same_period(num, den):
        return {"metric": name, "value": UNKNOWN,
                "reason": "inputs are from different periods"}
    if den.value == 0:
        return {"metric": name, "value": UNKNOWN, "reason": "zero denominator"}
    return {"metric": name, "value": round(num.value / den.value, 6),
            "period_end": num.period_end,
            "inputs": [num.concept, den.concept]}


def yoy_growth(periods: Sequence[FinancialPeriod], want: str,
               name: str) -> Dict:
    c = collapse(periods, want)
    if len(c) < 2:
        return {"metric": name, "value": UNKNOWN, "reason": "no prior period"}
    cur = c[-1]
    target = _d(cur.period_end) - timedelta(days=365)
    prior = [p for p in c[:-1]
             if abs((_d(p.period_end) - target).days) <= ALIGN_DAYS]
    if not prior:
        return {"metric": name, "value": UNKNOWN,
                "reason": "no period one year earlier"}
    p0 = prior[-1]
    if p0.value <= 0:
        return {"metric": name, "value": UNKNOWN,
                "reason": "prior value not positive; growth undefined"}
    return {"metric": name, "value": round(cur.value / p0.value - 1, 6),
            "current_period_end": cur.period_end,
            "prior_period_end": p0.period_end}


def instant_at(periods: Sequence[FinancialPeriod],
               end: str) -> Optional[FinancialPeriod]:
    """The balance-sheet value dated exactly `end` (latest filed wins)."""
    c = [p for p in collapse_instants(periods) if p.period_end == end]
    return c[-1] if c else None


def collapse_instants(periods: Sequence[FinancialPeriod]):
    return collapse(periods, "INSTANT")


def latest_common_instant(facts, a: str, b: str):
    """Most recent date at which BOTH balance-sheet concepts are reported,
    so a ratio never divides a May figure by an August one."""
    ea = {p.period_end for p in collapse_instants(facts.get(a, []))}
    eb = {p.period_end for p in collapse_instants(facts.get(b, []))}
    common = sorted(ea & eb)
    if not common:
        return None, None
    end = common[-1]
    return (instant_at(facts[a], end), instant_at(facts[b], end))


def summarise(facts: Dict[str, List[FinancialPeriod]], today: date) -> Dict:
    """Annual-basis fundamentals (latest fiscal year) plus the latest
    quarter, TTM, and freshness. Every number names its period."""
    def fy(c): return latest(facts.get(c, []), "FY")
    def inst(c): return latest(facts.get(c, []), "INSTANT")

    rev, gp, op, ni = fy("revenue"), fy("gross_profit"), \
        fy("operating_income"), fy("net_income")
    ocf, capex = fy("operating_cash_flow"), fy("capex")
    fcf = None
    if ocf and capex and _same_period(ocf, capex):
        fcf = {"value": round(ocf.value - abs(capex.value), 2),
               "period_end": ocf.period_end,
               "inputs": ["operating_cash_flow", "capex"]}
    cur = inst("current_assets")
    # Ratios pair inputs at one date. Debt/equity and the current ratio use
    # the latest date both sides are reported; ROA/ROE use the balance at
    # the fiscal year end the net income covers.
    debt_p, eq_p = latest_common_instant(facts, "total_debt", "equity")
    ca_p, cl_p = latest_common_instant(facts, "current_assets",
                                       "current_liabilities")
    ni_end = ni.period_end if ni else None
    ta_fy = instant_at(facts.get("total_assets", []), ni_end) if ni_end else None
    eq_fy = instant_at(facts.get("equity", []), ni_end) if ni_end else None
    out = {
        "basis": "latest fiscal year (reported)",
        "income": {k: _val(p) for k, p in
                   (("revenue", rev), ("gross_profit", gp),
                    ("operating_income", op), ("net_income", ni),
                    ("eps_diluted", fy("eps_diluted")))},
        "balance": {k: _val(inst(k)) for k in
                    ("cash", "total_debt", "current_assets",
                     "current_liabilities", "total_assets",
                     "total_liabilities", "equity")},
        "cash_flow": {"operating_cash_flow": _val(ocf), "capex": _val(capex),
                      "free_cash_flow": fcf or UNKNOWN},
        "derived": [
            yoy_growth(facts.get("revenue", []), "FY", "revenue_growth"),
            yoy_growth(facts.get("eps_diluted", []), "FY", "eps_growth"),
            ratio(gp, rev, "gross_margin"),
            ratio(op, rev, "operating_margin"),
            ratio(ni, rev, "net_margin"),
            ratio(debt_p, eq_p, "debt_to_equity"),
            ratio(ca_p, cl_p, "current_ratio"),
            ratio(ni, ta_fy, "return_on_assets"),
            ratio(ni, eq_fy, "return_on_equity"),
        ],
        "ttm_revenue": ttm(facts.get("revenue", [])),
        "ttm_net_income": ttm(facts.get("net_income", [])),
    }
    ends = [p.period_end for p in (rev, ni, cur) if p]
    newest = max(ends) if ends else None
    out["latest_period_end"] = newest
    out["freshness"] = freshness(newest, today)
    out["notes"] = ["ratios use period-end balances (not averages)"]
    return out


def _val(p: Optional[FinancialPeriod]):
    if p is None:
        return UNKNOWN
    return {"value": p.value, "unit": p.unit, "period_start": p.period_start,
            "period_end": p.period_end, "fiscal_year": p.fiscal_year,
            "fiscal_period": p.fiscal_period, "form": p.form,
            "filed": p.filed, "accession": p.accession}
