"""
Grounding for the chat layer.

Turns persisted agent state into a factual context block, and answers the
handful of state questions deterministically without involving a model at
all.

The rule this module exists to enforce: **the chat may explain state, and
may not invent it.** If a field is absent, the answer says so. A model
asked "what is the market regime?" with no state available will otherwise
produce a confident, plausible, fabricated answer - which is exactly the
failure this project already hit when an unrecognised ticker produced
invented penny-stock analysis.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

UNAVAILABLE = "I don't have that yet - the agent has not recorded it."

# Questions answerable from stored state alone, with no model call.
#
# Two things about this table are load-bearing:
#
# 1. **Order matters** - the first match wins. The specific scanner
#    questions come first, because "what regime was used for the scan?"
#    would otherwise be caught by the bare \bregime\b pattern and answered
#    with current state instead of the scan's recorded gate.
#
# 2. **Every pattern is matched against LOWERCASED text**, so none may
#    depend on capital letters. An earlier [A-Z]{1,5} ticker pattern could
#    never fire for exactly that reason.
_PATTERNS = [
    # --- scanner (specific before general) ---
    ("scan_regime", r"\bregime\b.*\b(used|scan)\b"),
    ("scan_regime", r"\bscan\b.*\bregime\b"),
    ("scan_counts", r"\bhow many\b.*\b(symbol|stock|securit|asset)"),
    ("scan_counts", r"\b(scanned|scanner)\b.*\b(how many|count|universe)\b"),
    ("why_rejected", r"\bwhy\b.*\b(rejected|excluded|filtered out|not eligible)\b"),
    ("why_rejected", r"\b(was|were)\b.*\brejected\b"),
    ("why_symbol", r"\bwhy\b.*\b(on the list|a candidate|in the scan|ranked|listed)\b"),
    ("watching", r"\bwhat\b.*\bwatching\b"),
    ("watching", r"\b(top|best)\b.*\bcandidates?\b"),
    ("watching", r"\bcandidates?\b"),
    # --- agent and market state ---
    ("market_open", r"\b(is|are)\b.*\bmarket\b.*\b(open|closed|trading)\b"),
    ("market_open", r"\bmarket\s+(open|closed|status|session)\b"),
    ("regime_why", r"\bwhy\b.*\b(bullish|bearish|neutral|mixed|volatile)\b"),
    ("regime", r"\b(market\s+)?regime\b"),
    ("agent_state", r"\bwhat\b.*\b(are you doing|agent.*doing|state)\b"),
    ("agent_state", r"\bagent\s+(state|status)\b"),
    ("freshness", r"\b(how\s+)?(fresh|stale|old|current)\b.*\bdata\b"),
    ("freshness", r"\bdata\b.*\b(fresh|stale|how old)\b"),
    ("positions", r"\b(open\s+)?positions?\b"),
    ("pnl", r"\b(p&?l|pnl|profit|made|lost)\b"),
    ("trading_enabled", r"\b(trading|are you)\b.*\b(enabled|live|real money|actually trad)\b"),
]


def classify_question(query: str) -> Optional[str]:
    """Which state question, if any, this text is asking."""
    if not query:
        return None
    text = query.lower()
    for topic, pattern in _PATTERNS:
        if re.search(pattern, text):
            return topic
    return None


def _fmt_age(seconds) -> str:
    if seconds is None:
        return "unknown age"
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s old"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m old"
    return f"{seconds / 3600:.1f}h old"


def build_context(session_dict: Optional[Dict]) -> str:
    """A factual block for a prompt. Never speculative.

    Passed to the model as the only permitted source for state claims.
    """
    if not session_dict:
        return (
            "AGENT STATE: unavailable. No session record could be read.\n"
            "Do not guess any of these values. Say they are unavailable."
        )

    detail = session_dict.get("regime_detail") or {}
    lines: List[str] = [
        "AGENT STATE (the only permitted source for these facts):",
        f"- session_date: {session_dict.get('session_date')}",
        f"- agent_state: {session_dict.get('agent_state')}",
        f"- market_session: {session_dict.get('market_status')}",
        f"- trading_enabled: {session_dict.get('trading_enabled')} "
        f"(this system has no order execution at all)",
        f"- emergency_stop: {session_dict.get('emergency_stop')}",
        f"- daily_risk_lock: {session_dict.get('daily_risk_lock')}",
        f"- market_regime: {session_dict.get('market_regime')}",
        f"- regime_score: {session_dict.get('market_regime_score')}",
        f"- regime_confidence: {session_dict.get('market_regime_confidence')} "
        f"(agreement among inputs and data quality, NOT a probability of a "
        f"market direction)",
        f"- risk_posture: {session_dict.get('risk_posture')} "
        f"(a recommendation to a future risk layer, not a decision)",
        f"- regime_updated_at: {session_dict.get('regime_updated_at')}",
        f"- open_positions: {session_dict.get('open_positions')}",
        f"- realized_pnl: {session_dict.get('realized_pnl')}",
        f"- unrealized_pnl: {session_dict.get('unrealized_pnl')}",
        f"- daily_capital_limit: {session_dict.get('daily_capital_limit')}",
        f"- capital_deployed: {session_dict.get('capital_deployed')}",
    ]

    if detail.get("trend"):
        lines.append(
            f"- regime dimensions: trend={detail.get('trend')}, "
            f"risk_mode={detail.get('risk_mode')}, "
            f"volatility={detail.get('volatility')}, "
            f"breadth_proxy={detail.get('breadth_proxy')}"
        )
    for reason in (detail.get("reasons") or [])[:8]:
        lines.append(f"- reason: {reason}")
    for warning in (detail.get("warnings") or [])[:5]:
        lines.append(f"- warning: {warning}")

    inputs = detail.get("inputs") or {}
    for symbol, info in inputs.items():
        lines.append(
            f"- input {symbol}: price={info.get('price')} "
            f"provider={info.get('provider')} "
            f"age={_fmt_age(info.get('age_seconds'))} "
            f"as_of={info.get('as_of')}"
        )

    lines.append(
        "RULES: state only what appears above. If a value is missing, say it "
        "is unavailable rather than estimating it. Never describe this system "
        "as placing trades."
    )
    return "\n".join(lines)


SCANNER_UNAVAILABLE = (
    "I don't have a scanner run to describe yet - none has been recorded "
    "for this session."
)


def _symbol_in(query: str) -> Optional[str]:
    """Pull a plausible ticker out of the question."""
    for token in re.findall(r"\b[A-Z]{1,5}\b", query or ""):
        if token not in ("I", "A", "WHY", "WHAT", "IS", "WAS", "THE", "ON",
                         "NOT", "HOW", "MANY", "AND", "OR", "DID", "DO"):
            return token
    return None


def answer_scanner_question(query: str, run_dict: Optional[Dict]
                            ) -> Optional[str]:
    """Answer a scanner question from a stored run, or None to pass it on.

    Deterministic: these are facts the scanner recorded. Routing them
    through a model would add a fabrication risk and buy nothing.

    Every answer states that a candidate is a research priority, because a
    ranked list of tickers invites being read as a buy list.
    """
    topic = classify_question(query)
    if topic not in ("watching", "why_symbol", "why_rejected", "scan_counts",
                     "scan_regime"):
        return None
    if not run_dict:
        return SCANNER_UNAVAILABLE

    candidates = run_dict.get("candidates") or []
    disclaimer = ("These are research priorities - symbols worth a closer "
                  "look - not recommendations. This build cannot place "
                  "orders.")

    if topic == "watching":
        if not candidates:
            return (f"Nothing is on the list right now. The last scan "
                    f"({run_dict.get('status')}) produced no candidates. "
                    f"{disclaimer}")
        lines = []
        for cand in candidates[:10]:
            feat = cand.get("features") or {}
            change = feat.get("session_change_pct")
            lines.append(
                f"  {cand.get('rank')}. {cand.get('symbol')} "
                f"${cand.get('price')} "
                f"({change:+.2f}% today) score {cand.get('scanner_score')}"
                if isinstance(change, (int, float)) else
                f"  {cand.get('rank')}. {cand.get('symbol')} "
                f"${cand.get('price')} score {cand.get('scanner_score')}"
            )
        return (
            f"Watching {len(candidates)} symbols from the scan at "
            f"{run_dict.get('completed_at')}, under regime "
            f"{run_dict.get('market_regime')}:\n" + "\n".join(lines)
            + f"\n{disclaimer}"
        )

    if topic == "why_symbol":
        symbol = _symbol_in(query)
        if not symbol:
            return "Which symbol did you mean?"
        match = next((c for c in candidates
                      if c.get("symbol") == symbol), None)
        if match is None:
            rejected = [r for r in (run_dict.get("rejection_samples") or [])
                        if r.get("symbol") == symbol]
            if rejected:
                return (f"{symbol} was not a candidate. It was rejected at "
                        f"the {rejected[0].get('stage')} stage: "
                        f"{', '.join(rejected[0].get('reasons', []))}.")
            return (f"{symbol} was not in the last scan's candidates, and I "
                    f"have no recorded rejection reason for it.")
        feat = match.get("features") or {}
        parts = [f"{symbol} ranked {match.get('rank')} with score "
                 f"{match.get('scanner_score')} because:"]
        for reason in match.get("reasons", []):
            parts.append(f"  - {reason}")
        parts.append(
            f"  measured: spread {match.get('spread_pct')}%, "
            f"session {feat.get('session_change_pct')}%, "
            f"15m {feat.get('return_15m')}%, "
            f"relative volume {feat.get('relative_volume')}, "
            f"VWAP distance {feat.get('distance_from_vwap_pct')}%"
        )
        for warning in match.get("warnings", []):
            parts.append(f"  warning: {warning}")
        parts.append(disclaimer)
        return "\n".join(parts)

    if topic == "why_rejected":
        symbol = _symbol_in(query)
        samples = run_dict.get("rejection_samples") or []
        if symbol:
            match = next((r for r in samples
                          if r.get("symbol") == symbol), None)
            if match:
                return (f"{symbol} was rejected at the "
                        f"{match.get('stage')} stage: "
                        f"{', '.join(match.get('reasons', []))}.")
            return (f"I have no recorded rejection reason for {symbol}. Only "
                    f"a bounded sample of rejections is kept per run, "
                    f"alongside the full reason counts.")
        counts = run_dict.get("rejection_reason_counts") or {}
        if not counts:
            return "No rejections were recorded in the last scan."
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
        body = "\n".join(f"  {count:,} x {reason}" for reason, count in top)
        return (f"The last scan rejected "
                f"{run_dict.get('rejected_count', 0):,} symbols. "
                f"Most common reasons:\n{body}")

    if topic == "scan_counts":
        return (
            f"The last scan looked at {run_dict.get('universe_count', 0):,} "
            f"assets: {run_dict.get('static_eligible_count', 0):,} passed the "
            f"static filters, {run_dict.get('dynamic_eligible_count', 0):,} "
            f"passed the liquidity and freshness filters, "
            f"{run_dict.get('shortlist_count', 0):,} reached the shortlist, "
            f"and {run_dict.get('candidate_count', 0)} became candidates. "
            f"It used {run_dict.get('provider_calls', 0)} provider requests "
            f"in {run_dict.get('duration_seconds')}s."
        )

    if topic == "scan_regime":
        gate = run_dict.get("regime_gate") or {}
        return (
            f"The scan ran under regime {run_dict.get('market_regime')} "
            f"(confidence {run_dict.get('regime_confidence')}, posture "
            f"{run_dict.get('risk_posture')}). That gate required a minimum "
            f"score of {gate.get('min_score')} and applied a spread "
            f"multiplier of {gate.get('spread_multiplier')}."
            + (f" {gate.get('warning')}" if gate.get("warning") else "")
        )

    return None


SIGNAL_UNAVAILABLE = (
    "I don't have a signal reading for that symbol - the engine has not "
    "recorded one."
)

# Ordered most specific first. A generic "why <TICKER>" must be tried
# LAST or it swallows every other question.
#
# `case_sensitive` matters: the ticker pattern is [A-Z]{1,5}, and under
# IGNORECASE it matched the "is" in "Why is agreement lower?" - so every
# signal question classified as why_direction and the specific handlers
# were unreachable.
_SIGNAL_PATTERNS = [
    ("macd", r"\bmacd\b", False),
    ("rsi", r"\brsi\b", False),
    ("magnitude", r"\bmagnitude\b|\bhow strong\b", False),
    ("agreement", r"\bagreement\b", False),
    ("market_support", r"\b(?:market|regime)\b.*\bsupport", False),
    ("which_disagree",
     r"\bwhich\s+signals?\b.*\bdisagree|\bwhat\s+disagrees?\b"
     r"|\bdisagree(?:ment)?\b", False),
    ("why_direction", r"\bwhy\s+(?:is|was)\b.*\b(buy|sell|hold|neutral)\b",
     False),
    # Only the TICKER is case-sensitive; the leading word is not.
    ("why_direction", r"\b[Ww]hy\s+[A-Z]{2,5}\b", True),
]


def classify_signal_question(query: str) -> Optional[str]:
    for topic, pattern, case_sensitive in _SIGNAL_PATTERNS:
        flags = 0 if case_sensitive else re.IGNORECASE
        if re.search(pattern, query or "", flags):
            return topic
    return None


def answer_signal_question(query: str, result: Optional[Dict]
                           ) -> Optional[str]:
    """Answer from a stored signal result, or None to pass it on.

    Deterministic. Every number here was computed by the engine and
    recorded; routing the question through a model would let it invent a
    reading, and a fabricated MACD value is indistinguishable from a real
    one to the reader.
    """
    topic = classify_signal_question(query)
    if topic is None:
        return None
    if not result:
        return SIGNAL_UNAVAILABLE

    symbol = result.get("symbol", "this symbol")
    direction = result.get("direction", "UNKNOWN")
    groups = {g.get("group"): g for g in result.get("group_results", [])}
    indicators = {i.get("indicator"): i
                  for i in result.get("indicator_results", [])}

    def _group_line(g):
        label = g.get("label", g.get("group"))
        note = " (its own members disagree)" if g.get(
            "internal_disagreement") else ""
        return f"{label} says {g.get('direction')}{note}"

    if topic == "why_direction":
        reasons = result.get("reasons") or []
        head = f"{symbol} is {direction}."
        if reasons:
            return head + " " + " ".join(reasons[:3])
        return head + " No reasons were recorded."

    if topic == "which_disagree":
        directional = [g for g in groups.values()
                       if g.get("direction") in ("BUY", "SELL")]
        conflicted = [g for g in groups.values()
                      if g.get("internal_disagreement")]
        parts = []
        if len({g["direction"] for g in directional}) > 1:
            parts.append("Across groups: "
                         + "; ".join(_group_line(g) for g in directional)
                         + ".")
        for g in conflicted:
            members = [m for m in g.get("members", [])
                       if m.get("direction") in ("BUY", "SELL")]
            parts.append(
                f"Inside {g.get('label')}: "
                + ", ".join(f"{m.get('label')} is {m.get('direction')}"
                            for m in members) + ".")
        if not parts:
            return f"Nothing disagrees for {symbol}: no group opposes another."
        return " ".join(parts)

    if topic in ("macd", "rsi"):
        sig = indicators.get(topic)
        if not sig:
            return f"I don't have a {topic.upper()} reading for {symbol}."
        raw = sig.get("raw_values") or {}
        detail = ", ".join(f"{k}={v}" for k, v in raw.items() if v is not None)
        return (f"{symbol} {sig.get('label', topic.upper())}: "
                f"{sig.get('direction')} - {sig.get('reason')}."
                + (f" Values: {detail}." if detail else ""))

    if topic == "agreement":
        return (
            f"{symbol} signal agreement is "
            f"{result.get('signal_agreement')}. That is how many independent "
            f"groups concur ({result.get('buy_groups')} buy, "
            f"{result.get('sell_groups')} sell, "
            f"{result.get('opinionated_groups')} with an opinion of "
            f"{result.get('groups_total')}), scaled by whether each group's "
            f"own members agreed. It is not a probability."
        )

    if topic == "magnitude":
        return (
            f"{symbol} signal magnitude is "
            f"{result.get('signal_magnitude')} - how far the readings sit "
            f"beyond their thresholds, normalised for this security's own "
            f"volatility. That is separate from agreement, and it is not a "
            f"probability."
        )

    if topic == "market_support":
        return (
            f"The market regime is {result.get('market_regime')}, which "
            f"scales {symbol}'s magnitude by "
            f"{result.get('regime_adjustment')} to "
            f"{result.get('regime_adjusted_magnitude')}. The regime never "
            f"creates or reverses a direction."
        )

    return None


EVIDENCE_UNAVAILABLE = (
    "I have no evidence recorded for that symbol. That is not the same "
    "as there being no news - it means the agent has not collected any."
)

NO_CATALYST = (
    "No active catalyst. Evidence was collected and none of it is "
    "current and material enough to explain a move. That is a valid "
    "answer, not a gap."
)

_EVIDENCE_PATTERNS = [
    ("same_story", r"\bsame story\b|\bsame (?:event|article)\b"
                   r"|\bduplicat\w+\b|\bsyndicat\w+\b"),
    ("dilution", r"\bdilut\w+\b|\boffering\b|\bshelf\b|\bs-3\b"),
    ("filing", r"\bsec\b|\bfiling\b|\b8-?k\b|\b10-?[qk]\b|\bform 4\b"),
    ("earnings_today", r"\bearnings\b.*\b(?:today|when|due|upcoming)\b"
                       r"|\bis earnings\b"),
    ("is_new", r"\b(?:is|was) (?:this|it) (?:a )?new\b|\bnovel\w*\b"
               r"|\bhow new\b"),
    ("provenance", r"\bfrom the company\b|\bcompany or\b|\bwho (?:said|"
                   r"reported|published)\b|\bwhat source\b|\bprimary source\b"),
    ("why_moving", r"\bwhy is\b.*\bmoving\b|\bwhat(?:'s| is) moving\b"
                   r"|\bwhy.*\bmove[sd]?\b"),
    ("any_news", r"\bany news\b|\bis there news\b|\bnews on\b"
                 r"|\bwhat catalyst\b|\bcatalyst\b"),
]


def classify_evidence_question(query: str) -> Optional[str]:
    lowered = (query or "").lower()
    for topic, pattern in _EVIDENCE_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            return topic
    return None


def answer_evidence_question(query: str, catalyst: Optional[Dict]
                             ) -> Optional[str]:
    """Answer from a stored catalyst result, or None to pass it on.

    Deterministic, and grounded strictly in what was collected. The model
    is never asked to fill a gap from general knowledge: "I have no
    evidence" is a correct answer, and a plausible invented headline is
    indistinguishable from a real one to the reader.
    """
    topic = classify_evidence_question(query)
    if topic is None:
        return None
    if not catalyst:
        return EVIDENCE_UNAVAILABLE

    symbol = catalyst.get("symbol", "this symbol")
    items = catalyst.get("items") or catalyst.get("top_evidence") or []
    primary = catalyst.get("primary_catalyst")
    has = catalyst.get("has_active_catalyst")

    def _cite(entry):
        src = entry.get("source") or {}
        when = entry.get("published_at") or "time unknown"
        return (f"{entry.get('headline', '')[:110]} "
                f"[{src.get('publisher', 'unknown')}, {when}]")

    if topic in ("why_moving", "any_news"):
        if not has or not primary:
            extra = ""
            if items:
                extra = (f" {len(items)} item(s) were collected but none is "
                         f"current and material enough.")
            return NO_CATALYST + extra
        conflict = ""
        if catalyst.get("conflicting_evidence"):
            conflict = (" Note the evidence CONFLICTS: "
                        + "; ".join(catalyst.get("conflict_detail", [])[:2]))
        return (
            f"{symbol}: {primary.get('type')} - {primary.get('headline','')[:120]}. "
            f"Direction {primary.get('direction')}, materiality "
            f"{primary.get('materiality')}, novelty {primary.get('novelty')}, "
            f"{primary.get('window')}. Source: {primary.get('publisher')} "
            f"({primary.get('source_class')}), "
            f"{catalyst.get('independent_source_count', 0)} independent "
            f"source(s).{conflict}"
        )

    if topic == "same_story":
        collapsed = catalyst.get("duplicates_collapsed", 0)
        groups = catalyst.get("duplicate_groups", 0)
        total = catalyst.get("total_evidence_count", 0)
        if not collapsed:
            return (f"No duplicates: the {total} item(s) for {symbol} describe "
                    f"{groups} distinct event(s).")
        return (
            f"Yes. {total} item(s) collapsed into {groups} distinct event(s); "
            f"{collapsed} were retellings of a story already counted. "
            f"{catalyst.get('independent_source_count', 0)} genuinely "
            f"independent source(s) back the strongest one."
        )

    if topic == "filing":
        filings = [i for i in items
                   if (i.get("source") or {}).get("provider") == "sec"]
        if not filings:
            return f"No SEC filing was retrieved for {symbol} in this window."
        return (f"{len(filings)} SEC filing(s) for {symbol}: "
                + "; ".join(_cite(f) for f in filings[:3]))

    if topic == "dilution":
        financing = [i for i in items if i.get("type") in
                     ("SHELF_REGISTRATION", "SHARE_OFFERING", "DILUTION",
                      "ATM_OFFERING", "CONVERTIBLE_DEBT", "DEBT_OFFERING")]
        if not financing:
            return (f"No financing or dilution event was found for {symbol} "
                    f"in this window.")
        lines = []
        for f in financing[:3]:
            stage = (f.get("raw_metadata") or {}).get("financing_stage", "")
            note = ""
            if stage == "ABILITY_TO_ISSUE":
                note = (" - this is CAPACITY to issue securities later, not "
                        "an offering being sold now")
            elif stage == "ACTUAL_OFFERING":
                note = " - securities are being sold"
            lines.append(f"{f.get('type')}: {_cite(f)}{note}")
        return " | ".join(lines)

    if topic == "earnings_today":
        earnings = [i for i in items if i.get("type") in
                    ("EARNINGS", "UPCOMING_EARNINGS")]
        if not earnings:
            return (f"No earnings event was found for {symbol} in the "
                    f"collected evidence. I do not have an earnings calendar, "
                    f"so this is not a confirmation that none is scheduled.")
        return "; ".join(_cite(e) for e in earnings[:2])

    if topic == "is_new":
        if not primary:
            return NO_CATALYST
        return (
            f"Novelty {primary.get('novelty')} on a 0-1 scale, window "
            f"{primary.get('window')}. 1.0 is a genuinely new development; "
            f"a low value means the story restates something already known. "
            f"{catalyst.get('duplicates_collapsed', 0)} retelling(s) were "
            f"collapsed."
        )

    if topic == "provenance":
        if not primary:
            return NO_CATALYST
        klass = primary.get("source_class")
        explain = ("a primary source - the issuer or a government body"
                   if klass == "PRIMARY" else
                   "a licensed news feed carrying a publisher's account")
        return (
            f"{primary.get('publisher')} ({klass}) - {explain}. "
            f"{catalyst.get('primary_source_count', 0)} of the supporting "
            f"item(s) are primary. Reliability describes how confidently we "
            f"can say the source stated this, not whether the interpretation "
            f"is right."
        )

    return None


def answer_from_state(query: str, session_dict: Optional[Dict]) -> Optional[str]:
    """Answer a state question directly, or return None to let chat handle it.

    Deterministic by design: these answers are facts the agent recorded,
    so routing them through a model would add a fabrication risk and buy
    nothing.
    """
    topic = classify_question(query)
    if topic is None:
        return None
    if not session_dict:
        return UNAVAILABLE

    s = session_dict
    detail = s.get("regime_detail") or {}

    if topic == "market_open":
        session = s.get("market_status", "UNKNOWN")
        if session == "UNKNOWN":
            return ("I can't confirm the market session right now - the "
                    "broker clock was unreachable, so I'm treating it as "
                    "unknown rather than guessing.")
        readable = {
            "OPEN": "open for regular trading",
            "PRE_MARKET": "in pre-market",
            "AFTER_HOURS": "in after-hours",
            "CLOSED": "closed",
        }.get(session, session)
        return f"The market is {readable} (as recorded at {s.get('updated_at')})."

    if topic == "regime":
        regime = s.get("market_regime", "UNKNOWN")
        if regime == "UNKNOWN":
            return ("The market regime is UNKNOWN - not enough usable data "
                    "to classify it. I won't guess a direction.")
        return (
            f"Regime: {regime} (score {s.get('market_regime_score')}, "
            f"confidence {s.get('market_regime_confidence')}). "
            f"Trend {detail.get('trend', 'unknown')}, "
            f"volatility {detail.get('volatility', 'unknown')}, "
            f"breadth {detail.get('breadth_proxy', 'unknown')}. "
            f"Risk posture {s.get('risk_posture')}. "
            f"Last evaluated {s.get('regime_updated_at')}. "
            f"Confidence measures how well the inputs agree and how fresh "
            f"they are - it is not a probability that the market will rise."
        )

    if topic == "regime_why":
        reasons = detail.get("reasons") or []
        if not reasons:
            return ("I don't have recorded reasons for the current regime - "
                    "it hasn't been evaluated yet.")
        body = "\n".join(f"  - {r}" for r in reasons[:8])
        return (f"Regime {s.get('market_regime')} "
                f"(score {s.get('market_regime_score')}) because:\n{body}")

    if topic == "agent_state":
        extra = ""
        if s.get("emergency_stop"):
            extra = " An emergency stop is active."
        elif s.get("daily_risk_lock"):
            extra = " A daily risk lock is active."
        return (f"Agent state: {s.get('agent_state')}. "
                f"Market session {s.get('market_status')}.{extra} "
                f"Trading is {'enabled' if s.get('trading_enabled') else 'disabled'}, "
                f"and there is no order execution in this build.")

    if topic == "freshness":
        inputs = detail.get("inputs") or {}
        if not inputs:
            return ("No market data inputs are recorded yet, so I can't tell "
                    "you how fresh they are.")
        parts = [f"{sym} {_fmt_age(i.get('age_seconds'))} from "
                 f"{i.get('provider') or 'unknown provider'}"
                 for sym, i in inputs.items()]
        quality = detail.get("data_quality") or {}
        suffix = ""
        if quality:
            suffix = (f" Of {quality.get('instruments_requested')} instruments: "
                      f"{quality.get('fresh')} fresh, {quality.get('stale')} stale, "
                      f"{quality.get('missing')} missing.")
        return "Market data ages: " + "; ".join(parts) + "." + suffix

    if topic == "positions":
        return (f"Open positions: {s.get('open_positions', 0)}. "
                f"This build has no order execution, so there is nothing to "
                f"open a position with.")

    if topic == "pnl":
        return (f"Realized P&L {s.get('realized_pnl', 0)}, "
                f"unrealized {s.get('unrealized_pnl', 0)}, "
                f"capital deployed {s.get('capital_deployed', 0)} of a "
                f"{s.get('daily_capital_limit')} daily limit. "
                f"These are zero because no trading has occurred - the agent "
                f"cannot place orders in this build.")

    if topic == "trading_enabled":
        return (f"Trading is "
                f"{'enabled' if s.get('trading_enabled') else 'disabled'} in "
                f"state, but more importantly this build contains no order "
                f"execution path of any kind - no broker adapter, paper or "
                f"live. Nothing can be bought or sold.")

    return None
