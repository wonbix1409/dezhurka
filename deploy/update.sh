#!/usr/bin/env bash
# Обновить стенд: подтянуть код из GitHub и пересобрать/перезапустить контейнеры.
#   sudo bash deploy/update.sh
# config.yaml и .env не трогаются (они в .gitignore).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Тот же набор сервисов, что поднял setup.sh: Zabbix, если его контейнеры есть
PROFILE=()
docker ps -a --format '{{.Names}}' | grep -q 'zabbix-server' && PROFILE=(--profile zabbix)
COMPOSE=(docker compose -f deploy/docker-compose.stack.yml --env-file .env "${PROFILE[@]}")

# Репозиторий клонирован обычным пользователем, а скрипт идёт через sudo
OWNER="$(stat -c %U .)"
as_owner() { if [[ "$(id -un)" == "$OWNER" ]]; then "$@"; else sudo -u "$OWNER" "$@"; fi; }

if [[ -n "$(as_owner git status --porcelain --untracked-files=no)" ]]; then
  echo "В отслеживаемых файлах есть локальные правки — обновление остановлено:" >&2
  as_owner git status --short --untracked-files=no >&2
  echo "Сбросить их: git checkout -- <файл>  (config.yaml и .env это не затронет)" >&2
  exit 1
fi

BEFORE="$(as_owner git rev-parse --short HEAD)"
as_owner git pull --ff-only
AFTER="$(as_owner git rev-parse --short HEAD)"

if [[ "$BEFORE" == "$AFTER" ]]; then
  echo "Новых коммитов нет ($AFTER). Перезапускаю на всякий случай."
else
  echo "Обновлено: $BEFORE → $AFTER"
  as_owner git log --format='  %h %s' "$BEFORE..$AFTER"
fi

"${COMPOSE[@]}" up -d --build --remove-orphans
sleep 5
"${COMPOSE[@]}" ps
