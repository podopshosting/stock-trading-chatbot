"""
Autonomy: the boundary between what the agent may do alone and what it
may not.

The agent is autonomous in the sense that no individual PAPER trade
needs a human. It is not autonomous about its own limits. It may engage
a protection, never lift one; it may size risk inside a ceiling, never
raise the ceiling; and LIVE execution is not a mode it can be in.
"""
from .alerts import (
    Alert, AlertKind, AlertSink, DynamoDBAlertSink, InMemoryAlertSink,
    Severity,
)
from .daily import daily_counters
from .decisions import DecisionLog, DynamoDBDecisionLog, InMemoryDecisionLog
from .health import (
    HALTING, LATCHING, TOLERATED_FOR_ENTRIES, ActiveCondition, Condition,
    DynamoDBHealthStore, HealthSnapshot, HealthState, HealthStore,
    InMemoryHealthStore,
)
from .policy import (
    FORBIDDEN, PERMITTED, Action, AutonomyPolicy, ExecutionMode,
    LiveExecutionRefused, PolicyViolation, policy_from_environment,
)
from .explain import explain, classify as classify_question, extract_symbol
from .schedule import CRON as CYCLE_CRON, next_cycle_time
from .snapshot import (
    DynamoDBSnapshotStore, InMemorySnapshotStore, SnapshotStore,
)
from .sessions import (
    DynamoDBSessionStore, InMemorySessionStore, SessionStore, SessionTally,
    aggregate_evidence, build_report, finalize_session, record_cycle,
)
from .versions import (
    BEHAVIOURAL_KEYS, cohort_key, current_versions, same_cohort,
    split_by_cohort,
)

__all__ = [n for n in dir() if not n.startswith("_")]
from .inactivity import explain_inactivity  # noqa: F401,E402
