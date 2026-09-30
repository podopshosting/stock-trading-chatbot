"""
AgentStateService: the only thing that mutates agent session state.

Every write is read-modify-write against the store's revision, retried on
a concurrent update. Callers pass a function describing the change rather
than handing over a session object they might have been holding for a
while - that removes the whole class of bug where a stale in-memory copy
overwrites someone else's work.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from ..config import DEFAULT_CONFIG, AgentConfig
from ..observability import log_event
from .models import AgentSession, AgentState, MarketSession
from .store import ConcurrentUpdate, StateStore, today_market_date


class AgentStateService:

    MAX_RETRIES = 4

    def __init__(self, store: StateStore, config: AgentConfig = DEFAULT_CONFIG,
                 clock: Optional[Callable[[], str]] = None):
        self.store = store
        self.config = config
        self._clock = clock or (lambda: today_market_date(config.market_timezone))

    # -- reads ------------------------------------------------------------

    def session_date(self) -> str:
        return self._clock()

    def get_session(self, session_date: Optional[str] = None) -> AgentSession:
        date = session_date or self.session_date()
        created = self.store.get(date) is None
        session = self.store.get_or_create(date, defaults={
            "trading_enabled": self.config.trading_enabled,
            "daily_capital_limit": self.config.capital.daily_capital_limit,
        })
        if created:
            log_event("agent_state_created", session_date=date,
                      agent_state=str(session.agent_state),
                      trading_enabled=session.trading_enabled)
        return session

    # -- writes -----------------------------------------------------------

    def _mutate(self, change: Callable[[AgentSession], object],
                session_date: Optional[str] = None):
        """Apply `change` to the current session under optimistic locking.

        Retries on ConcurrentUpdate by re-reading, so the change is always
        applied to the newest state rather than to whatever we read first.
        """
        date = session_date or self.session_date()
        last_error: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES):
            session = self.get_session(date)
            expected = session.revision
            result = change(session)
            try:
                self.store.put(session, expected_revision=expected)
                return session, result
            except ConcurrentUpdate as e:
                last_error = e
                log_event("agent_state_write_retry", session_date=date,
                          attempt=attempt + 1, expected_revision=expected)
                continue

        raise last_error  # type: ignore[misc]

    def transition(self, target: AgentState, reason: str = "",
                   session_date: Optional[str] = None) -> AgentSession:
        target = AgentState.parse(target)

        def change(session: AgentSession):
            source = session.agent_state
            changed = session.transition_to(target, reason)
            return source, changed

        session, (source, changed) = self._mutate(change, session_date)
        if changed:
            log_event("agent_state_transition", session_date=session.session_date,
                      source=str(source), target=str(target), reason=reason)
        return session

    def set_market_session(self, market: MarketSession,
                           session_date: Optional[str] = None) -> AgentSession:
        def change(session: AgentSession):
            previous = session.market_status
            if previous is market:
                return previous, False
            session.market_status = market
            session._touch()
            return previous, True

        session, (previous, changed) = self._mutate(change, session_date)
        if changed:
            log_event("market_session_changed",
                      session_date=session.session_date,
                      previous=str(previous), current=str(market))
        return session

    def record_regime(self, regime: str, score: Optional[float],
                      confidence: float, risk_posture: str,
                      detail: Optional[Dict] = None,
                      session_date: Optional[str] = None) -> AgentSession:
        delta = self.config.regime.thresholds.min_score_delta_for_transition

        def change(session: AgentSession):
            return session.record_regime(
                regime=regime, score=score, confidence=confidence,
                risk_posture=risk_posture, detail=detail,
                min_score_delta=delta,
            )

        session, transition = self._mutate(change, session_date)
        log_event("regime_evaluated", session_date=session.session_date,
                  regime=regime, score=score, confidence=confidence,
                  risk_posture=risk_posture)
        if regime == "UNKNOWN":
            log_event("regime_unknown", session_date=session.session_date,
                      reason=(detail or {}).get("primary_reason", ""))
        if transition is not None:
            log_event("regime_changed", session_date=session.session_date,
                      previous=transition.previous_regime,
                      current=transition.new_regime,
                      previous_score=transition.previous_score,
                      new_score=transition.new_score)
        return session

    def trigger_emergency_stop(self, reason: str,
                               session_date: Optional[str] = None) -> AgentSession:
        session, _ = self._mutate(
            lambda s: s.trigger_emergency_stop(reason), session_date
        )
        log_event("emergency_stop_triggered",
                  session_date=session.session_date, reason=reason)
        return session

    def clear_emergency_stop(self, reason: str = "",
                             session_date: Optional[str] = None) -> AgentSession:
        session, _ = self._mutate(
            lambda s: s.clear_emergency_stop(reason), session_date
        )
        log_event("emergency_stop_cleared",
                  session_date=session.session_date, reason=reason)
        return session

    def set_daily_risk_lock(self, reason: str,
                            session_date: Optional[str] = None) -> AgentSession:
        session, _ = self._mutate(
            lambda s: s.set_daily_risk_lock(reason), session_date
        )
        log_event("daily_risk_lock_set",
                  session_date=session.session_date, reason=reason)
        return session

    def mark_scan(self, at: str, session_date: Optional[str] = None) -> AgentSession:
        def change(session: AgentSession):
            session.last_scan_at = at
            session._touch()

        session, _ = self._mutate(change, session_date)
        return session
