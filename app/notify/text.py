"""Тексты уведомлений — одно место, чтобы Telegram и почта говорили одинаково."""
from __future__ import annotations

import re
from html import escape as _html_escape
from zoneinfo import ZoneInfo

from ..models import Incident, Status

# Telegram превращает «/слово» в кликабельную команду бота (например «/var» в пути диска).
# Невидимый word joiner после слэша ломает автоссылку, а текст выглядит так же.
# «;» — конец HTML-сущности вроде &quot; после экранирования кавычек.
_CMD_RE = re.compile(r"(^|[\s(\[«;])/(?=[A-Za-z0-9_])")


def escape(text: str) -> str:
    return _CMD_RE.sub(lambda m: m.group(1) + "/\u2060", _html_escape(text))

SEVERITY = {"critical": "🔴 Critical", "warning": "🟠 Warning", "info": "🔵 Info"}
STATUS = {
    Status.TRIGGERED: "🚨 Ждёт реакции",
    Status.ACKNOWLEDGED: "👀 В работе",
    Status.RESOLVED: "✅ Решён",
}


def fmt_time(dt, tz: str = "Europe/Moscow") -> str:
    if dt is None:
        return "—"
    from datetime import UTC

    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).strftime("%d.%m %H:%M")


def incident_url(public_url: str, incident: Incident) -> str:
    return f"{public_url.rstrip('/')}/incidents/{incident.id}"


def telegram_html(incident: Incident, service_name: str, public_url: str, reason: str | None = None) -> str:
    lines = []
    if reason and incident.status == Status.TRIGGERED:
        lines.append(f"<b>{escape(reason)}</b>")
    lines.append(f"<b>#{incident.id} {escape(incident.title)}</b>")
    lines.append(f"{SEVERITY.get(incident.severity, incident.severity)} · {escape(service_name)} · {incident.source}")
    if incident.details:
        details = incident.details if len(incident.details) <= 700 else incident.details[:700] + "…"
        lines.append(f"<blockquote>{escape(details)}</blockquote>")
    status = STATUS.get(incident.status, incident.status)
    if incident.status == Status.ACKNOWLEDGED:
        status += f": {escape(incident.acknowledged_by or '')}"
    elif incident.status == Status.RESOLVED:
        status += f": {escape(incident.resolved_by or '')}"
    lines.append(status)
    if incident.alert_count > 1:
        lines.append(f"Повторов алерта: {incident.alert_count}")
    lines.append(f'<a href="{_html_escape(incident_url(public_url, incident))}">Открыть в панели</a>')
    return "\n".join(lines)


def email_subject(incident: Incident, service_name: str) -> str:
    tag = {Status.TRIGGERED: "ИНЦИДЕНТ", Status.ACKNOWLEDGED: "В РАБОТЕ", Status.RESOLVED: "РЕШЁН"}[incident.status]
    return f"[{tag}] #{incident.id} {incident.title} — {service_name}"


def email_body(incident: Incident, service_name: str, public_url: str, reason: str | None = None) -> str:
    parts = []
    if reason and incident.status == Status.TRIGGERED:
        parts.append(reason)
    parts += [
        f"#{incident.id} {incident.title}",
        f"Сервис: {service_name}",
        f"Важность: {incident.severity}, источник: {incident.source}",
        f"Статус: {STATUS.get(incident.status, incident.status)}",
    ]
    if incident.acknowledged_by:
        parts.append(f"Взял: {incident.acknowledged_by}")
    if incident.resolved_by:
        parts.append(f"Решил: {incident.resolved_by}")
    if incident.details:
        parts += ["", incident.details]
    parts += ["", f"Подтвердить или закрыть: {incident_url(public_url, incident)}"]
    return "\n".join(parts)
