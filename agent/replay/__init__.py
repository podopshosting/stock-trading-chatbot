"""
Historical replay.

Two properties make a backtest worth reading, and both are structural
here rather than conventional:

1. The strategy cannot see past `now`. Reaching forward raises
   LookaheadError, and a run that raises is reported as VOID rather than
   returned with a caveat. A contaminated backtest looks identical to a
   good one, so it has to be refused rather than annotated.

2. Decisions and fills are separated in time. An order decided on bar N
   fills at bar N+1's open. A backtest that fills at the close it just
   decided on has used the fill price as an input, which makes any
   strategy profitable and the result meaningless.

The loop drives the same signal, hypothesis, risk, position and journal
modules that run live. It does not contain a second, simpler model of
the strategy, because that would measure the model rather than the
system that will actually trade.
"""
from .broker import ReplayBroker
from .clock import LookaheadError, ReplayClock
from .data import Bar, PointInTimeEvidence, PointInTimeSeries
from .engine import (
    CONFIG_VERSION, DEFAULT_WARMUP_BARS, ReplayConfig, ReplayResult, run,
)

__all__ = [
    "CONFIG_VERSION", "DEFAULT_WARMUP_BARS", "Bar", "LookaheadError",
    "PointInTimeEvidence", "PointInTimeSeries", "ReplayBroker",
    "ReplayClock", "ReplayConfig", "ReplayResult", "run",
]
