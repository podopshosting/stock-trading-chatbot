"""
Grounded explanations of what the agent did and why.

DETERMINISTIC. Every answer is assembled from a stored row - a decision,
a journalled trade, a health snapshot, a session tally - with no
language model in the loop. That is the strongest available form of
"the LLM explains stored decisions; it does not rewrite them": a model
cannot alter a decision it never touches. A model may later be asked to
rephrase these answers, but the text here is the authority, and each
answer names the stored sources it was built from.

An answer for something the record does not contain says so. It does not
fall back to a plausible-sounding guess: "I have no decision recorded for
AAPL today" is a fact, and a fabricated reason is not.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

INTENTS = (
    "NOW", "WHY_NO_TRADE", "WHY_REJECTED", "WHY_OPENED", "WHY_EXITED",
    "RISK_REMAINING", "HEALTH", "RECONCILIATION", "HOW_TODAY",
)

_SYMBOL = re.compile(r"\b([A-Z]{1,5})\b")
_STOP = {"I", "A", "THE", "WHY", "DID", "YOU", "HOW", "ARE", "IS", "WHAT",
         "DO", "AND", "OR", "TO", "IN", "ON", "OF", "IT", "MY", "ME", "WE",
         "NOT", "NO", "HAS", "HAVE", "HAD", "WAS", "WERE", "TODAY", "NOW",
         "PASS", "GO", "OK", "AT", "BY", "AN", "AS", "SO", "UP", "IF"}


def extract_symbol(query: str) -> Optional[str]:
    """A ticker the user named. Uppercase only, so ordinary words are not
    mistaken for symbols ('how did today go' names no company)."""
    for match in _SYMBOL.findall(query or ""):
        if match not in _STOP:
            return match
    return None


def classify(query: str) -> Optional[str]:
    q = (query or "").lower()
    if not q.strip():
        return None
    if re.search(r"(why|how come).*(haven'?t|have not|not|no|didn'?t|"
                 r"did not).*(trade|trad|buy|enter|position)", q) \
            or "no trades" in q or "zero trades" in q:
        return "WHY_NO_TRADE"
    if re.search(r"why.*(reject|refus|pass|skip|decline|not buy|avoid)", q):
        return "WHY_REJECTED"
    if re.search(r"why.*(open|enter|buy|bought|take|took|long)", q):
        return "WHY_OPENED"
    if re.search(r"why.*(exit|clos|sell|sold|stop|flatten|got out)", q):
        return "WHY_EXITED"
    if re.search(r"(how much|what).*(risk|capital|budget|room|left|remain)",
                 q) or "risk remain" in q:
        return "RISK_REMAINING"
    if re.search(r"reconcil|broker.*(match|agree|pass)|positions? match", q):
        return "RECONCILIATION"
    if re.search(r"(are you|is the agent|you).*(healthy|ok|okay|well|"
                 r"working|running)|health|status of the agent", q):
        return "HEALTH"
    if re.search(r"how (did|was|is).*(today|session|day|go)|"
                 r"(today|session).*(summary|recap|result)", q):
        return "HOW_TODAY"
    if re.search(r"(what are you|what is it|what're you).*(doing|up to)|"
                 r"doing right now|current(ly)? doing", q):
        return "NOW"
    return None


def _money(v) -> str:
    return "unknown" if v is None else f"${v:,.2f}"


def _answer(text: str, sources: List[str], grounded: bool = True,
            intent: Optional[str] = None, **extra) -> Dict:
    out = {"answer": text, "sources": sources, "grounded": grounded,
           "intent": intent, "llm_used": False}
    out.update(extra)
    return out


def _no_record(intent, what, sources=None):
    return _answer(f"I have no record of {what}. I will not guess.",
                   sources or [], grounded=False, intent=intent)


def explain(query: str, ctx: Dict) -> Dict:
    """Answer from stored context, or say the record is silent."""
    intent = classify(query)
    if intent is None:
        return _answer(
            "I can answer: what I'm doing now, why I haven't traded, why I "
            "rejected / opened / exited a symbol, how much risk remains, "
            "whether I'm healthy, whether the broker reconciled, and how "
            "today went.", [], grounded=False, intent=None)
    symbol = extract_symbol(query)
    handler = {
        "NOW": _now, "WHY_NO_TRADE": _why_no_trade,
        "WHY_REJECTED": _why_rejected, "WHY_OPENED": _why_opened,
        "WHY_EXITED": _why_exited, "RISK_REMAINING": _risk_remaining,
        "HEALTH": _health, "RECONCILIATION": _reconciliation,
        "HOW_TODAY": _how_today}[intent]
    return handler(ctx, symbol)


# --- intents ---------------------------------------------------------------

def _now(ctx, _symbol):
    last = ctx.get("last_cycle")
    if not last:
        return _no_record("NOW", "any cycle having run yet",
                          ["last_cycle"])
    state = ctx.get("agent_state") or "UNKNOWN"
    lines = [f"Agent state: {state}. Mode: {ctx.get('mode', 'UNKNOWN')} "
             "(real money disabled)."]
    lines.append(f"Last cycle: {last.get('phase')} at "
                 f"{last.get('finished_at')}, outcome {last.get('outcome')}.")
    n = last.get("open_positions") or 0
    lines.append(f"Open positions: {n}."
                 + (" " + _position_lines(ctx) if n else ""))
    lines.append(f"Scanned {last.get('symbols_scanned', 0)} candidate(s), "
                 f"formed {last.get('hypotheses_generated', 0)} "
                 f"hypothesis(es), {last.get('decisions_approved', 0)} "
                 f"approved by the Risk Governor, "
                 f"{last.get('entries_submitted', 0)} order(s) placed.")
    halts = last.get("halt_reasons") or []
    if halts:
        lines.append("New entries are blocked: " + ", ".join(halts) + ".")
    h = (ctx.get("health") or {}).get("state")
    if h:
        lines.append(f"Health: {h}.")
    return _answer(" ".join(lines), ["last_cycle", "health", "positions"],
                   intent="NOW")


def _position_lines(ctx) -> str:
    rows = ctx.get("positions") or []
    return "; ".join(
        f"{p['symbol']} {p['quantity']:.4g} @ {p['entry_price']:.2f}, stop "
        f"{p['plan']['stop_price']:.2f}" for p in rows) + "."


def _why_no_trade(ctx, _symbol):
    rows = ctx.get("decisions") or []
    tally = ctx.get("tally") or {}
    if tally.get("entries"):
        return _answer(
            f"I have traded today: {tally['entries']} entr"
            f"{'y' if tally['entries'] == 1 else 'ies'}. Ask about a "
            "specific symbol for the reasoning.", ["tally"],
            intent="WHY_NO_TRADE")
    if not rows:
        return _no_record(
            "WHY_NO_TRADE",
            "any candidate being evaluated today - the scanner may have "
            "produced none, or the market may not have been open",
            ["decisions"])
    counts: Dict[str, int] = {}
    codes: Dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        for c in (r.get("risk") or {}).get("reason_codes", []):
            codes[c] = codes.get(c, 0) + 1
    parts = [f"I evaluated {len(rows)} candidate(s) today and entered none. "
             "Outcomes: " + ", ".join(f"{k} x{v}" for k, v in
                                      sorted(counts.items())) + "."]
    if codes:
        top = sorted(codes.items(), key=lambda kv: -kv[1])[:4]
        parts.append("Most common Risk Governor reasons: " +
                     ", ".join(f"{c} x{n}" for c, n in top) + ".")
    parts.append("A day with no trades is a valid outcome: available "
                 "capital is a ceiling, not a target.")
    return _answer(" ".join(parts), ["decisions"], intent="WHY_NO_TRADE")


def _decision_rows(ctx, symbol):
    return [r for r in (ctx.get("decisions") or [])
            if r.get("symbol") == symbol]


def _why_rejected(ctx, symbol):
    if not symbol:
        return _answer("Which symbol? Name it, e.g. 'why did you reject "
                       "AAPL'.", [], grounded=False, intent="WHY_REJECTED")
    rows = _decision_rows(ctx, symbol)
    if not rows:
        return _no_record("WHY_REJECTED",
                          f"{symbol} being evaluated today", ["decisions"])
    row = rows[-1]
    outcome = row["outcome"]
    if outcome == "ENTERED":
        return _answer(f"I did not reject {symbol}: I entered it. Ask why "
                       "I opened it.", ["decisions"],
                       intent="WHY_REJECTED")
    lines = [f"{symbol}: {outcome}."]
    risk = row.get("risk") or {}
    if risk.get("reason_codes"):
        lines.append("The Risk Governor refused it for every one of: " +
                     "; ".join(risk.get("reasons") or
                               risk["reason_codes"]) + ".")
    hyp = row.get("hypothesis") or {}
    if hyp:
        lines.append(f"Hypothesis: {hyp.get('strategy')}, strength "
                     f"{hyp.get('strength')}.")
        blocking = [c["code"] for c in hyp.get("contradictions", [])
                    if c.get("severity", "").endswith("BLOCKING")]
        if blocking:
            lines.append("Blocking contradictions: " +
                         ", ".join(blocking) + ".")
    if row.get("detail") and not risk:
        lines.append(row["detail"])
    return _answer(" ".join(lines), ["decisions"], intent="WHY_REJECTED",
                   decision_row_id=row.get("decision_row_id"))


def _why_opened(ctx, symbol):
    if not symbol:
        return _answer("Which symbol?", [], grounded=False,
                       intent="WHY_OPENED")
    rows = [r for r in _decision_rows(ctx, symbol)
            if r["outcome"] == "ENTERED"]
    if not rows:
        return _no_record("WHY_OPENED",
                          f"opening a position in {symbol} today",
                          ["decisions"])
    row = rows[-1]
    hyp, risk, order = (row.get("hypothesis") or {}, row.get("risk") or {},
                        row.get("order") or {})
    lines = [f"Opened {symbol}. Strategy {hyp.get('strategy')}, hypothesis "
             f"strength {hyp.get('strength')} (a component breakdown is on "
             "the stored hypothesis; it is not blended into one score)."]
    if risk:
        lines.append(f"The Risk Governor approved it: capital "
                     f"{_money(risk.get('capital_required'))}, maximum loss "
                     f"{_money(risk.get('max_loss'))}.")
    if order:
        lines.append(f"Order {order.get('status')}: "
                     f"{order.get('filled_quantity')} @ "
                     f"{order.get('average_fill_price')}.")
    v = row.get("versions") or {}
    if v:
        lines.append(f"Made under strategy {v.get('strategy')} / risk "
                     f"{v.get('risk')}, code {v.get('code_sha')}.")
    return _answer(" ".join(lines), ["decisions"], intent="WHY_OPENED",
                   decision_row_id=row.get("decision_row_id"))


def _why_exited(ctx, symbol):
    if not symbol:
        return _answer("Which symbol?", [], grounded=False,
                       intent="WHY_EXITED")
    trades = [t for t in (ctx.get("trades") or [])
              if t.get("symbol") == symbol]
    if not trades:
        return _no_record("WHY_EXITED",
                          f"an exit from {symbol} today", ["trades"])
    t = trades[-1]
    reasons = t.get("all_exit_reasons") or [t.get("exit_reason")]
    return _answer(
        f"Exited {symbol}: primary reason {t.get('exit_reason')}"
        + (f" (also: {', '.join(r for r in reasons if r != t.get('exit_reason'))})"
           if len(reasons) > 1 else "")
        + f". Entry {t.get('entry_price')}, exit {t.get('exit_price')}, "
          f"result {_money(t.get('net_pnl'))} "
          f"({t.get('r_multiple')}R), outcome {t.get('outcome')}. "
          "Every rule that fired is recorded, not just the winner.",
        ["trades"], intent="WHY_EXITED", trade_id=t.get("trade_id"))


def _risk_remaining(ctx, _symbol):
    limits, daily = ctx.get("limits"), ctx.get("daily")
    if not limits or daily is None:
        return _no_record("RISK_REMAINING", "today's limits or counters",
                          ["limits", "daily"])
    cap = limits["daily_capital_limit"] - daily["capital_deployed_today"]
    loss = limits["daily_loss_limit"] + min(0.0, daily["realized_pnl_today"])
    slots = limits["max_new_positions_per_day"] - \
        daily["positions_opened_today"]
    return _answer(
        f"Capital: {_money(max(0.0, cap))} of "
        f"{_money(limits['daily_capital_limit'])} remains (cumulative "
        f"gross, so a closed position's capital still counts). Loss budget: "
        f"{_money(max(0.0, loss))} of {_money(limits['daily_loss_limit'])}. "
        f"New positions: {max(0, slots)} of "
        f"{limits['max_new_positions_per_day']}. Realised today "
        f"{_money(daily['realized_pnl_today'])}. These are ceilings, not "
        "targets.", ["limits", "daily"], intent="RISK_REMAINING")


def _health(ctx, _symbol):
    h = ctx.get("health")
    if not h:
        return _no_record("HEALTH", "the health state", ["health"])
    lines = [f"Health: {h['state']}. New entries "
             f"{'permitted' if h['entries_permitted'] else 'blocked'}; "
             "exits are always permitted."]
    if h.get("blocking_reasons"):
        lines.append("Blocking: " + "; ".join(h["blocking_reasons"]) + ".")
    alerts = ctx.get("alerts") or []
    if alerts:
        lines.append("Alerts today: " + ", ".join(
            sorted({a["kind"] for a in alerts})) + ".")
    return _answer(" ".join(lines), ["health", "alerts"], intent="HEALTH")


def _reconciliation(ctx, _symbol):
    last, tally = ctx.get("last_cycle"), ctx.get("tally") or {}
    if not last:
        return _no_record("RECONCILIATION", "a cycle having run",
                          ["last_cycle"])
    step = next((s for s in last.get("steps", [])
                 if s["name"] == "reconcile"), None)
    if step is None:
        return _no_record("RECONCILIATION",
                          "reconciliation in the last cycle",
                          ["last_cycle"])
    text = (f"Last reconciliation: {'PASSED' if step['ok'] else 'FAILED'}"
            + (f" ({step['detail']})" if step.get("detail") else "")
            + f". Today: {tally.get('reconciliation_checks', 0)} check(s), "
              f"{tally.get('reconciliation_failures', 0)} failure(s), "
              f"{tally.get('emergency_stops', 0)} emergency stop(s).")
    return _answer(text, ["last_cycle", "tally"], intent="RECONCILIATION")


def _how_today(ctx, _symbol):
    tally, report = ctx.get("tally"), ctx.get("report")
    if not tally:
        return _no_record("HOW_TODAY", "a session having run today",
                          ["tally"])
    lines = [f"{tally['cycles_live_market']} market-hours cycle(s); "
             f"{tally['entries']} entr{'y' if tally['entries'] == 1 else 'ies'}, "
             f"{tally['exits']} exit(s), {tally['risk_approvals']} "
             f"approval(s)."]
    if tally.get("zero_trade_day"):
        lines.append("A zero-trade day: valid, not a failure.")
    if report:
        lines.append(f"Session report: "
                     f"{'ALL CHECKS PASSED' if report['session_ok'] else 'FAILED ' + ', '.join(report['failed_checks'])}"
                     f"; realised {_money(report.get('realized_pnl'))}.")
        lines.append(report.get("sample_adequacy_note", ""))
    else:
        lines.append("The session report is written after the close.")
    return _answer(" ".join(l for l in lines if l), ["tally", "report"],
                   intent="HOW_TODAY")
