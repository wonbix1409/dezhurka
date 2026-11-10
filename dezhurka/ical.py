"""Календарь дежурств в формате iCalendar (подписка из Google/Яндекс/Outlook-календаря)."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta

from .config import Config, Schedule
from .schedule import DbOverride, Shift, _all_overrides, on_call


def token(secret: str, schedule_id: str) -> str:
    return hmac.new(secret.encode(), f"ical:{schedule_id}".encode(), hashlib.sha256).hexdigest()[:24]


def shifts(schedule: Schedule, start: datetime, end: datetime, overrides: list[DbOverride]) -> list[Shift]:
    """Смены с учётом подмен на отрезке [start, end) — всё в naive UTC."""
    bounds = sorted({o.start for o in _all_overrides(schedule, overrides)})
    out: list[Shift] = []
    t = start
    while t < end:
        s = on_call(schedule, t, overrides)
        stop = min(s.end, end)
        nxt = next((b for b in bounds if b > t), None)
        if nxt is not None and nxt < stop:
            stop = nxt
        out.append(Shift(s.user_id, t, stop, s.is_override))
        t = stop
    # склеиваем соседние куски одного человека
    merged: list[Shift] = []
    for s in out:
        if merged and merged[-1].user_id == s.user_id and merged[-1].end == s.start:
            p = merged[-1]
            merged[-1] = Shift(p.user_id, p.start, s.end, p.is_override and s.is_override)
        else:
            merged.append(s)
    return merged


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def render(config: Config, schedule: Schedule, overrides: list[DbOverride], now: datetime, days: int = 90) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Dezhurka//schedule//RU",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_esc('Дежурства: ' + (schedule.name or schedule.id))}",
    ]
    for s in shifts(schedule, now - timedelta(days=7), now + timedelta(days=days), overrides):
        user = config.user(s.user_id)
        name = user.name if user else s.user_id
        uid = hashlib.sha1(f"{schedule.id}|{s.user_id}|{s.start.isoformat()}".encode()).hexdigest()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@dezhurka",
            f"DTSTAMP:{_fmt(now)}",
            f"DTSTART:{_fmt(s.start)}",
            f"DTEND:{_fmt(s.end)}",
            f"SUMMARY:{_esc(name + (' (подмена)' if s.is_override else ''))}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
