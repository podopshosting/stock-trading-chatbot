"""
The replay clock, and the lookahead guard.

Everything in a backtest depends on one property: at the moment a
decision is made, the system must not be able to see anything that had
not happened yet. Every other source of backtest error is a rounding
issue by comparison. Lookahead does not make results slightly optimistic
- it makes them arbitrary, because a strategy that can see the next bar
can be made to return any number you like.

So the guard here is not a convention or a code review item. Reaching
past `now` raises `LookaheadError`. A backtest that cannot complete
because it tried to peek is a correct outcome; a backtest that completes
by peeking is worthless and looks identical to a good one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


class LookaheadError(Exception):
    """Something reached for data that had not happened yet.

    Never caught and continued past. If this is raised, the result of
    the run is void, because the amount of contamination is unknown.
    """


class ReplayClock:
    """Authoritative 'now' for a replay.

    Time only ever moves forward, and only when advanced explicitly.
    There is no method that sets it backwards, because a backtest that
    can rewind can also re-decide with knowledge it did not have.
    """

    def __init__(self, start: Optional[str] = None):
        self._index: int = -1
        self._timestamp: Optional[str] = start
        self._advances: int = 0

    @property
    def index(self) -> int:
        """Which bar we are standing on. -1 before the run starts."""
        return self._index

    @property
    def timestamp(self) -> Optional[str]:
        return self._timestamp

    @property
    def started(self) -> bool:
        return self._index >= 0

    @property
    def advances(self) -> int:
        return self._advances

    def advance(self, index: int, timestamp: Optional[str] = None) -> None:
        """Move to a later bar. Refuses to stand still or go back."""
        if index <= self._index:
            raise LookaheadError(
                f"the replay clock cannot move from bar {self._index} to "
                f"{index}; time only moves forward")
        if (timestamp is not None and self._timestamp is not None
                and timestamp < self._timestamp):
            raise LookaheadError(
                f"the replay clock cannot move from {self._timestamp} back "
                f"to {timestamp}")
        self._index = index
        if timestamp is not None:
            self._timestamp = timestamp
        self._advances += 1

    def assert_visible(self, index: int, what: str = "data") -> None:
        """Guard for index-addressed data."""
        if not self.started:
            raise LookaheadError(
                f"{what} was requested before the replay clock started")
        if index > self._index:
            raise LookaheadError(
                f"{what} at bar {index} is in the future; the clock is at "
                f"bar {self._index}")

    def assert_not_future(self, timestamp: Optional[str],
                          what: str = "data") -> None:
        """Guard for time-addressed data such as news and filings.

        A timestamp that cannot be parsed or is missing is treated as
        NOT visible. An item whose publication time is unknown might
        have been published after the decision, and assuming otherwise
        is exactly the mistake this class exists to prevent.
        """
        if not self.started:
            raise LookaheadError(
                f"{what} was requested before the replay clock started")
        if self._timestamp is None:
            return
        if timestamp is None:
            raise LookaheadError(
                f"{what} has no timestamp, so it cannot be shown to be "
                "visible at the decision time")
        if timestamp > self._timestamp:
            raise LookaheadError(
                f"{what} is dated {timestamp}, after the clock at "
                f"{self._timestamp}")

    def is_visible(self, timestamp: Optional[str]) -> bool:
        """Non-raising form, for filtering rather than asserting."""
        if timestamp is None:
            return False
        if self._timestamp is None:
            return True
        return timestamp <= self._timestamp

    def __repr__(self) -> str:
        return (f"ReplayClock(index={self._index}, "
                f"timestamp={self._timestamp!r})")
