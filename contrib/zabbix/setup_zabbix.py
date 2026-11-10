#!/usr/bin/env python3
"""Подключить Zabbix к «Дежурке» через Zabbix API — вместо ручной настройки в веб-интерфейсе.

Создаёт (или обновляет, если уже есть):
  * медиа-тип «Дежурка» (Webhook) со скриптом dezhurka_webhook.js;
  * канал этого медиа-типа у пользователя, от имени которого вы входите;
  * действие «Дежурка: проблемы» с Operations и Recovery operations —
    новые проблемы открывают инцидент, восстановление его закрывает.

Запуск (нужен только Python 3.8+, без зависимостей):
  python3 setup_zabbix.py --zabbix-url https://zabbix.example.ru \\
      --webhook-url https://dezhurka.example.ru/api/v1/ingest/<integration_key>/zabbix
Пароль спрашивается скрыто или берётся из переменной ZABBIX_PASSWORD.
Проверено на Zabbix 7.0.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MEDIA_NAME = "Дежурка"
ACTION_NAME = "Дежурка: проблемы"
SEVERITIES = {"info": 1, "warning": 2, "average": 3, "high": 4, "disaster": 5}

PARAMS = [
    ("url", None),  # подставляется из --webhook-url
    ("event_id", "{EVENT.ID}"),
    ("event_value", "{EVENT.VALUE}"),
    ("status", "{EVENT.STATUS}"),
    ("severity", "{EVENT.SEVERITY}"),
    ("host", "{HOST.NAME}"),
    ("trigger_id", "{TRIGGER.ID}"),
    ("trigger_name", "{EVENT.NAME}"),
    ("message", "{ALERT.MESSAGE}"),
]

TEMPLATES = [
    {
        "eventsource": 0,
        "recovery": 0,
        "subject": "Проблема: {EVENT.NAME}",
        "message": "Хост: {HOST.NAME}\nНачало: {EVENT.DATE} {EVENT.TIME}\nДанные: {EVENT.OPDATA}",
    },
    {
        "eventsource": 0,
        "recovery": 1,
        "subject": "Решено: {EVENT.NAME}",
        "message": "Хост: {HOST.NAME}\nДлительность: {EVENT.DURATION}",
    },
]


class ZabbixError(RuntimeError):
    pass


class Api:
    def __init__(self, base_url: str) -> None:
        self.url = base_url.rstrip("/") + "/api_jsonrpc.php"
        self.token: str | None = None
        self._id = 0

    def call(self, method: str, params: dict | list) -> object:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json-rpc"})
        if self.token and method != "user.login":
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise ZabbixError(f"не достучался до {self.url}: {e}") from e
        if "error" in data:
            err = data["error"]
            raise ZabbixError(f"{method}: {err.get('message')} {err.get('data', '')}".strip())
        return data["result"]


def ensure_media_type(api: Api, script: str, webhook_url: str) -> str:
    params = [{"name": n, "value": v if v is not None else webhook_url} for n, v in PARAMS]
    fields = {
        "name": MEDIA_NAME,
        "type": 4,  # Webhook
        "script": script,
        "parameters": params,
        "timeout": "10s",
        "status": 0,
        "message_templates": TEMPLATES,
        "description": "Инциденты и эскалации в «Дежурке». Создано contrib/zabbix/setup_zabbix.py",
    }
    found = api.call("mediatype.get", {"output": ["mediatypeid"], "filter": {"name": MEDIA_NAME}})
    if found:
        mid = found[0]["mediatypeid"]
        api.call("mediatype.update", {"mediatypeid": mid, **fields})
        print(f"  медиа-тип «{MEDIA_NAME}» обновлён (id {mid})")
    else:
        mid = api.call("mediatype.create", fields)["mediatypeids"][0]
        print(f"  медиа-тип «{MEDIA_NAME}» создан (id {mid})")
    return mid


def ensure_user_media(api: Api, username: str, mediatypeid: str) -> str:
    users = api.call(
        "user.get", {"output": ["userid"], "filter": {"username": username}, "selectMedias": "extend"}
    )
    if not users:
        raise ZabbixError(f"пользователь {username} не найден")
    user = users[0]
    keep = [
        {k: m[k] for k in ("mediatypeid", "sendto", "active", "severity", "period")}
        for m in user.get("medias", [])
        if m["mediatypeid"] != mediatypeid
    ]
    keep.append(
        {"mediatypeid": mediatypeid, "sendto": "dezhurka", "active": 0, "severity": 63, "period": "1-7,00:00-24:00"}
    )
    api.call("user.update", {"userid": user["userid"], "medias": keep})
    print(f"  пользователю {username} добавлен канал «{MEDIA_NAME}»")
    return user["userid"]


def ensure_action(api: Api, mediatypeid: str, userid: str, min_severity: int) -> None:
    fields = {
        "name": ACTION_NAME,
        "eventsource": 0,  # триггеры
        "status": 0,
        "esc_period": "1h",
        "filter": {
            "evaltype": 0,
            # серьёзность триггера >= min_severity
            "conditions": [{"conditiontype": 4, "operator": 5, "value": str(min_severity)}],
        },
        "operations": [
            {
                "operationtype": 0,  # отправить сообщение
                "esc_step_from": 1,
                "esc_step_to": 1,
                "opmessage": {"default_msg": 1, "mediatypeid": mediatypeid},
                "opmessage_usr": [{"userid": userid}],
            }
        ],
        "recovery_operations": [
            {"operationtype": 11, "opmessage": {"default_msg": 1}},  # уведомить всех участников
        ],
    }
    found = api.call("action.get", {"output": ["actionid"], "filter": {"name": ACTION_NAME}})
    if found:
        aid = found[0]["actionid"]
        fields.pop("eventsource")  # менять нельзя
        api.call("action.update", {"actionid": aid, **fields})
        print(f"  действие «{ACTION_NAME}» обновлено (id {aid})")
    else:
        aid = api.call("action.create", fields)["actionids"][0]
        print(f"  действие «{ACTION_NAME}» создано (id {aid})")


def fix_demo_agent(api: Api, host: str, dns: str) -> None:
    """Демо-стенд: агент живёт в соседнем контейнере, а не на 127.0.0.1 сервера."""
    hosts = api.call("host.get", {"output": ["hostid"], "filter": {"host": host}, "selectInterfaces": "extend"})
    if not hosts:
        print(f"  хост «{host}» не найден — пропускаю")
        return
    for itf in hosts[0]["interfaces"]:
        if itf["type"] == "1" and (itf["useip"] != "0" or itf["dns"] != dns):
            api.call("hostinterface.update", {"interfaceid": itf["interfaceid"], "useip": 0, "dns": dns, "ip": ""})
            print(f"  хост «{host}»: агент теперь опрашивается по имени {dns}")
            return
    print(f"  хост «{host}»: агент уже настроен на {dns}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Подключить Zabbix к «Дежурке»")
    ap.add_argument("--zabbix-url", default="http://127.0.0.1:8080", help="адрес веб-интерфейса Zabbix")
    ap.add_argument("--webhook-url", required=True, help=".../api/v1/ingest/<integration_key>/zabbix")
    ap.add_argument("--user", default="Admin", help="пользователь Zabbix с правами Super admin")
    ap.add_argument("--min-severity", choices=SEVERITIES, default="warning", help="с какой серьёзности звать дежурного")
    ap.add_argument("--fix-demo-agent", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not args.webhook_url.rstrip("/").endswith("/zabbix"):
        print("--webhook-url должен заканчиваться на /zabbix", file=sys.stderr)
        return 2
    script = (Path(__file__).resolve().parent / "dezhurka_webhook.js").read_text(encoding="utf-8")
    password = os.environ.get("ZABBIX_PASSWORD") or getpass.getpass(f"Пароль Zabbix для {args.user}: ")

    api = Api(args.zabbix_url)
    try:
        version = api.call("apiinfo.version", {})
        print(f"Zabbix {version}: {args.zabbix_url}")
        api.token = api.call("user.login", {"username": args.user, "password": password})
        mid = ensure_media_type(api, script, args.webhook_url)
        uid = ensure_user_media(api, args.user, mid)
        ensure_action(api, mid, uid, SEVERITIES[args.min_severity])
        if args.fix_demo_agent:
            fix_demo_agent(api, "Zabbix server", "zabbix-agent")
        api.call("user.logout", {})
    except ZabbixError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    print("Готово: проблемы Zabbix будут приходить в «Дежурку», восстановление закроет инцидент.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
