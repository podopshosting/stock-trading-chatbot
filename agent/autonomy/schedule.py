"""
When the next cycle will run.

The cycle fires at minutes 2, 7, 12, ... of UTC hours 13-21 on weekdays.
Two minutes after the scanner, so the cycle reads the freshest scan. The
window covers the US session in both EDT and EST; outside market hours
the cycle resolves its own phase and does nothing, so the window can be
wider than the session without trading outside it.

This only DESCRIBES the schedule for the dashboard. The authoritative
schedule is the EventBridge rule, and a test pins the two together so
this cannot silently drift from it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

CYCLE_MINUTE_OFFSET = 2
CYCLE_MINUTE_STEP = 5
FIRST_UTC_HOUR = 13
LAST_UTC_HOUR = 21
CRON = (f"cron({CYCLE_MINUTE_OFFSET}/{CYCLE_MINUTE_STEP} "
        f"{FIRST_UTC_HOUR}-{LAST_UTC_HOUR} ? * MON-FRI *)")


def next_cycle_time(now: Optional[datetime] = None) -> datetime:
    """The next scheduled invocation strictly after `now` (UTC)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Bounded: at most a week of minutes, so a bug cannot spin forever.
    for _ in range(7 * 24 * 60):
        if (candidate.weekday() < 5
                and FIRST_UTC_HOUR <= candidate.hour <= LAST_UTC_HOUR
                and candidate.minute % CYCLE_MINUTE_STEP
                == CYCLE_MINUTE_OFFSET % CYCLE_MINUTE_STEP):
            return candidate
        candidate += timedelta(minutes=1)
    raise RuntimeError("no scheduled cycle found within a week")
