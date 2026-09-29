"""Injectable time source (FR-15). Nothing else in the codebase calls ``datetime.now``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    tz: ZoneInfo

    def now(self) -> datetime:
        return datetime.now(self.tz)


@dataclass(frozen=True)
class FixedClock:
    at: datetime

    def now(self) -> datetime:
        return self.at

    @classmethod
    def on(cls, day: date, tz: str = "Asia/Kolkata", hour: int = 2) -> FixedClock:
        return cls(datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(tz)))
