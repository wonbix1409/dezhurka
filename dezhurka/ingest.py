"""Разбор входящих вебхуков в единый формат алерта.

Поддерживаются:
  * Prometheus Alertmanager (webhook_config, version 4) — и Grafana Alerting,
    которая шлёт совместимый формат;
  * Zabbix (медиа-тип Webhook, скрипт в contrib/zabbix);
  * произвольный JSON (generic) — для своих скриптов и cron-задач.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

TRIGGER = "trigger"
RESOLVE = "resolve"

_SEVERITY_ALIASES = {
    # Zabbix
    "disaster": "critical",
    "high": "critical",
    "average": "warning",
    "warning": "warning",
    "information": "info",
    "not classified": "info",
    # Prometheus-конвенции
    "critical": "critical",
    "page": "critical",
    "error": "critical",
    "warn": "warning",
    "info": "info",
    "none": "info",
}


def normalize_severity(raw: Any) -> str:
    if raw is None:
        return "critical"
    return _SEVERITY_ALIASES.get(str(raw).strip().lower(), "critical")


@dataclass
class Alert:
    action: str  # trigger | resolve
    dedup_key: str
    title: str
    details: str = ""
    severity: str = "critical"
    source: str = "generic"
    labels: dict[str, str] = field(default_factory=dict)


class IngestError(ValueError):
    pass


def _labels_key(labels: dict[str, Any]) -> str:
    blob = json.dumps(labels, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def parse_alertmanager(payload: dict[str, Any]) -> list[Alert]:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise IngestError("ожидался объект Alertmanager с непустым списком alerts")
    out: list[Alert] = []
    for a in alerts:
        labels = {str(k): str(v) for k, v in (a.get("labels") or {}).items()}
        ann = {str(k): str(v) for k, v in (a.get("annotations") or {}).items()}
        key = a.get("fingerprint") or _labels_key(labels)
        name = labels.get("alertname", "Alert")
        title = ann.get("summary") or name
        where = labels.get("instance") or labels.get("job") or ""
        if where and where not in title:
            title = f"{title} ({where})"
        details = ann.get("description", "")
        if a.get("generatorURL"):
            details = (details + "\n" + a["generatorURL"]).strip()
        out.append(
            Alert(
                action=RESOLVE if a.get("status") == "resolved" else TRIGGER,
                dedup_key=f"am:{key}",
                title=title,
                details=details,
                severity=normalize_severity(labels.get("severity")),
                source="alertmanager",
                labels=labels,
            )
        )
    return out


def parse_zabbix(payload: dict[str, Any]) -> list[Alert]:
    event_id = payload.get("event_id")
    if not event_id:
        raise IngestError("Zabbix: нет поля event_id (проверьте скрипт медиа-типа)")
    status = str(payload.get("status", "PROBLEM")).upper()
    value = str(payload.get("event_value", "1"))
    resolved = status in {"RESOLVED", "OK"} or value == "0"
    host = payload.get("host") or ""
    trigger = payload.get("trigger_name") or payload.get("subject") or "Zabbix problem"
    title = f"{trigger} ({host})" if host and host not in trigger else trigger
    labels = {k: str(payload[k]) for k in ("host", "trigger_id", "event_id") if payload.get(k)}
    return [
        Alert(
            action=RESOLVE if resolved else TRIGGER,
            dedup_key=f"zbx:{event_id}",
            title=title,
            details=str(payload.get("message") or ""),
            severity=normalize_severity(payload.get("severity")),
            source="zabbix",
            labels=labels,
        )
    ]


def parse_generic(payload: dict[str, Any]) -> list[Alert]:
    title = payload.get("title")
    if not title:
        raise IngestError("generic: обязательное поле title")
    action = str(payload.get("action") or payload.get("status") or TRIGGER).lower()
    if action in {"resolve", "resolved", "ok"}:
        action = RESOLVE
    elif action in {"trigger", "firing", "problem"}:
        action = TRIGGER
    else:
        raise IngestError("generic: action должен быть trigger или resolve")
    key = payload.get("dedup_key") or "t:" + hashlib.sha256(str(title).encode()).hexdigest()[:16]
    return [
        Alert(
            action=action,
            dedup_key=f"gen:{key}",
            title=str(title)[:500],
            details=str(payload.get("details") or ""),
            severity=normalize_severity(payload.get("severity")),
            source="generic",
            labels={str(k): str(v) for k, v in (payload.get("labels") or {}).items()},
        )
    ]


PARSERS = {
    "alertmanager": parse_alertmanager,
    "grafana": parse_alertmanager,
    "zabbix": parse_zabbix,
    "generic": parse_generic,
}
