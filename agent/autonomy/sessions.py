"""
Session accounting, the formal session report, and readiness evidence.

Three jobs that share one data source:

1. A running TALLY per session, updated by every cycle.
2. A SESSION REPORT, written once at the close, verifying that the day
   ended the way the policy says it must.
3. EVIDENCE for the readiness gate, aggregated from finalized sessions
   of the CURRENT cohort only.

The readiness gates are not changed to make them pass. This module only
supplies what they measure, and supplies it honestly: a session counts
toward "sessions completed" only if it actually ran during market hours
and finalized, and a day on which the agent correctly did nothing is
reported as a zero-trade day, not hidden.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..observability import log_event
from .versions import cohort_key

LIVE_PHASES = {"OPENING", "INTRADAY", "PRE_CLOSE"}

# Cash is compared to expected within this tolerance.
CASH_TOLERANCE = 0.01


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionTally:
    session_date: str
    cohort: str = ""
    versions: Dict = field(default_factory=dict)
    # Every code SHA this session actually ran under, in the order first
    # seen. `versions` holds only the one the tally was OPENED with, so a
    # session that spans a redeploy would otherwise be attributed
    # entirely to its first runtime - which is how 14 cycles came to be
    # recorded under a SHA that ran one of them.
    code_shas: List[str] = field(default_factory=list)
    # Per-cycle data quality, so a session cannot claim to be real-time
    # evidence when its fills were computed on a delayed feed.
    realtime_data_cycles: int = 0
    delayed_data_cycles: int = 0
    unknown_data_quality_cycles: int = 0
    # Per-cycle FeedQuality counts (REALTIME_SIP, REALTIME_IEX,
    # DELAYED_SIP, STALE, UNKNOWN). Kept as counts rather than a single
    # verdict so "which feed, how often" is never lost to aggregation.
    feed_quality_counts: Dict[str, int] = field(default_factory=dict)

    cycles_total: int = 0
    cycles_live_market: int = 0
    cycles_completed: int = 0
    cycles_halted: int = 0
    cycles_aborted: int = 0
    cycles_skipped_duplicate: int = 0
    live_cycles_with_real_quotes: int = 0

    symbols_scanned: int = 0
    hypotheses: int = 0
    risk_approvals: int = 0
    entries: int = 0
    exits: int = 0
    exits_failed: int = 0
    trades_journalled: int = 0

    quotes_requested: int = 0
    quotes_missing: int = 0
    quotes_stale: int = 0
    duplicate_attempts: int = 0
    reconciliation_checks: int = 0
    reconciliation_failures: int = 0
    emergency_stops: int = 0
    risk_locks: int = 0
    eod_flatten_failures: int = 0
    alerts: List[str] = field(default_factory=list)
    rejections_by_code: Dict[str, int] = field(default_factory=dict)
    halt_reasons: Dict[str, int] = field(default_factory=dict)
    first_cycle_at: Optional[str] = None
    last_cycle_at: Optional[str] = None
    # Cash at the first cycle of the day. The end-of-day cash check is
    # against THIS, not the account's starting balance: the starting
    # balance is only right on day one, and from day two it would report
    # a reconciliation failure on a perfectly healthy account.
    opening_cash: Optional[float] = None
    finalized: bool = False
    revision: int = 0

    def absorb(self, cycle: Dict, decisions: Optional[List[Dict]] = None
               ) -> None:
        """Fold one cycle's result into the tally."""
        self.cycles_total += 1
        stamp = cycle.get("finished_at") or _now()
        self.first_cycle_at = self.first_cycle_at or stamp
        self.last_cycle_at = stamp

        outcome = cycle.get("outcome")
        if outcome == "COMPLETED" or outcome == "COMPLETED_WITH_ERRORS":
            self.cycles_completed += 1
        elif outcome == "HALTED":
            self.cycles_halted += 1
        elif outcome == "ABORTED":
            self.cycles_aborted += 1
        elif outcome == "SKIPPED_DUPLICATE":
            self.cycles_skipped_duplicate += 1

        is_live = cycle.get("phase") in LIVE_PHASES
        if is_live:
            self.cycles_live_market += 1
            requested = cycle.get("quotes_requested") or 0
            missing = cycle.get("quotes_missing") or 0
            if requested and missing < requested:
                self.live_cycles_with_real_quotes += 1
                # An unreported feed is UNKNOWN, not real-time: a cycle
                # from before the agent recorded this must not be
                # promoted by the silence.
                quality = cycle.get("data_quality") or "UNKNOWN"
                self.feed_quality_counts[quality] = \
                    self.feed_quality_counts.get(quality, 0) + 1
                # Only the consolidated tape, in real time, counts as
                # real-time evidence. IEX is real-time and not the
                # national best bid and offer.
                if quality in ("REAL_TIME", "REALTIME_SIP"):
                    self.realtime_data_cycles += 1
                elif quality in ("DELAYED", "DELAYED_SIP", "REALTIME_IEX",
                                 "STALE"):
                    self.delayed_data_cycles += 1
                else:
                    self.unknown_data_quality_cycles += 1

        self.symbols_scanned += cycle.get("symbols_scanned") or 0
        self.hypotheses += cycle.get("hypotheses_generated") or 0
        self.risk_approvals += cycle.get("decisions_approved") or 0
        self.entries += cycle.get("entries_submitted") or 0
        self.exits += cycle.get("exits_submitted") or 0
        self.exits_failed += cycle.get("exits_failed") or 0
        self.trades_journalled += cycle.get("trades_journalled") or 0
        self.quotes_requested += cycle.get("quotes_requested") or 0
        self.quotes_missing += cycle.get("quotes_missing") or 0
        self.quotes_stale += cycle.get("quotes_stale") or 0
        self.duplicate_attempts += cycle.get("duplicate_attempts") or 0

        steps = {s["name"]: s for s in cycle.get("steps", [])}
        if "reconcile" in steps:
            self.reconciliation_checks += 1
            if not steps["reconcile"].get("ok"):
                self.reconciliation_failures += 1
        if cycle.get("emergency_stop_engaged"):
            self.emergency_stops += 1
        if cycle.get("eod_flatten_failed"):
            self.eod_flatten_failures += 1
        for reason in cycle.get("halt_reasons", []):
            self.halt_reasons[reason] = self.halt_reasons.get(reason, 0) + 1
            if reason == "DAILY_LOSS_LIMIT":
                self.risk_locks += 1
        for kind in cycle.get("alerts_raised", []):
            if kind not in self.alerts:
                self.alerts.append(kind)

        for row in decisions or []:
            if row.get("outcome") == "REFUSED":
                for code in (row.get("risk") or {}).get("reason_codes", []):
                    self.rejections_by_code[code] = \
                        self.rejections_by_code.get(code, 0) + 1

    @property
    def ran_during_market_hours(self) -> bool:
        return self.cycles_live_market > 0

    @property
    def zero_trade_day(self) -> bool:
        return self.ran_during_market_hours and self.entries == 0

    @property
    def single_runtime_version(self) -> Optional[bool]:
        """Did one program produce this whole session?

        None when no SHA was recorded at all: unknown, not yes.
        """
        if not self.code_shas:
            return None
        return len(self.code_shas) == 1

    @property
    def classification(self) -> Dict:
        from .evidence_class import classify_session
        return classify_session(self)

    def as_dict(self) -> Dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["ran_during_market_hours"] = self.ran_during_market_hours
        d["zero_trade_day"] = self.zero_trade_day
        d["single_runtime_version"] = self.single_runtime_version
        d["classification"] = self.classification
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "SessionTally":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


class SessionStore:
    def get(self, session_date: str) -> Optional[SessionTally]:
        raise NotImplementedError

    def put(self, tally: SessionTally) -> None:
        raise NotImplementedError

    def list(self, limit: int = 400) -> List[SessionTally]:
        raise NotImplementedError

    def get_report(self, session_date: str) -> Optional[Dict]:
        raise NotImplementedError

    def put_report(self, session_date: str, report: Dict) -> bool:
        """Write once. Returns False if one already exists."""
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    def __init__(self):
        self._tallies: Dict[str, Dict] = {}
        self._reports: Dict[str, Dict] = {}

    def get(self, session_date):
        d = self._tallies.get(session_date)
        return SessionTally.from_dict(json.loads(json.dumps(d))) if d else None

    def put(self, tally):
        tally.revision += 1
        self._tallies[tally.session_date] = json.loads(
            json.dumps(tally.as_dict(), default=str))

    def list(self, limit=400):
        return [SessionTally.from_dict(d) for _, d in
                sorted(self._tallies.items())][-limit:]

    def get_report(self, session_date):
        return self._reports.get(session_date)

    def put_report(self, session_date, report):
        if session_date in self._reports:
            return False
        self._reports[session_date] = report
        return True


class DynamoDBSessionStore(SessionStore):
    """PK = SESSIONS (one partition, so listing is one query),
    SK = <date>. Reports under PK = SESSIONREPORT."""

    def __init__(self, table_name: Optional[str] = None, client=None):
        self.table_name = table_name or os.environ.get(
            "AGENT_JOURNAL_TABLE", "stock-agent-dev-journal")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("dynamodb")
        return self._client

    def get(self, session_date):
        r = self.client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": "SESSIONS"}, "SK": {"S": session_date}},
            ConsistentRead=True)
        item = r.get("Item")
        return (SessionTally.from_dict(json.loads(item["payload"]["S"]))
                if item else None)

    def put(self, tally):
        tally.revision += 1
        self.client.put_item(
            TableName=self.table_name,
            Item={"PK": {"S": "SESSIONS"}, "SK": {"S": tally.session_date},
                  "payload": {"S": json.dumps(tally.as_dict(),
                                              default=str)}})

    def list(self, limit=400):
        r = self.client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": "SESSIONS"}},
            Limit=limit)
        return [SessionTally.from_dict(json.loads(i["payload"]["S"]))
                for i in r.get("Items", [])]

    def get_report(self, session_date):
        r = self.client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": "SESSIONREPORT"}, "SK": {"S": session_date}},
            ConsistentRead=True)
        item = r.get("Item")
        return json.loads(item["payload"]["S"]) if item else None

    def put_report(self, session_date, report):
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={"PK": {"S": "SESSIONREPORT"},
                      "SK": {"S": session_date},
                      "payload": {"S": json.dumps(report, default=str)}},
                ConditionExpression="attribute_not_exists(SK)")
            return True
        except Exception as exc:                          # noqa: BLE001
            if "ConditionalCheckFailed" in str(exc):
                return False
            raise


def record_cycle(store: SessionStore, session_date: str, cycle: Dict,
                 versions: Dict, decisions: Optional[List[Dict]] = None,
                 opening_cash: Optional[float] = None) -> SessionTally:
    """Fold a cycle into the day's tally.

    A cycle under DIFFERENT behavioural versions than the tally was
    started with does not get merged in: it would pool two experiments.
    The mismatch is recorded on the tally instead.
    """
    tally = store.get(session_date) or SessionTally(
        session_date=session_date, versions=dict(versions),
        cohort=cohort_key(versions))
    if tally.cohort and tally.cohort != cohort_key(versions):
        tally.halt_reasons["COHORT_CHANGED_MID_SESSION"] = \
            tally.halt_reasons.get("COHORT_CHANGED_MID_SESSION", 0) + 1
    # The code SHA is NOT part of the cohort key (a docs commit must not
    # reset the evidence), so a redeploy inside a session is invisible to
    # the check above. Record it here: a session with two SHAs is not a
    # measurement of either one.
    sha = (versions or {}).get("code_sha")
    if sha and sha not in tally.code_shas:
        tally.code_shas.append(sha)
        if len(tally.code_shas) > 1:
            tally.halt_reasons["RUNTIME_VERSION_CHANGED_MID_SESSION"] = \
                tally.halt_reasons.get(
                    "RUNTIME_VERSION_CHANGED_MID_SESSION", 0) + 1
    if tally.opening_cash is None and opening_cash is not None:
        tally.opening_cash = opening_cash
    tally.absorb(cycle, decisions)
    store.put(tally)
    return tally


def build_report(tally: SessionTally, trades: List, positions_open: int,
                 broker_positions: Optional[List[Dict]],
                 account: Optional[Dict], describe_fn=None) -> Dict:
    """The formal end-of-day verification.

    Each check is reported individually and `session_ok` is derived from
    them, so a session cannot read as clean while one of its checks
    failed.
    """
    realized = sum(t.net_pnl for t in trades)
    cash = (account or {}).get("cash")
    # Independent of the broker: opening cash plus what the JOURNAL says
    # the day earned. If the broker's cash disagrees, either a trade is
    # missing from the journal or the broker did something unrecorded.
    # With no recorded opening cash this cannot be checked, and an
    # unverifiable reconciliation is reported as failed, not passed.
    expected_cash = (None if tally.opening_cash is None
                     else tally.opening_cash + realized)
    cash_ok = (cash is not None and expected_cash is not None
               and abs(cash - expected_cash) <= CASH_TOLERANCE)

    broker_flat = (broker_positions is not None
                   and len(broker_positions) == 0)
    journal_complete = (tally.entries == len(trades) + positions_open)

    checks = {
        "no_positions_open": positions_open == 0,
        "broker_flat": broker_flat,
        "cash_reconciles": cash_ok,
        "journal_complete": journal_complete,
        "no_eod_flatten_failure": tally.eod_flatten_failures == 0,
        "reconciliation_clean": tally.reconciliation_failures == 0,
        "no_emergency_stop": tally.emergency_stops == 0,
        "ran_during_market_hours": tally.ran_during_market_hours,
    }
    performance = describe_fn(trades) if describe_fn else {}
    return {
        "session_date": tally.session_date,
        "cohort": tally.cohort,
        "versions": tally.versions,
        "written_at": _now(),
        "checks": checks,
        "session_ok": all(checks.values()),
        "failed_checks": [k for k, v in checks.items() if not v],
        "cash": {"actual": cash,
                 "opening": tally.opening_cash,
                 "expected": (None if expected_cash is None
                              else round(expected_cash, 4)),
                 "tolerance": CASH_TOLERANCE},
        "tally": tally.as_dict(),
        "zero_trade_day": tally.zero_trade_day,
        "trades": len(trades),
        "realized_pnl": round(realized, 4),
        "performance_verdict": performance.get("verdict"),
        "performance_summary": performance.get("summary"),
        "sample_adequacy_note": (
            "A single session cannot demonstrate an edge. The verdict "
            "above is the journal's own and is reported unchanged."),
    }


def finalize_session(store: SessionStore, session_date: str, trades: List,
                     positions_open: int, broker_positions, account,
                     describe_fn=None) -> Optional[Dict]:
    """Write the session report once, and mark the tally finalized.

    Idempotent: a second call returns the existing report. Returns None
    if the agent never ran during market hours that day (a holiday, say)
    - there is nothing to report and inventing a session would inflate
    the count of sessions completed.
    """
    existing = store.get_report(session_date)
    if existing is not None:
        return existing
    tally = store.get(session_date)
    if tally is None or not tally.ran_during_market_hours:
        return None
    report = build_report(tally, trades, positions_open, broker_positions,
                          account, describe_fn)
    if store.put_report(session_date, report):
        tally.finalized = True
        store.put(tally)
        log_event("session_report_written", session_date=session_date,
                  session_ok=report["session_ok"],
                  failed=report["failed_checks"])
    return store.get_report(session_date) or report


def aggregate_evidence(tallies: List[SessionTally], current_cohort: str
                       ) -> Dict:
    """What the readiness gate measures, for the CURRENT cohort only.

    Sessions from other cohorts are counted and listed but never pooled:
    they ran different behaviour, so adding them would produce a number
    describing neither.
    """
    from .evidence_class import STRATEGY_GRADE, classify_evidence, classify_session
    mine = [t for t in tallies if t.cohort == current_cohort]
    finalized = [t for t in mine if t.finalized and t.ran_during_market_hours]
    # A session that spanned a redeploy is not a measurement of either
    # runtime, so it is named and set aside rather than counted.
    classified = [(t, classify_session(t)) for t in finalized]
    void = [(t, c) for t, c in classified
            if c["evidence_class"] == "VOID"]
    countable = [t for t, c in classified if c["evidence_class"] != "VOID"]
    strategy_grade = [t for t, c in classified
                      if c["evidence_class"] == str(STRATEGY_GRADE)]
    body = classify_evidence(countable)
    return {
        "current_cohort": current_cohort,
        # What this body of evidence is allowed to prove. Carried into the
        # readiness report so a performance gate cannot be satisfied by
        # delayed-data fills or by a session with no single runtime.
        "evidence_class": body["evidence_class"],
        "data_quality": body["data_quality"],
        "counts_toward_strategy_gates": body["counts_toward_strategy_gates"],
        "evidence_class_reasons": body["reasons"],
        "strategy_grade_sessions": len(strategy_grade),
        "sessions_voided_by_redeploy": [
            {"session_date": t.session_date, "code_shas": c["code_shas"]}
            for t, c in void],
        "cohorts_seen": sorted({t.cohort for t in tallies if t.cohort}),
        "sessions_in_other_cohorts": len(tallies) - len(mine),
        "sessions_completed": len(countable),
        "live_data_path_exercised": any(
            t.live_cycles_with_real_quotes > 0 for t in mine),
        "reconciliation_clean_sessions": sum(
            1 for t in finalized if t.reconciliation_failures == 0
            and t.emergency_stops == 0),
        "zero_trade_days": sum(1 for t in finalized if t.zero_trade_day),
        "paper_trades": sum(t.trades_journalled for t in mine),
        "hypotheses": sum(t.hypotheses for t in mine),
        "risk_approvals": sum(t.risk_approvals for t in mine),
        "risk_rejections": sum(sum(t.rejections_by_code.values())
                               for t in mine),
        "duplicate_attempts": sum(t.duplicate_attempts for t in mine),
        "eod_flatten_failures": sum(t.eod_flatten_failures for t in mine),
        "stale_data_blocks": sum(t.quotes_stale for t in mine),
        "provider_failures": sum(t.quotes_missing for t in mine),
        "emergency_stops": sum(t.emergency_stops for t in mine),
        "risk_locks": sum(t.risk_locks for t in mine),
        "reconciliation_failures": sum(t.reconciliation_failures
                                       for t in mine),
        "live_market_cycles": sum(t.cycles_live_market for t in mine),
    }
