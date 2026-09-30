"""
Deterministic explanation of a quantitative signal result.

No language model. The same result always produces the same sentences,
because an explanation that can drift from the numbers it claims to
explain is worse than no explanation: it reads as authoritative while
being unfalsifiable.

Everything here is derived from the structured result. Nothing is
restated from an indicator's own prose without the structure behind it.
"""
from __future__ import annotations

from typing import List

from .models import (
    GroupDirection, QuantitativeSignalResult, SignalDirection,
)


def _join(labels: List[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return " and ".join([", ".join(labels[:-1]), labels[-1]]) \
        if len(labels) > 2 else " and ".join(labels)


def build_reasons(result: QuantitativeSignalResult) -> List[str]:
    lines: List[str] = []
    groups = result.group_results

    directional = [g for g in groups if g.direction.is_directional]
    supporting = [g for g in directional
                  if str(g.direction) == str(result.direction)]
    opposing = [g for g in directional
                if str(g.direction) != str(result.direction)]
    conflicted = [g for g in groups if g.internal_disagreement]
    silent = [g for g in groups if g.direction is GroupDirection.NO_SIGNAL]
    neutral = [g for g in groups if g.direction is GroupDirection.NEUTRAL]
    deadlocked = [g for g in groups if g.direction is GroupDirection.MIXED]

    # --- the headline conclusion ---
    if result.direction.is_directional:
        if len(supporting) >= 2:
            lines.append(
                f"{_join([g.label for g in supporting])} independently "
                f"support {result.direction}."
            )
            lines.append(
                "Agreement across independent groups is stronger evidence "
                "than agreement within one."
            )
        elif supporting:
            lines.append(
                f"Only {supporting[0].label} supports {result.direction}."
            )
            lines.append(
                "A single group is weaker evidence than agreement across "
                "groups."
            )
        if opposing:
            lines.append(
                f"{_join([g.label for g in opposing])} argues the other way, "
                f"which lowers signal agreement."
            )
    else:
        if len(directional) >= 2 and not supporting:
            buy = [g.label for g in directional
                   if g.direction is GroupDirection.BUY]
            sell = [g.label for g in directional
                    if g.direction is GroupDirection.SELL]
            if buy and sell:
                lines.append(
                    f"{_join(buy)} points up while {_join(sell)} points "
                    f"down."
                )
                lines.append(
                    "Independent groups disagree, so there is not enough "
                    "agreement for a directional call."
                )
        elif not directional:
            lines.append("No independent group produced a directional signal.")

    # --- why a group's opinion is weakened ---
    for group in conflicted:
        members = [m for m in group.members if m.direction.is_directional]
        detail = _join([f"{m.label} is {m.direction}" for m in members])
        lines.append(
            f"Inside {group.label}, {detail} - that internal disagreement "
            f"halves the group's weight."
        )

    for group in deadlocked:
        lines.append(
            f"{group.label} is deadlocked: its members oppose each other "
            f"with equal strength, so it casts no vote."
        )

    # --- what was measured but said nothing, vs what could not be measured ---
    if neutral:
        lines.append(
            f"{_join([g.label for g in neutral])} "
            f"{'was' if len(neutral) == 1 else 'were'} measured but did not "
            f"cross a threshold, so {'it casts' if len(neutral) == 1 else 'they cast'} "
            f"no vote."
        )
    if silent:
        lines.append(
            f"{_join([g.label for g in silent])} could not be computed, so "
            f"{'it was' if len(silent) == 1 else 'they were'} not counted. "
            f"That is missing evidence, not a neutral reading."
        )

    # --- magnitude, stated separately from agreement ---
    if result.direction.is_directional:
        lines.append(
            f"Signal agreement is {result.signal_agreement:.2f} and signal "
            f"magnitude is {result.signal_magnitude:.2f}. Agreement is how "
            f"many independent groups concur; magnitude is how far the "
            f"readings sit beyond their thresholds. Neither is a "
            f"probability."
        )

    # --- regime context ---
    if result.direction.is_directional and result.regime_adjustment != 1.0:
        lines.append(
            f"The {result.market_regime} market regime scales magnitude to "
            f"{result.regime_adjusted_magnitude:.2f}. The regime never "
            f"creates or reverses a direction."
        )

    return lines


def indicator_reasons(result: QuantitativeSignalResult) -> List[str]:
    """One line per indicator, for a detail view."""
    return [f"{s.label}: {s.reason}" for s in result.indicator_results]
