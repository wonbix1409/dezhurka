"""Вычисление дежурного по графику на заданный момент."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Schedule


def local_to_utc(dt: datetime, tz: str) -> datetime:
    """Время из конфига (без пояса — значит в поясе графика) → naive UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(UTC).replace(tzinfo=None)


def utc_to_local(dt: datetime, tz: str) -> datetime:
    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


@dataclass(frozen=True)
class Shift:
    user_id: str
    start: datetime  # UTC
    end: datetime  # UTC
    is_override: bool = False


@dataclass(frozen=True)
class DbOverride:
    user_id: str
    start: datetime  # UTC
    end: datetime  # UTC


def rotation_shift(schedule: Schedule, at: datetime) -> Shift:
    """Смена ротации, в которую попадает момент at (UTC)."""
    rot = schedule.rotation
    start = local_to_utc(rot.start, schedule.timezone)
    length = timedelta(hours=rot.shift_hours)
    # floor работает и для моментов до начала ротации
    n = math.floor((at - start) / length)
    shift_start = start + n * length
    user = rot.users[n % len(rot.users)]
    return Shift(user, shift_start, shift_start + length)


def _all_overrides(schedule: Schedule, extra: list[DbOverride]) -> list[DbOverride]:
    from_config = [
        DbOverride(o.user, local_to_utc(o.start, schedule.timezone), local_to_utc(o.end, schedule.timezone))
        for o in schedule.overrides
    ]
    # Последняя добавленная подмена побеждает
    return from_config + list(extra)


def on_call(schedule: Schedule, at: datetime, overrides: list[DbOverride] = ()) -> Shift:
    """Кто дежурит в момент at (UTC). Подмены важнее ротации."""
    for o in reversed(_all_overrides(schedule, list(overrides))):
        if o.start <= at < o.end:
            return Shift(o.user_id, o.start, o.end, is_override=True)
    return rotation_shift(schedule, at)


def upcoming(schedule: Schedule, at: datetime, count: int = 4) -> list[Shift]:
    """Ближайшие смены ротации (для панели), подмены показываются отдельно."""
    first = rotation_shift(schedule, at)
    length = first.end - first.start
    rot = schedule.rotation
    start = local_to_utc(rot.start, schedule.timezone)
    n0 = math.floor((first.start - start) / length)
    return [
        Shift(rot.users[(n0 + i) % len(rot.users)], first.start + i * length, first.end + i * length)
        for i in range(count)
    ]
