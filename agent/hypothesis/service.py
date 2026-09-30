"""
Hypothesis orchestration.

Takes the outputs of the scanner, signal and evidence stages and turns
each candidate into a structured argument. Stores every outcome,
including the ones that produced nothing, because "we looked at NVDA and
declined" is exactly the question a decision log exists to answer.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from ..observability import log_event
from .engine import CONFIG_VERSION, generate
from .models import HypothesisRun, Strategy, TradeHypothesis, utcnow


class HypothesisService:
    def __init__(self, store=None, clock=None):
        self.store = store
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).date().isoformat())

    def generate_for(self, symbol: str, signal: Dict,
                     catalyst: Optional[Dict] = None,
                     regime: Optional[Dict] = None,
                     **ids) -> TradeHypothesis:
        return generate(symbol, signal, catalyst, regime, **ids)

    def run(self, inputs: Sequence[Dict], regime: Optional[Dict] = None,
            session_date: Optional[str] = None,
            scanner_run_id: Optional[str] = None,
            signal_run_id: Optional[str] = None,
            evidence_run_id: Optional[str] = None) -> HypothesisRun:
        """Generate hypotheses for a batch.

        `inputs` is a sequence of {"symbol", "signal", "catalyst"}.
        """
        started = time.time()
        session_date = session_date or self._clock()
        started_at = utcnow()

        run = HypothesisRun(
            hypothesis_run_id=HypothesisRun.make_id(session_date, started_at),
            session_date=session_date, started_at=started_at,
            scanner_run_id=scanner_run_id, signal_run_id=signal_run_id,
            evidence_run_id=evidence_run_id, config_version=CONFIG_VERSION,
            considered_count=len(inputs),
        )
        log_event("hypothesis_run_started",
                  hypothesis_run_id=run.hypothesis_run_id,
                  considered=len(inputs), config_version=CONFIG_VERSION)

        for entry in inputs:
            hypothesis = generate(
                entry.get("symbol", ""), entry.get("signal") or {},
                entry.get("catalyst"), regime,
                scanner_run_id=scanner_run_id, signal_run_id=signal_run_id,
                evidence_run_id=evidence_run_id)
            run.hypotheses.append(hypothesis)
            key = str(hypothesis.strategy)
            run.strategy_counts[key] = run.strategy_counts.get(key, 0) + 1
            if hypothesis.is_actionable:
                run.generated_count += 1
            else:
                run.rejected_count += 1

        run.completed_at = utcnow()
        run.duration_seconds = round(time.time() - started, 3)

        if self.store is not None:
            self.store.save_run(run)
            log_event("hypothesis_persisted",
                      hypothesis_run_id=run.hypothesis_run_id,
                      count=len(run.hypotheses))

        log_event("hypothesis_run_completed",
                  hypothesis_run_id=run.hypothesis_run_id,
                  considered=run.considered_count,
                  generated=run.generated_count,
                  rejected=run.rejected_count,
                  strategies=run.strategy_counts,
                  duration_seconds=run.duration_seconds)
        return run

    def actionable(self, run: HypothesisRun) -> List[TradeHypothesis]:
        """Hypotheses worth passing to the Risk Governor, strongest first.

        Ordering is a convenience for the caller, not a ranking of
        expected return. The Risk Governor evaluates each on its own
        merits and can reject all of them.
        """
        return sorted((h for h in run.hypotheses if h.is_actionable),
                      key=lambda h: h.hypothesis_strength, reverse=True)
