"""Clock abstractions for fakoli-state.

Provides a Protocol so lease and heartbeat logic never calls time.time() directly,
making deterministic unit tests possible without sleep().
"""

from __future__ import annotations

import datetime
from datetime import timedelta
from typing import Protocol


class Clock(Protocol):
    """Wall-clock abstraction so lease/heartbeat tests are deterministic."""

    def now(self) -> datetime.datetime: ...


class SystemClock:
    """Production impl: returns timezone-aware UTC now."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)


class FrozenClock:
    """Test impl: returns a fixed time; tests can advance it explicitly."""

    def __init__(self, start: datetime.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError(
                "FrozenClock requires a timezone-aware datetime; got a naive datetime. "
                "Pass e.g. datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)."
            )
        self._current: datetime.datetime = start

    def now(self) -> datetime.datetime:
        return self._current

    def advance(
        self,
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
    ) -> None:
        """Move the frozen clock forward by the given duration."""
        self._current += timedelta(seconds=seconds, minutes=minutes, hours=hours)
