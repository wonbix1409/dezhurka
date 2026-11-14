#!/usr/bin/env python3
"""Собирает конфиг Xray для контейнера tg-proxy из ссылок vless:// (по одной на строку).

    python3 deploy/tg-proxy/make-config.py tg-proxy.links > tg-proxy.json

Что получается:
  * SOCKS на порту 1080 — виден только контейнерам стека (наружу порт не публикуется);
  * через узлы пропускаются только домены Telegram, всё остальное отбрасывается —
    этим прокси нельзя воспользоваться ни для чего, кроме Bot API;
  * несколько ссылок → Xray сам выбирает живой и самый быстрый узел (leastPing),
    блокировка одного узла Telegram-уведомления не ломает.
Поддерживаются VLESS + Reality, транспорт tcp (с flow xtls-rprx-vision) и xhttp.
Ссылки и готовый конфиг содержат ключи доступа: chmod 600, в git не класть.
"""
import json
import sys
from urllib.parse import parse_qs, unquote, urlparse

TG_DOMAINS = ["domain:telegram.org", "domain:t.me", "domain:telegram.me"]


def outbound(link: str, tag: str) -> dict:
    u = urlparse(link.strip())
    if u.scheme != "vless" or not u.username or not u.hostname:
        raise ValueError("нужна ссылка вида vless://UUID@адрес:порт?...")
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if q.get("security") != "reality":
        raise ValueError("поддерживается только security=reality")
    net = q.get("type", "tcp")
    user = {"id": unquote(u.username), "encryption": q.get("encryption", "none")}
    if q.get("flow"):
        user["flow"] = q["flow"]
    stream = {
        "network": net,
        "security": "reality",
        "realitySettings": {
            "serverName": q.get("sni", ""),
            "fingerprint": q.get("fp", "chrome"),
            "publicKey": q.get("pbk", ""),
            "shortId": q.get("sid", ""),
            "spiderX": unquote(q.get("spx", "/")),
        },
    }
    if net == "xhttp":
        stream["xhttpSettings"] = {"path": unquote(q.get("path", "/")), "mode": q.get("mode", "auto")}
        if q.get("host"):
            stream["xhttpSettings"]["host"] = q["host"]
    elif net != "tcp":
        raise ValueError(f"транспорт {net} не поддерживается (нужен tcp или xhttp)")
    for k in ("serverName", "publicKey"):
        if not stream["realitySettings"][k]:
            raise ValueError(f"в ссылке нет {'sni' if k == 'serverName' else 'pbk'}")
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443, "users": [user]}]},
        "streamSettings": stream,
    }


def main():
    src = open(sys.argv[1], encoding="utf-8") if len(sys.argv) > 1 else sys.stdin
    links = [l.strip() for l in src if l.strip() and not l.lstrip().startswith("#")]
    if not links:
        sys.exit("Нет ни одной ссылки vless://")
    nodes = []
    for i, link in enumerate(links, 1):
        name = unquote(urlparse(link).fragment) or f"{i}"
        try:
            nodes.append(outbound(link, f"node-{i}"))
        except ValueError as ex:
            sys.exit(f"Ссылка {i} ({name}): {ex}")
        print(f"узел node-{i}: {name} → {nodes[-1]['settings']['vnext'][0]['address']}", file=sys.stderr)
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "socks", "listen": "0.0.0.0", "port": 1080, "protocol": "socks",
                      "settings": {"auth": "noauth", "udp": False}}],
        "outbounds": nodes + [{"tag": "block", "protocol": "blackhole"}],
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": [{"tag": "tg", "selector": ["node-"], "strategy": {"type": "leastPing"}}],
            "rules": [
                {"type": "field", "inboundTag": ["socks"], "domain": TG_DOMAINS, "balancerTag": "tg"},
                {"type": "field", "inboundTag": ["socks"], "outboundTag": "block"},
            ],
        },
        "observatory": {"subjectSelector": ["node-"], "probeUrl": "https://www.google.com/generate_204",
                        "probeInterval": "1m", "enableConcurrency": True},
    }
    json.dump(cfg, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
