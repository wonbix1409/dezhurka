"""Тексты уведомлений — одно место, чтобы Telegram и почта говорили одинаково."""
from __future__ import annotations

import re
from html import escape as _html_escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..models import Incident, Status

# Telegram превращает «/слово» в кликабельную команду бота (например «/var» в пути диска).
# Невидимый word joiner после слэша ломает автоссылку, а текст выглядит так же.
# «;» — конец HTML-сущности вроде &quot; после экранирования кавычек.
_CMD_RE = re.compile(r"(^|[\s(\[«;])/(?=[A-Za-z0-9_])")


def escape(text: str) -> str:
    return _CMD_RE.sub(lambda m: m.group(1) + "/\u2060", _html_escape(text))


_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def escape_with_links(text: str) -> str:
    """Как escape, но URL становятся короткой ссылкой «🔗 хост».

    Длинные адреса вроде generatorURL Prometheus Telegram иначе режет на куски
    и подсвечивает обрывки (`g0.tab`) как отдельные домены.
    """
    out, pos = [], 0
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:)»")
        out.append(escape(text[pos : m.start()]))
        host = urlsplit(url).netloc or url
        out.append(f'<a href="{_html_escape(url, quote=True)}">🔗 {_html_escape(host)}</a>')
        pos = m.start() + len(url)
    out.append(escape(text[pos:]))
    return "".join(out)

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
        lines.append(f"<blockquote>{escape_with_links(details)}</blockquote>")
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


def email_body(
    incident: Incident,
    service_name: str,
    public_url: str,
    reason: str | None = None,
    actions: list[tuple[str, str]] | None = None,
) -> str:
    parts = []
    if reason and incident.status == Status.TRIGGERED:
        parts.append(reason)
    parts += [
        f"#{incident.id} {incident.title}",
        f"Сервис: {service_name}",
        f"Важность: {incident.severity}, источник: {incident.source}",
        f"Статус: {STATUS.get(incident.status, incident.status)}",
    ]
    if actions:
        # Ссылки — сразу под статусом, чтобы ночью не листать письмо
        parts.append("")
        parts += [f"{label}: {url}" for label, url in actions]
    if incident.acknowledged_by:
        parts.append(f"Взял: {incident.acknowledged_by}")
    if incident.resolved_by:
        parts.append(f"Решил: {incident.resolved_by}")
    if incident.details:
        parts += ["", incident.details]
    parts += ["", f"Открыть в панели: {incident_url(public_url, incident)}"]
    return "\n".join(parts)


_BTN = {
    # основное действие — тёмная кнопка, второе — контурная
    0: "background:#1f2328;color:#ffffff;border:1px solid #1f2328;",
    1: "background:#ffffff;color:#1a7f37;border:1px solid #1a7f37;",
}


def email_html(
    incident: Incident,
    service_name: str,
    public_url: str,
    reason: str | None = None,
    actions: list[tuple[str, str]] | None = None,
) -> str:
    """HTML-версия письма: кнопки действий и описание инцидента.

    Вёрстка таблицами и инлайн-стилями — так письмо одинаково выглядит в Gmail, Яндексе,
    Outlook и на телефоне.
    """
    e = _html_escape
    head = e(reason) if reason and incident.status == Status.TRIGGERED else ""
    rows = [
        ("Сервис", service_name),
        ("Важность", SEVERITY.get(incident.severity, incident.severity)),
        ("Источник", incident.source),
        ("Статус", STATUS.get(incident.status, incident.status)),
    ]
    if incident.acknowledged_by:
        rows.append(("Взял", incident.acknowledged_by))
    if incident.resolved_by:
        rows.append(("Решил", incident.resolved_by))
    info = "".join(
        f'<tr><td style="padding:3px 12px 3px 0;color:#656d76;white-space:nowrap">{e(k)}</td>'
        f'<td style="padding:3px 0;color:#1f2328">{e(str(v))}</td></tr>'
        for k, v in rows
    )
    buttons = "".join(
        f'<td style="padding:0 10px 0 0"><a href="{e(url, quote=True)}" target="_blank" '
        f'style="{_BTN[min(i, 1)]}display:inline-block;padding:12px 26px;border-radius:8px;'
        f'font-size:16px;font-weight:600;text-decoration:none;font-family:Arial,sans-serif">{e(label)}</a></td>'
        for i, (label, url) in enumerate(actions or [])
    )
    details = (
        f'<div style="margin:16px 0 0;padding:12px 14px;background:#f6f8fa;border-radius:8px;'
        f'font-family:Menlo,Consolas,monospace;font-size:13px;color:#1f2328;white-space:pre-wrap;'
        f'word-break:break-word">{e(incident.details[:2000])}</div>'
        if incident.details
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#f6f8fa">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8fa"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid #d0d7de;border-radius:10px">
<tr><td style="padding:22px 24px;font-family:Arial,sans-serif;font-size:15px;line-height:1.45;color:#1f2328">
{f'<div style="color:#cf222e;font-weight:600;margin:0 0 6px">{head}</div>' if head else ''}
<div style="font-size:19px;font-weight:700;margin:0 0 14px">#{incident.id} {e(incident.title)}</div>
{f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 18px"><tr>{buttons}</tr></table>' if buttons else ''}
<table role="presentation" cellpadding="0" cellspacing="0" style="font-size:14px">{info}</table>
{details}
<div style="margin:18px 0 0;font-size:13px"><a href="{e(incident_url(public_url, incident), quote=True)}" target="_blank" style="color:#0969da;text-decoration:none">Открыть в панели</a></div>
</td></tr></table>
<div style="font-family:Arial,sans-serif;font-size:12px;color:#8c959f;padding:12px">Дежурка</div>
</td></tr></table>
</body></html>"""
