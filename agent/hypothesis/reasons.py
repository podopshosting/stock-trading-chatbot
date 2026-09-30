"""
Deterministic explanation of a hypothesis.

No language model. The same inputs always produce the same sentences,
because an explanation that can drift from the argument it claims to
explain reads as authoritative while being unfalsifiable.
"""
from __future__ import annotations

from typing import List, Sequence

from .models import ContradictionSeverity, Strategy, TradeHypothesis


def build_reasons(hypothesis: TradeHypothesis,
                  strategy_reasons: Sequence[str]) -> List[str]:
    lines: List[str] = list(strategy_reasons)
    q = hypothesis.quantitative
    e = hypothesis.evidence
    m = hypothesis.market

    if hypothesis.strategy is not Strategy.NO_VALID_STRATEGY:
        lines.append(
            f"Quantitative: {q.direction} with {q.agreement:.2f} agreement "
            f"across {q.opinionated_groups} independent group(s) and "
            f"{q.magnitude:.2f} magnitude.")

    if e.collected:
        if e.active_catalyst:
            lines.append(
                f"Evidence: {e.direction} {e.catalyst_type} catalyst, "
                f"materiality {e.materiality:.2f}, novelty {e.novelty:.2f}, "
                f"{e.independent_sources} independent source(s), "
                f"{e.primary_sources} primary.")
        else:
            lines.append(
                "Evidence: collected, no active catalyst. Price is moving "
                "without a published reason, which is a real observation "
                "rather than a missing one.")
    else:
        lines.append(
            "Evidence: not collected for this symbol, so a published "
            "negative could exist unseen.")

    lines.append(
        f"Market: regime {m.regime} (confidence {m.regime_confidence:.2f}), "
        f"risk posture {m.risk_posture}.")

    blocking = [c for c in hypothesis.contradictions
                if c.severity is ContradictionSeverity.BLOCKING]
    if blocking:
        lines.append(
            "Blocked by: "
            + "; ".join(f"{c.code} - {c.detail}" for c in blocking))
    else:
        others = [c for c in hypothesis.contradictions
                  if c.severity is not ContradictionSeverity.BLOCKING]
        if others:
            lines.append(
                "Arguing against: "
                + "; ".join(f"{c.code} ({c.detail})" for c in others[:4]))

    if hypothesis.is_actionable:
        lines.append(
            f"Hypothesis strength {hypothesis.hypothesis_strength:.2f}. This "
            f"is an argument for further evaluation, not approval to trade - "
            f"only the Risk Governor can approve anything, and it has not "
            f"run yet.")
    return lines
