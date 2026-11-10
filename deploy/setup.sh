#!/usr/bin/env bash
# Установка демо-стенда «Дежурки» на чистый Ubuntu-сервер.
#   sudo bash deploy/setup.sh            — Дежурка + Prometheus + Alertmanager + Grafana
#   sudo bash deploy/setup.sh --zabbix   — то же самое плюс Zabbix
# Повторный запуск безопасен: секреты и конфиг не перезаписываются.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE=(docker compose -f deploy/docker-compose.stack.yml --env-file .env)
WITH_ZABBIX=0
[[ "${1:-}" == "--zabbix" ]] && WITH_ZABBIX=1 && COMPOSE+=(--profile zabbix)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
rand() { openssl rand -hex "$1"; }

if [[ $EUID -ne 0 ]]; then
  echo "Запустите через sudo: sudo bash deploy/setup.sh $*" >&2
  exit 1
fi

# ---------- Docker ----------
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  say "Ставлю Docker…"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl openssl >/dev/null
  curl -fsSL https://get.docker.com | sh
fi
command -v openssl >/dev/null || apt-get install -y -qq openssl >/dev/null

# Демон должен работать до проверки Docker Hub, иначе проверка ложно падает.
# docker.service стартует с -H fd:// и без docker.socket не поднимается.
systemctl unmask docker.socket docker.service >/dev/null 2>&1 || true
systemctl reset-failed docker.service >/dev/null 2>&1 || true
systemctl enable --now docker.socket >/dev/null 2>&1 || true
systemctl enable --now docker.service >/dev/null 2>&1 || true
for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 \
  || { echo "Docker не запустился. Смотрите: journalctl -u docker.service -n 50" >&2; exit 1; }

# Docker Hub ограничивает доступ из РФ — если образы не тянутся, включаем зеркало
if ! timeout 60 docker pull -q hello-world >/dev/null 2>&1; then
  say "Docker Hub недоступен, включаю зеркало mirror.gcr.io"
  mkdir -p /etc/docker
  if [[ -s /etc/docker/daemon.json ]]; then
    cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
    echo "Старый /etc/docker/daemon.json сохранён в daemon.json.bak и заменён." >&2
  fi
  echo '{ "registry-mirrors": ["https://mirror.gcr.io"] }' > /etc/docker/daemon.json
  systemctl restart docker
  timeout 60 docker pull -q hello-world >/dev/null 2>&1 \
    || { echo "Образы всё равно не скачиваются. Пропишите в /etc/docker/daemon.json зеркало вашего хостинга и запустите скрипт снова." >&2; exit 1; }
fi

# ---------- Секреты (.env) ----------
if [[ ! -f .env ]]; then
  say "Создаю .env с секретами"
  IP="$(curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
  read -rsp "Токен Telegram-бота от @BotFather (Enter — пропустить): " TG_TOKEN; echo
  umask 077
  cat > .env <<EOF
DEZHURKA_PUBLIC_URL=http://${IP}:8000
DEZHURKA_SECRET_KEY=$(rand 32)
DEZHURKA_ADMIN_PASSWORD=$(rand 8)
DEZHURKA_TELEGRAM_TOKEN=${TG_TOKEN}
DEZHURKA_KEY_DEMO=$(rand 16)
GRAFANA_ADMIN_PASSWORD=$(rand 8)
ZABBIX_DB_PASSWORD=$(rand 16)
EOF
  umask 022
else
  say ".env уже есть — оставляю как есть"
fi
set -a; source .env; set +a

# ---------- Конфиг ----------
if [[ ! -f config.yaml ]]; then
  read -rp "Ваше имя для графика дежурств [Дежурный]: " USER_NAME
  USER_NAME="${USER_NAME:-Дежурный}"
  while :; do
    read -rp "Часовой пояс IANA, например Europe/Moscow или Asia/Yekaterinburg [Europe/Moscow]: " TZ_NAME
    TZ_NAME="${TZ_NAME:-Europe/Moscow}"
    TZ_NAME="$(printf '%s' "$TZ_NAME" | tr -cd 'A-Za-z0-9_/+-')"
    # «UTC+5» и подобное не годится: нужен ключ из базы часовых поясов
    [[ -n "$TZ_NAME" && "$TZ_NAME" != *..* && -f "/usr/share/zoneinfo/$TZ_NAME" ]] && break
    echo "Не знаю часовой пояс «$TZ_NAME». Нужен формат Регион/Город, список: timedatectl list-timezones" >&2
  done
  # Убираем символы, которые сломали бы YAML или sed; имя берём в кавычки
  USER_NAME="$(printf '%s' "$USER_NAME" | tr -d '"\\|&')"
  sed -e "s|__USER_NAME__|\"${USER_NAME}\"|" -e "s|__TIMEZONE__|${TZ_NAME}|g" deploy/config.demo.yaml > config.yaml
fi
# Часовой пояс стенда для Zabbix (время в сообщениях) — берём из config.yaml
if ! grep -q '^STACK_TZ=' .env; then
  STACK_TZ="$(sed -n 's/^[[:space:]]*timezone:[[:space:]]*\([A-Za-z0-9_/+-]*\).*/\1/p' config.yaml | head -1)"
  echo "STACK_TZ=${STACK_TZ:-Europe/Moscow}" >> .env
fi

sed "s|__INTEGRATION_KEY__|${DEZHURKA_KEY_DEMO}|" deploy/alertmanager/alertmanager.yml.template \
  > deploy/alertmanager/alertmanager.yml
chmod 644 deploy/alertmanager/alertmanager.yml

# ---------- Запуск ----------
say "Собираю и запускаю контейнеры (первый раз — несколько минут)…"
"${COMPOSE[@]}" up -d --build

say "Жду, пока «Дежурка» поднимется…"
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break
  sleep 2
done
if ! curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  echo "Дежурка не отвечает. Логи: ${COMPOSE[*]} logs dezhurka" >&2
  exit 1
fi

cat <<EOF

========================================================================
 Готово.

 Дежурка     ${DEZHURKA_PUBLIC_URL}
             логин любой, пароль: ${DEZHURKA_ADMIN_PASSWORD}
 Grafana     http://<ip>:3000   admin / ${GRAFANA_ADMIN_PASSWORD}
EOF
if [[ $WITH_ZABBIX -eq 1 ]]; then
cat <<EOF
 Zabbix      http://<ip>:8080   Admin / zabbix  ← СРАЗУ смените пароль
             подключить к Дежурке (медиа-тип, получатель, действие):
             sudo bash deploy/zabbix-connect.sh
EOF
fi
cat <<EOF
 Prometheus и Alertmanager открыты только локально. С вашего компьютера:
             ssh -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 <user>@<ip>

 Дальше:
  1. Панель → «Люди» → откройте свою ссылку на бота и нажмите «Старт».
  2. Тестовый алерт:
     curl -X POST http://127.0.0.1:8000/api/v1/ingest/${DEZHURKA_KEY_DEMO}/generic \\
       -H 'Content-Type: application/json' -d '{"title":"Тест Дежурки","dedup_key":"test-1"}'

 Пароли хранятся в ${ROOT}/.env
========================================================================
EOF
