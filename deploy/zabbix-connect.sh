#!/usr/bin/env bash
# Демо-стенд: подключить Zabbix из compose к «Дежурке» одной командой.
#   sudo bash deploy/zabbix-connect.sh
# Спросит пароль пользователя Admin в Zabbix (скрытый ввод). Повторный запуск безопасен.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

KEY="$(grep '^DEZHURKA_KEY_DEMO=' .env | cut -d= -f2-)"
[[ -n "$KEY" ]] || { echo "В .env нет DEZHURKA_KEY_DEMO — сначала запустите deploy/setup.sh --zabbix" >&2; exit 1; }
command -v python3 >/dev/null || apt-get install -y -qq python3 >/dev/null

# Вебхук вызывает zabbix-server изнутри сети compose, поэтому адрес — имя сервиса dezhurka
python3 contrib/zabbix/setup_zabbix.py \
  --zabbix-url http://127.0.0.1:8080 \
  --webhook-url "http://dezhurka:8000/api/v1/ingest/${KEY}/zabbix" \
  --fix-demo-agent "$@"
