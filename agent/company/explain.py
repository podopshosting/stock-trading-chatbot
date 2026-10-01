"""
Company questions, answered from stored company intelligence.

Deterministic. No language model is consulted, and none could be: the
answers are assembled from the structured records the providers produced,
and every answer names which record it came from. Where the record is
silent the answer says so.

That rule exists because a language model will cheerfully state a
dividend yield from memory, and a number remembered from training is
indistinguishable - to the reader - from one measured this morning. The
whole point of the company layer is that the difference is visible.

Each answer also carries the provenance the record carries: the window a
dividend history covers, the fiscal period a margin belongs to, the price
and date a yield was computed from.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

INTENTS = (
    "DIVIDEND", "LAST_DIVIDEND", "DIVIDEND_CUT", "DIVIDEND_GROWTH",
    "NEXT_EX_DATE",
    "SPLITS", "COMPETITORS", "COMPARE_WITH", "REVENUE_GROWTH",
    "MARGINS", "DEBT", "LAST_EARNINGS", "NEXT_EARNINGS",
    "HOLDING_DIFFERENCE", "CORPORATE_ACTIONS",
)

_SYMBOL = re.compile(r"\b([A-Z]{1,5})\b")
_STOP = {"I", "A", "THE", "WHY", "DID", "YOU", "HOW", "ARE", "IS", "WHAT",
         "DO", "DOES", "AND", "OR", "TO", "IN", "ON", "OF", "IT", "MY",
         "ME", "WE", "NOT", "NO", "HAS", "HAVE", "HAD", "WAS", "WERE",
         "PAY", "PAYS", "CUT", "ITS", "WHO", "WHEN", "WOULD", "THIS",
         "MUCH", "LAST", "NEXT", "BEFORE", "EVER", "ANY", "AT", "BY",
         "AN", "AS", "SO", "UP", "IF", "FOR", "BE", "LOOK", "SAME"}


def symbols(query: str) -> List[str]:
    """Tickers named in the question, in order, de-duplicated.

    Uppercase only, so ordinary words are not read as companies. Two
    symbols is how "how does GIS compare with CPB" is recognised.
    """
    out: List[str] = []
    for match in _SYMBOL.findall(query or ""):
        if match not in _STOP and match not in out:
            out.append(match)
    return out


def classify(query: str) -> Optional[str]:
    q = (query or "").lower()
    if not q.strip():
        return None
    if re.search(r"(would|does|is).*(differ|different|change).*(hold|holding|"
                 r"overnight|longer)|hold(ing)? it|if we held", q):
        return "HOLDING_DIFFERENCE"
    if re.search(r"(dividend|payout|distribution).*(grow|growth|increas|"
                 r"rais|hike|cagr)|"
                 r"(grow|growth|increas|rais|hike).*(dividend|payout)", q):
        return "DIVIDEND_GROWTH"
    if re.search(r"(cut|reduc|suspend|stopp?ed|slash).*(dividend)|"
                 r"dividend.*(cut|reduc|suspend|stopp?ed|safe)", q):
        return "DIVIDEND_CUT"
    if re.search(r"(next|upcoming|when).*(ex.?dividend|ex.?date)|ex.?date", q):
        return "NEXT_EX_DATE"
    if re.search(r"(last|latest|most recent|when).*(dividend|payout|"
                 r"distribution)", q):
        return "LAST_DIVIDEND"
    if re.search(r"dividend|yield|payout", q):
        return "DIVIDEND"
    if re.search(r"split", q):
        return "SPLITS"
    if re.search(r"(compare|versus|vs\.?|against|how does).*(with|to|vs)", q) \
            and len(symbols(query)) >= 2:
        return "COMPARE_WITH"
    if re.search(r"competitor|peer|rival|similar compan|who else", q):
        return "COMPETITORS"
    if re.search(r"revenue.*(grow|growth|rising|increas|declin|shrink)|"
                 r"(grow|growth).*revenue|sales grow", q):
        return "REVENUE_GROWTH"
    if re.search(r"margin|profitab", q):
        return "MARGINS"
    if re.search(r"debt|leverage|borrow|balance sheet", q):
        return "DEBT"
    if re.search(r"(next|upcoming|when is).*(earnings|report)", q):
        return "NEXT_EARNINGS"
    if re.search(r"earnings|eps|beat|miss|quarter", q):
        return "LAST_EARNINGS"
    if re.search(r"corporate action|merger|spin.?off|name change|"
                 r"symbol change", q):
        return "CORPORATE_ACTIONS"
    return None


def _answer(text: str, sources: List[str], grounded: bool = True,
            intent: Optional[str] = None, **extra) -> Dict:
    out = {"answer": text, "sources": sources, "grounded": grounded,
           "intent": intent, "llm_used": False}
    out.update(extra)
    return out


def _silent(intent: str, what: str, sources: Optional[List[str]] = None) -> Dict:
    return _answer(f"The record does not contain {what}. I will not answer "
                   f"that from memory.", sources or [], grounded=False,
                   intent=intent)


def _pct(v) -> str:
    return "unknown" if not isinstance(v, (int, float)) else f"{v:.2%}"


def _num(v) -> str:
    if not isinstance(v, (int, float)):
        return "unknown"
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= size:
            return f"${v / size:,.2f}{unit}"
    return f"${v:,.0f}"


def _metric(summary: Dict, name: str):
    for row in (summary or {}).get("derived", []):
        if row.get("metric") == name:
            return row
    return None


# --- intent handlers -------------------------------------------------------

def _dividend(service, symbol, _others, intent):
    d = service.dividends(symbol)
    if d.get("status") == "UNKNOWN":
        return _answer(
            f"{symbol}: I cannot establish a dividend status. "
            f"{d.get('reason') or ''}".strip(), ["dividends"],
            grounded=False, intent=intent)
    label = d["dividend_stock"]
    bits = [f"{symbol} - Dividend stock: {label} (internal status "
            f"{d['status']}: {d['reason']})."]
    if d.get("trailing_12m_amount") is not None:
        bits.append(f"Trailing 12 months {d['trailing_12m_amount']:.4g} "
                    f"{(d.get('currency') or 'USD')}.")
    if d.get("trailing_yield_pct") is not None:
        bits.append(f"Yield {d['trailing_yield_pct']:.2f}% computed from "
                    f"{d.get('yield_price_basis')}.")
    if d.get("frequency_days"):
        bits.append(f"Typical interval {d['frequency_days']} days.")
    if d.get("years_paid"):
        bits.append(f"Payments seen in {d['years_paid']} calendar years "
                    f"within the {d.get('window_years')}-year window "
                    f"retrieved.")
    if d.get("special_dividends"):
        bits.append(f"Special distributions (not counted as regular): "
                    f"{', '.join(d['special_dividends'][:4])}.")
    return _answer(" ".join(bits), ["dividends", "corporate_actions"],
                   intent=intent, dividend_stock=label)


def _last_dividend(service, symbol, _others, intent):
    d = service.dividends(symbol)
    history = d.get("history") or []
    if not history:
        return _silent(intent, f"any dividend for {symbol}", ["dividends"])
    latest = history[0]
    extra = ""
    if latest.get("pay_date"):
        extra = f", paid {latest['pay_date']}"
    kind = "special" if latest["kind"] == "SPECIAL" else "regular"
    return _answer(
        f"{symbol}'s most recent recorded dividend went ex on "
        f"{latest['ex_date']}: {latest['amount']:.4g} ({kind})"
        f"{extra}.", ["dividends"], intent=intent)


def _dividend_cut(service, symbol, _others, intent):
    d = service.dividends(symbol)
    status = d.get("status")
    history = [h for h in (d.get("history") or []) if h["kind"] == "REGULAR"]
    if status == "UNKNOWN" or not history:
        return _silent(intent, f"enough dividend history for {symbol}",
                       ["dividends"])
    amounts = [(h["ex_date"], h["amount"]) for h in reversed(history)]
    cuts = [(d2, a1, a2) for (d1, a1), (d2, a2) in zip(amounts, amounts[1:])
            if a2 < a1 * 0.999]
    if status in ("SUSPENDED", "DISCONTINUED"):
        head = (f"{symbol}'s regular dividend appears {status}: "
                f"{d['reason']}.")
    elif cuts:
        head = (f"{symbol}'s per-share dividend has decreased "
                f"{len(cuts)} time(s) in the recorded window, most "
                f"recently on {cuts[-1][0]} "
                f"({cuts[-1][1]:.4g} to {cuts[-1][2]:.4g}).")
    else:
        head = (f"No decrease in {symbol}'s regular per-share dividend "
                f"appears in the recorded window.")
    return _answer(
        head + f" This covers the {d.get('window_years')}-year window "
        f"retrieved from the provider, and a per-share amount can also "
        f"change because of a split.", ["dividends", "splits"],
        intent=intent)


def _dividend_growth(service, symbol, _others, intent):
    d = service.dividends(symbol)
    g = d.get("growth") or {}
    spans = g.get("spans") or {}
    known = [(k, spans[k]) for k in ("1y", "3y", "5y")
             if spans.get(k, {}).get("growth_pct") is not None]
    if not known:
        reasons = sorted({sp.get("reason") for sp in spans.values()
                          if sp.get("reason")})
        return _answer(
            f"No dividend growth figure can be given for {symbol}: "
            + (reasons[0] if reasons else "no regular dividends were found")
            + ". An unreachable span is left unknown rather than reported as "
              "zero, because a company with four years of payments has not "
              "held its dividend flat for five.",
            ["dividends"], grounded=True, intent=intent)

    parts = []
    for key, sp in known:
        label = f"{key} {'a year ' if sp.get('annualised') else ''}"
        parts.append(f"{label}{sp['growth_pct']:+.1f}%")
    body = (f"{symbol}'s regular dividend has grown "
            + ", ".join(parts) + ". ")
    # Said rather than assumed: an unadjusted series turns a 4-for-1
    # split into a 75% cut, so the reader needs to know which they have.
    if g.get("split_adjusted"):
        body += ("Amounts before any split are restated into current share "
                 "terms, so a split does not appear as a cut. ")
    unknown = [k for k in ("1y", "3y", "5y")
               if spans.get(k, {}).get("growth_pct") is None]
    if unknown:
        first = spans[unknown[0]].get("reason") or "the history is too short"
        body += (f"The {', '.join(unknown)} span(s) are unknown: {first}. ")
    body += ("Special dividends are excluded throughout - a one-off "
             "distribution is not a change in the regular rate.")
    return _answer(body, ["dividends"], intent=intent)


def _next_ex_date(service, symbol, _others, intent):
    d = service.dividends(symbol)
    if d.get("next_ex_date"):
        return _answer(
            f"{symbol}'s next recorded ex-dividend date is "
            f"{d['next_ex_date']}, in {d['days_until_ex']} day(s). The "
            f"price adjusts down by about the dividend on that date, so it "
            f"is not free return to a holder.", ["dividends"], intent=intent)
    return _answer(
        f"No future ex-dividend date has been declared for {symbol} in the "
        f"window retrieved (which looks ahead as well as back). Companies "
        f"usually declare a quarter at a time, so this means 'not yet "
        f"announced', not 'none coming'.", ["dividends"], grounded=True,
        intent=intent, next_ex_date=None)


def _splits(service, symbol, _others, intent):
    s = service.splits(symbol)
    if not s.get("count"):
        return _answer(
            f"No stock split for {symbol} appears in the retrieved window.",
            ["splits"], intent=intent)
    lines = "; ".join(f"{t['ex_date']}: {t['label']}"
                      for t in s["timeline"][:6])
    return _answer(
        f"{symbol} has {s['count']} recorded split(s). {lines}."
        + (" A split within the last year can make an unadjusted price "
           "history look like a crash or a surge." if s.get("recent") else ""),
        ["splits"], intent=intent)


def _competitors(service, symbol, _others, intent):
    ps = service.peers(symbol)
    if not ps.get("peers"):
        reasons = {r["symbol"]: r["reasons"] for r in ps.get("rejected", [])}
        return _answer(
            f"No structured peer was found for {symbol}. Candidates were "
            f"rejected for: "
            f"{sorted({r for v in reasons.values() for r in v})}. Peers come "
            f"from industry classification, never from a model's "
            f"suggestion.", ["peers"], grounded=False, intent=intent)
    listed = ", ".join(f"{p['symbol']} ({'/'.join(p['reasons'])})"
                       for p in ps["peers"])
    note = ("" if ps.get("complete") else
            f" {len(ps.get('candidates_not_yet_profiled') or [])} universe "
            f"members are not yet classified, so this set may grow.")
    return _answer(
        f"{symbol}'s structured peers: {listed}. Each needs at least one "
        f"industry reason - a shared sector or a similar market "
        f"capitalisation alone is not enough.{note}",
        ["peers"], intent=intent)


def _compare_with(service, symbol, others, intent):
    from .compare import compare_metric
    other = others[0]
    a, b = service.fundamentals(symbol), service.fundamentals(other)
    rows, mismatched = [], []
    for name in ("revenue_growth", "operating_margin", "net_margin",
                 "debt_to_equity", "current_ratio"):
        ma, mb = _metric(a, name), _metric(b, name)
        if not ma or not mb:
            continue
        got = compare_metric(
            name, {"value": ma.get("value"),
                   "period_end": ma.get("period_end")
                   or ma.get("current_period_end")},
            {other: {"value": mb.get("value"),
                     "period_end": mb.get("period_end")
                     or mb.get("current_period_end")}})
        if got["status"] == "OK":
            rows.append(f"{name}: {symbol} {_pct(got['subject']['value'])} "
                        f"vs {other} {_pct(got['peers'][other])}")
        else:
            mismatched.append(name)
    if not rows:
        return _answer(
            f"I cannot compare {symbol} with {other} on aligned periods. "
            f"{symbol}'s latest reported period ends "
            f"{a.get('latest_period_end')} and {other}'s ends "
            f"{b.get('latest_period_end')}; comparing different fiscal "
            f"periods would produce a number describing neither.",
            ["fundamentals"], grounded=False, intent=intent)
    tail = (f" Not comparable on aligned periods: {', '.join(mismatched)}."
            if mismatched else "")
    return _answer(
        f"{symbol} against {other}, like-for-like periods only. "
        + "; ".join(rows) + "." + tail
        + " These are relative facts, not a ranking.",
        ["fundamentals"], intent=intent)


def _revenue_growth(service, symbol, _others, intent):
    f = service.fundamentals(symbol)
    row = _metric(f, "revenue_growth")
    if not row or not isinstance(row.get("value"), (int, float)):
        return _answer(
            f"{symbol}: revenue growth is UNKNOWN"
            + (f" ({row.get('reason')})" if row else "")
            + f". Fundamentals freshness: {f.get('freshness')}.",
            ["fundamentals"], grounded=False, intent=intent)
    ttm = f.get("ttm_revenue") or {}
    extra = (f" Trailing-twelve-month revenue {_num(ttm.get('value'))} from "
             f"{len(ttm.get('contributing') or [])} reported quarters."
             if isinstance(ttm.get("value"), (int, float)) else
             f" TTM revenue unavailable: {ttm.get('reason')}.")
    return _answer(
        f"{symbol} revenue growth {_pct(row['value'])} "
        f"({row.get('prior_period_end')} to "
        f"{row.get('current_period_end')}, annual basis, as reported to "
        f"SEC).{extra} Freshness: {f.get('freshness')}.",
        ["fundamentals"], intent=intent)


def _margins(service, symbol, _others, intent):
    f = service.fundamentals(symbol)
    parts = []
    for name in ("gross_margin", "operating_margin", "net_margin"):
        row = _metric(f, name)
        value = row.get("value") if row else None
        parts.append(f"{name.replace('_', ' ')} "
                     + (_pct(value) if isinstance(value, (int, float))
                        else f"UNKNOWN ({(row or {}).get('reason', 'not reported')})"))
    return _answer(
        f"{symbol}, period ending {f.get('latest_period_end')}: "
        + "; ".join(parts)
        + f". Freshness {f.get('freshness')}. A margin is UNKNOWN when the "
        f"company does not report that line, or when the two sides come "
        f"from different periods.", ["fundamentals"], intent=intent)


def _debt(service, symbol, _others, intent):
    f = service.fundamentals(symbol)
    balance = f.get("balance") or {}
    debt, equity = balance.get("total_debt"), balance.get("equity")
    dte = _metric(f, "debt_to_equity")
    cr = _metric(f, "current_ratio")
    if not isinstance(debt, dict):
        return _answer(
            f"{symbol} does not report a combined debt figure in the "
            f"concepts retrieved. Freshness {f.get('freshness')}.",
            ["fundamentals"], grounded=False, intent=intent)
    bits = [f"{symbol} total debt {_num(debt['value'])} as of "
            f"{debt['period_end']} ({debt.get('form')}, filed "
            f"{debt.get('filed')})."]
    if isinstance(equity, dict):
        bits.append(f"Shareholders' equity {_num(equity['value'])} as of "
                    f"{equity['period_end']}.")
    for row, label in ((dte, "Debt/equity"), (cr, "Current ratio")):
        if row and isinstance(row.get("value"), (int, float)):
            bits.append(f"{label} {row['value']:.2f} (both sides dated "
                        f"{row.get('period_end')}).")
        elif row:
            bits.append(f"{label} UNKNOWN: {row.get('reason')}.")
    return _answer(" ".join(bits), ["fundamentals"], intent=intent)


def _last_earnings(service, symbol, _others, intent):
    e = service.earnings(symbol)
    records = e.get("records") or []
    if not records:
        return _silent(intent, f"earnings records for {symbol}", ["earnings"])
    last = records[-1]
    trends = e.get("trends") or {}
    bits = [f"{symbol}'s most recent reported period ends "
            f"{last['period_end']}: diluted EPS "
            f"{last.get('eps_actual')} (as reported to SEC)."]
    if last.get("revenue_actual") is not None:
        bits.append(f"Revenue {_num(last['revenue_actual'])}.")
    if last.get("eps_estimate") is None:
        bits.append("No consensus estimate is available, so there is no "
                    "beat or miss: estimates are never inferred.")
    else:
        bits.append(f"Estimate {last['eps_estimate']}, surprise "
                    f"{last.get('eps_surprise')}.")
    if isinstance((trends.get("eps_yoy") or {}).get("value"), (int, float)):
        y = trends["eps_yoy"]
        bits.append(f"EPS year-over-year {_pct(y['value'])} "
                    f"({y.get('prior')} to {y.get('current')}).")
    return _answer(" ".join(bits), ["earnings"], intent=intent)


def _next_earnings(service, symbol, _others, intent):
    e = service.earnings(symbol)
    return _answer(
        f"I do not know when {symbol} next reports: "
        f"{e.get('next_earnings_note')}. SEC filings establish what was "
        f"reported, not what is scheduled.",
        ["earnings"], grounded=False, intent=intent)


def _corporate_actions(service, symbol, _others, intent):
    ca = service.corporate_actions(symbol)
    actions = ca.get("actions") or []
    if not actions:
        return _answer(
            f"No corporate action for {symbol} appears in the retrieved "
            f"window." + (f" Provider error: {ca['error']}"
                          if ca.get("error") else ""),
            ["corporate_actions"], grounded=not ca.get("error"),
            intent=intent)
    counts: Dict[str, int] = {}
    for a in actions:
        counts[a["type"]] = counts.get(a["type"], 0) + 1
    recent = sorted((a for a in actions if a.get("ex_date")),
                    key=lambda a: a["ex_date"], reverse=True)[:4]
    listed = "; ".join(f"{a['ex_date']} {a['type']}" for a in recent)
    return _answer(
        f"{symbol} has {len(actions)} recorded corporate actions over "
        f"{ca.get('window_years')} years: "
        + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items()))
        + f". Most recent: {listed}.",
        ["corporate_actions"], intent=intent)


def _holding_difference(service, symbol, _others, intent):
    h = service.holding_context(symbol)
    factors = [f for f in h.get("factors", []) if f["state"] != "UNKNOWN"]
    named = "; ".join(f"{f['factor']} {f['state']} ({f['why']})"
                      for f in factors[:5]) or "no factor is established"
    return _answer(
        f"For {symbol} the longer-horizon context is {h['label']} "
        f"({h['why']}). Factors: {named}. This is research context and NOT "
        f"an instruction: it does not change the intraday decision, the "
        f"Risk Governor, or the policy of flattening before the close. "
        f"Overnight positions remain {h['overnight_execution']}, and there "
        f"is no composite score behind this label.",
        ["holding_context", "fundamentals", "dividends", "earnings"],
        intent=intent, label=h["label"])


_HANDLERS: Dict[str, Callable] = {
    "DIVIDEND": _dividend, "LAST_DIVIDEND": _last_dividend,
    "DIVIDEND_CUT": _dividend_cut, "DIVIDEND_GROWTH": _dividend_growth,
    "NEXT_EX_DATE": _next_ex_date,
    "SPLITS": _splits, "COMPETITORS": _competitors,
    "COMPARE_WITH": _compare_with, "REVENUE_GROWTH": _revenue_growth,
    "MARGINS": _margins, "DEBT": _debt,
    "LAST_EARNINGS": _last_earnings, "NEXT_EARNINGS": _next_earnings,
    "CORPORATE_ACTIONS": _corporate_actions,
    "HOLDING_DIFFERENCE": _holding_difference,
}


def explain(query: str, service, default_symbol: Optional[str] = None) -> Dict:
    """Answer a company question from stored intelligence, or decline.

    `service` is a CompanyService. A question naming no company cannot be
    answered, because the records are per company - saying which one is
    missing is more useful than guessing a symbol.
    """
    intent = classify(query)
    if intent is None:
        return _answer(
            "I can answer company questions about dividends, ex-dividend "
            "dates, dividend cuts, splits, corporate actions, earnings, "
            "revenue growth, margins, debt, competitors, a comparison with "
            "a named company, and how the longer-horizon context looks.",
            [], grounded=False, intent=None)
    found = symbols(query)
    symbol = found[0] if found else default_symbol
    if not symbol:
        return _answer(
            "Which company? Company records are per symbol, so I need a "
            "ticker before I can answer from them.", [], grounded=False,
            intent=intent)
    others = [s for s in found if s != symbol]
    if intent == "COMPARE_WITH" and not others:
        return _answer(f"Compare {symbol} with which company?", [],
                       grounded=False, intent=intent)
    try:
        out = _HANDLERS[intent](service, symbol, others, intent)
    except Exception as exc:                              # noqa: BLE001
        return _answer(
            f"I could not read the records for {symbol}: "
            f"{type(exc).__name__}. I will not answer from memory instead.",
            [], grounded=False, intent=intent)
    out["symbol"] = symbol
    return out
