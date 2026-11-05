import pytest

from dezhurka.ingest import RESOLVE, TRIGGER, IngestError, parse_alertmanager, parse_generic, parse_zabbix

AM = {
    "version": "4",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighErrorRate", "severity": "critical", "instance": "api-1:8080"},
            "annotations": {"summary": "5xx > 5%", "description": "Ошибок больше 5% за 5 минут"},
            "fingerprint": "abc123",
            "generatorURL": "http://prom/graph",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "DiskFull", "severity": "warning"},
            "annotations": {},
        },
    ],
}


def test_alertmanager():
    a, b = parse_alertmanager(AM)
    assert a.action == TRIGGER and a.dedup_key == "am:abc123"
    assert a.title == "5xx > 5% (api-1:8080)"
    assert "http://prom/graph" in a.details
    assert b.action == RESOLVE and b.severity == "warning"
    assert b.dedup_key.startswith("am:")  # без fingerprint — хэш меток
    assert parse_alertmanager({"alerts": [AM["alerts"][1]]})[0].dedup_key == b.dedup_key


def test_alertmanager_empty():
    with pytest.raises(IngestError):
        parse_alertmanager({"alerts": []})


def test_zabbix_problem_and_recovery():
    p = parse_zabbix({"event_id": "777", "status": "PROBLEM", "severity": "High", "host": "db-1", "trigger_name": "MySQL down"})
    r = parse_zabbix({"event_id": "777", "status": "RESOLVED", "event_value": "0"})
    assert p[0].action == TRIGGER and p[0].severity == "critical"
    assert p[0].title == "MySQL down (db-1)"
    assert r[0].action == RESOLVE and r[0].dedup_key == p[0].dedup_key


def test_generic():
    a = parse_generic({"title": "Бэкап не прошёл", "dedup_key": "backup", "severity": "warning"})[0]
    assert a.action == TRIGGER and a.dedup_key == "gen:backup" and a.severity == "warning"
    assert parse_generic({"title": "x", "dedup_key": "backup", "action": "resolve"})[0].action == RESOLVE
    with pytest.raises(IngestError):
        parse_generic({"dedup_key": "x"})
    with pytest.raises(IngestError):
        parse_generic({"title": "x", "action": "explode"})
