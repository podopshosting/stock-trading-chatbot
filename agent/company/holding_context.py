"""
HoldingContext: longer-horizon context for a company.

NOT an execution instruction, NOT a signal, and NOT a score. It returns
one label plus the named factors behind it, kept separate from the
intraday hypothesis. There is no weighted composite: the label follows
from stated rules over named factors, and every factor is shown.

Overnight holding stays DISABLED regardless of this output.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .models import DividendStatus, HoldingContextLabel as L

POSITIVE, NEGATIVE, CAUTION, UNKNOWN = "POSITIVE", "NEGATIVE", "CAUTION", "UNKNOWN"
OVERNIGHT_EXECUTION = "DISABLED"
MIN_KNOWN_FACTORS = 3


def _val(x):
    return x.get("value") if isinstance(x, dict) else None


def factors(dividend, fundamentals: Optional[Dict], earnings_trends: Optional[Dict],
            splits: Optional[Dict], days_to_earnings: Optional[int],
            freshness: str) -> List[Dict]:
    out: List[Dict] = []

    def add(name, state, why, inputs=None):
        out.append({"factor": name, "state": state, "why": why,
                    "inputs": inputs or []})

    # dividend profile
    if dividend is None or dividend.status is DividendStatus.UNKNOWN:
        add("dividend_profile", UNKNOWN, "dividend status not established")
    elif dividend.status is DividendStatus.ACTIVE:
        add("dividend_profile", POSITIVE, "regular dividend cadence",
            ["dividend_history"])
    elif dividend.status in (DividendStatus.SUSPENDED,
                             DividendStatus.DISCONTINUED):
        add("dividend_profile", NEGATIVE, f"dividend {dividend.status}",
            ["dividend_history"])
    elif dividend.status is DividendStatus.IRREGULAR:
        add("dividend_profile", CAUTION, "irregular dividend history",
            ["dividend_history"])
    else:
        add("dividend_profile", UNKNOWN, "no dividend (neutral for holding)")

    d = {x["metric"]: x for x in (fundamentals or {}).get("derived", [])}
    # growth
    g = _val(d.get("revenue_growth"))
    if g is None or g == "UNKNOWN":
        add("fundamental_trend", UNKNOWN, "revenue growth unavailable")
    elif g > 0.02:
        add("fundamental_trend", POSITIVE, f"revenue growth {g:.1%}",
            ["revenue_growth"])
    elif g < -0.02:
        add("fundamental_trend", NEGATIVE, f"revenue decline {g:.1%}",
            ["revenue_growth"])
    else:
        add("fundamental_trend", UNKNOWN, "revenue roughly flat")
    # balance sheet
    dte, cr = _val(d.get("debt_to_equity")), _val(d.get("current_ratio"))
    if dte in (None, "UNKNOWN") and cr in (None, "UNKNOWN"):
        add("balance_sheet_risk", UNKNOWN, "leverage and liquidity unavailable")
    else:
        bad = (isinstance(dte, (int, float)) and dte > 3.0) or \
              (isinstance(cr, (int, float)) and cr < 0.8)
        add("balance_sheet_risk", CAUTION if bad else POSITIVE,
            f"debt/equity {dte}, current ratio {cr}",
            ["debt_to_equity", "current_ratio"])
    # cash flow quality
    ni = ((fundamentals or {}).get("income") or {}).get("net_income")
    fcf = ((fundamentals or {}).get("cash_flow") or {}).get("free_cash_flow")
    fv, nv = _val(fcf), _val(ni)
    if not isinstance(fv, (int, float)) or not isinstance(nv, (int, float)):
        add("cash_flow_quality", UNKNOWN, "free cash flow or net income unavailable")
    elif fv < 0:
        add("cash_flow_quality", NEGATIVE, "negative free cash flow",
            ["operating_cash_flow", "capex"])
    elif nv > 0 and fv < 0.5 * nv:
        add("cash_flow_quality", CAUTION,
            "free cash flow under half of net income", ["free_cash_flow", "net_income"])
    else:
        add("cash_flow_quality", POSITIVE, "free cash flow supports earnings",
            ["free_cash_flow", "net_income"])
    # earnings
    ey = ((earnings_trends or {}).get("eps_yoy") or {}).get("value")
    if ey in (None, "UNKNOWN"):
        add("earnings_trend", UNKNOWN, "EPS year-over-year unavailable")
    elif ey > 0.03:
        add("earnings_trend", POSITIVE, f"EPS up {ey:.1%} year over year")
    elif ey < -0.03:
        add("earnings_trend", NEGATIVE, f"EPS down {-ey:.1%} year over year")
    else:
        add("earnings_trend", UNKNOWN, "EPS roughly flat")
    # event risks
    if days_to_earnings is not None and 0 <= days_to_earnings <= 7:
        add("upcoming_earnings_risk", CAUTION,
            f"earnings in {days_to_earnings} day(s)")
    if dividend is not None and dividend.days_until_ex is not None \
            and 0 <= dividend.days_until_ex <= 5:
        add("ex_dividend_adjustment", CAUTION,
            f"ex-dividend in {dividend.days_until_ex} day(s); the price "
            "adjusts down by about the dividend and it is not free return")
    if splits and splits.get("recent"):
        add("corporate_action_risk", CAUTION,
            f"split within the last year ({splits['last']['type']})")
    out.append({"factor": "data_freshness",
                "state": POSITIVE if freshness == "CURRENT" else
                (CAUTION if freshness in ("AGING", "STALE") else UNKNOWN),
                "why": f"fundamentals freshness {freshness}", "inputs": []})
    return out


def derive(factor_list: List[Dict], freshness: str) -> Dict:
    known = [f for f in factor_list
             if f["state"] != UNKNOWN and f["factor"] != "data_freshness"]
    count = {s: sum(f["state"] == s for f in known)
             for s in (POSITIVE, NEGATIVE, CAUTION)}
    if len(known) < MIN_KNOWN_FACTORS:
        label = L.INSUFFICIENT_DATA
        why = f"only {len(known)} factor(s) known; need {MIN_KNOWN_FACTORS}"
    elif freshness == "STALE":
        label = L.INSUFFICIENT_DATA
        why = "fundamentals are STALE; context would describe a past company"
    elif count[NEGATIVE] >= 2:
        label, why = L.UNFAVORABLE, "two or more negative factors"
    elif count[NEGATIVE] == 1 or count[CAUTION] >= 2:
        label, why = L.CAUTIOUS, "a negative factor or several cautions"
    elif count[POSITIVE] >= 3 and count[CAUTION] == 0:
        label, why = L.FAVORABLE, "three or more positive factors, no cautions"
    else:
        label, why = L.NEUTRAL, "mixed or limited evidence"
    return {"label": str(label), "why": why, "counts": count,
            "known_factors": len(known)}


def holding_context(dividend, fundamentals, earnings_trends, splits,
                    days_to_earnings, freshness) -> Dict:
    fl = factors(dividend, fundamentals, earnings_trends, splits,
                 days_to_earnings, freshness)
    res = derive(fl, freshness)
    res.update({
        "factors": fl,
        "is_execution_instruction": False,
        "overnight_execution": OVERNIGHT_EXECUTION,
        "composite_score": None,
        "note": ("Longer-horizon context only. It does not change the "
                 "intraday decision, the Risk Governor, or the policy of "
                 "flattening before the close."),
    })
    return res
