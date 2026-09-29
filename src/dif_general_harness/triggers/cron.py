"""Five-field cron (minute hour day-of-month month day-of-week), evaluated in the tenant's
time zone: ``*``, ``*/n``, ``a-b``, ``a-b/n`` and lists. Day-of-week 0 or 7 is Sunday. When
both day fields are restricted, either may match (standard cron).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"bad step in {part!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
            if step_text:
                end = hi
        if not (lo <= start <= end <= hi):
            raise CronError(f"{part!r} is outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        parts = expr.split()
        if len(parts) != 5:
            raise CronError(f"cron {expr!r} needs 5 fields")
        try:
            fields = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES, strict=True)]
        except ValueError as exc:
            raise CronError(f"cron {expr!r}: {exc}") from None
        weekdays = frozenset(d % 7 for d in fields[4])
        return cls(
            fields[0], fields[1], fields[2], fields[3], weekdays, parts[2] == "*", parts[4] == "*"
        )

    def _day_ok(self, dt: datetime) -> bool:
        dom = dt.day in self.days
        dow = (dt.isoweekday() % 7) in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def next_after(self, after: datetime) -> datetime:
        """The first matching minute strictly after ``after`` (aware datetime)."""
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if dt.month not in self.months:
                dt = (dt.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(dt):
                dt = dt.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if dt.hour not in self.hours:
                dt = dt.replace(minute=0) + timedelta(hours=1)
                continue
            if dt.minute not in self.minutes:
                dt += timedelta(minutes=1)
                continue
            return dt
        raise CronError("no matching time within a year")


def next_fire(expr: str, after_ts: float, timezone: str = "UTC") -> float:
    tz = ZoneInfo(timezone)
    return Cron.parse(expr).next_after(datetime.fromtimestamp(after_ts, tz)).timestamp()
