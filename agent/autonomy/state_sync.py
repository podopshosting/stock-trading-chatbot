"""
Keep the visible agent state in step with what the cycle actually did.

This is PRESENTATION. The dashboard shows "what is the agent doing now",
and that has to reflect reality - but nothing here influences trading,
so every failure is swallowed. A state-machine hiccup must never be the
reason a position goes unmanaged.

The state graph is strict (WATCHING cannot jump straight to
POSITION_OPEN), so reaching a target means walking valid edges. The path
is found by search rather than hardcoded, so it stays correct if the
graph changes.

EMERGENCY_STOP and DAILY_RISK_LOCK go through the state service's own
methods. The graph deliberately makes EMERGENCY_STOP terminal: clearing
it is a human act, and nothing in this module can do it.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional

from ..state.models import AgentState, allowed_targets


_PROTECTIVE = {AgentState.EMERGENCY_STOP,
               AgentState.DAILY_RISK_LOCK}


def target_state(cycle: Dict) -> Optional[AgentState]:
    """What state the cycle's outcome implies. None means leave it."""
    phase = cycle.get("phase")
    if phase == "CLOSED":
        return AgentState.MARKET_CLOSED
    if phase == "PRE_MARKET":
        return AgentState.PRE_MARKET
    if phase == "UNKNOWN":
        return None
    if cycle.get("emergency_stop_engaged"):
        return AgentState.EMERGENCY_STOP
    if "DAILY_LOSS_LIMIT" in (cycle.get("halt_reasons") or []):
        return AgentState.DAILY_RISK_LOCK
    if (cycle.get("open_positions") or 0) > 0:
        return (AgentState.POSITION_EXITING
                if (cycle.get("exits_submitted") or 0) > 0
                else AgentState.POSITION_OPEN)
    return (AgentState.WATCHING if (cycle.get("hypotheses_generated") or 0)
            else AgentState.SCANNING)


def path_between(source: AgentState, target: AgentState
                 ) -> Optional[List[AgentState]]:
    """Shortest valid chain of transitions, or None if unreachable."""
    if source == target:
        return []
    seen = {source}
    queue = deque([(source, [])])
    while queue:
        node, path = queue.popleft()
        for nxt in allowed_targets(node):
            if nxt in seen:
                continue
            # Never route THROUGH a protective state. The graph makes
            # them reachable from everywhere, so an unrestricted search
            # happily goes WATCHING -> DAILY_RISK_LOCK -> POSITION_EXITING
            # and would engage a session-long lock as a side effect of
            # drawing a status badge.
            if nxt in _PROTECTIVE and nxt != target:
                continue
            if nxt == target:
                return path + [nxt]
            seen.add(nxt)
            queue.append((nxt, path + [nxt]))
    return None


def sync_state(state_service, cycle: Dict, session_date: str) -> Dict:
    """Best effort. Returns what happened; never raises."""
    outcome = {"target": None, "moved": [], "error": None}
    try:
        target = target_state(cycle)
        outcome["target"] = str(target) if target else None
        if target is None:
            return outcome
        session = state_service.get_session(session_date)
        current = session.agent_state
        if current == target:
            return outcome

        if target is AgentState.EMERGENCY_STOP:
            state_service.trigger_emergency_stop(
                "; ".join(cycle.get("errors") or [])[:200]
                or "emergency stop engaged by the cycle", session_date)
            outcome["moved"].append("EMERGENCY_STOP")
            return outcome
        if target is AgentState.DAILY_RISK_LOCK:
            state_service.set_daily_risk_lock(
                "daily loss limit reached", session_date)
            outcome["moved"].append("DAILY_RISK_LOCK")
            return outcome

        # Never walk OUT of a protective state: the graph forbids it and
        # only a human may.
        if current in (AgentState.EMERGENCY_STOP,
                       AgentState.DAILY_RISK_LOCK):
            if target is AgentState.MARKET_CLOSED:
                state_service.transition(target, "market closed",
                                         session_date)
                outcome["moved"].append(str(target))
            return outcome

        for step in path_between(current, target) or []:
            state_service.transition(step, "cycle sync", session_date)
            outcome["moved"].append(str(step))
    except Exception as exc:                              # noqa: BLE001
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return outcome
