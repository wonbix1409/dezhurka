# Дежурка

Дежурства и эскалации для ИТ-команд поверх того мониторинга, который у вас уже есть.
Alertmanager, Grafana или Zabbix присылают алерт — «Дежурка» знает, кто сейчас дежурит,
зовёт его в Telegram и на почту, а если он не ответил за 5 минут — зовёт следующего.

Замена Opsgenie (Atlassian прекращает его поддержку 5 апреля 2027) и PagerDuty для команд в РФ:
ставится к вам в контур одной командой, работает без публичного HTTPS, хранит данные у вас.

## Что умеет

- **Приём алертов**: Prometheus Alertmanager, Grafana Alerting, Zabbix, произвольный JSON.
- **Дедупликация**: повторяющийся алерт не будит людей снова, а увеличивает счётчик. Когда источник
  присылает «resolved», инцидент закрывается сам.
- **Графики дежурств**: ротация с любой длиной смены (неделя, сутки, 12 часов), часовые пояса,
  подмены прямо в панели.
- **Эскалации**: шаги «дежурный → тимлид → …», время ожидания на каждом шаге, повтор по кругу.
- **Telegram-бот**: личные уведомления и пост в общий чат команды, кнопки «Беру» и «Решено»;
  после нажатия все сообщения по инциденту обновляются. Команды `/oncall` и `/open`.
- **Почта** через ваш SMTP.
- **Панель**: открытые инциденты, хронология, кто дежурит сейчас и дальше, среднее время реакции (MTTA).
- **Конфиг как код**: люди, графики, политики и сервисы — в одном `config.yaml`, удобно держать в git.

Как всё устроено внутри — схемы сети, жизненный цикл инцидента, порты демо-стенда, прокси до Telegram —
в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Быстрый старт

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# заполните .env: секрет, пароль панели, токен бота от @BotFather, ключ интеграции
docker compose up -d --build
```

Панель — `http://localhost:8000` (логин любой, пароль из `DEZHURKA_ADMIN_PASSWORD`).
Документация API — `/api/docs`.

Дальше:

1. Откройте **Люди** и раздайте каждому его личную ссылку на бота — человек нажимает «Старт»,
   и Telegram привязан.
2. Добавьте бота в общий чат команды, напишите там `/start` — бот покажет `chat_id`.
   Впишите его в `team_chat_id` сервиса.
3. Подключите источник алертов (ниже) и отправьте тестовый алерт.

Без Docker:

```bash
pip install -r requirements.txt
DEZHURKA_CONFIG=config.yaml uvicorn dezhurka.main:app_factory --factory --port 8000
```

Запускайте **один процесс** (`--workers 1`): эскалации и бот работают внутри него.

## Демо-стенд на сервере одной командой

На чистом Ubuntu 22.04/24.04 поднимает «Дежурку», Prometheus, Alertmanager, node-exporter и Grafana,
а с флагом `--zabbix` — ещё и Zabbix. Alertmanager сразу отправляет алерты в «Дежурку».

```bash
sudo bash deploy/setup.sh            # без Zabbix: 1 vCPU, 2 ГБ RAM
sudo bash deploy/setup.sh --zabbix   # с Zabbix: 2 vCPU, 4 ГБ RAM
```

Скрипт ставит Docker, генерирует пароли в `.env`, спрашивает токен бота (скрытым вводом), имя
и часовой пояс, а в конце печатает адреса и пароли. Если Docker Hub недоступен, он включает зеркало.

Демо-мониторинг (Prometheus, Alertmanager, node-exporter, Grafana) живёт в профиле `monitoring`,
Zabbix — в профиле `zabbix`. Если он больше не нужен, удалите его контейнеры — `update.sh`
дальше будет поднимать только «Дежурку»:

```bash
C="docker compose -f deploy/docker-compose.stack.yml --env-file .env --profile monitoring --profile zabbix"
$C rm -sf prometheus alertmanager node-exporter grafana zabbix-server zabbix-web zabbix-agent zabbix-db
```

## Telegram через прокси в контейнере

Если api.telegram.org недоступен напрямую, Bot API можно пустить через свой VLESS+Reality-узел.
Xray работает в отдельном контейнере стека (профиль `tg-proxy`), порт наружу не публикуется,
и через узлы пропускаются только домены Telegram — остальное прокси отбрасывает.
Через прокси «Дежурка» ходит только в Bot API; почта и вебхуки идут напрямую.

```bash
nano tg-proxy.links                     # ссылки vless://… из панели, по одной на строку
chmod 600 tg-proxy.links
python3 deploy/tg-proxy/make-config.py tg-proxy.links > tg-proxy.json
chown 65532:65532 tg-proxy.json && chmod 600 tg-proxy.json   # образ Xray работает не от root (uid 65532)
echo 'DEZHURKA_TELEGRAM_PROXY=socks5h://tg-proxy:1080' >> .env   # или замените старую строку
docker compose -f deploy/docker-compose.stack.yml --env-file .env --profile tg-proxy up -d
```

Несколько ссылок — Xray сам выбирает живой узел. `tg-proxy.links` и `tg-proxy.json` содержат ключи
и в git не попадают. `update.sh` сохраняет профиль, если контейнер `tg-proxy` существует.

## Продакшен

**HTTPS.** Telegram-кнопки «Беру» и вебхуки должны ходить по https, поэтому поставьте перед
«Дежуркой» обратный прокси. Пример для Caddy (сертификат выпускается сам):

```
dezhurka.example.ru {
    reverse_proxy 127.0.0.1:8000
}
```

В `.env` укажите `DEZHURKA_PUBLIC_URL=https://dezhurka.example.ru`, а порт 8000 в
`docker-compose.yml` привяжите к loopback: `"127.0.0.1:8000:8000"`.

**Бэкап.** Всё состояние — база в томе `dezhurka-data` плюс ваши `config.yaml` и `.env`.
Копия SQLite без остановки сервиса:

```bash
docker compose exec dezhurka python -c "
import sqlite3
src = sqlite3.connect('data/dezhurka.db')
dst = sqlite3.connect('data/backup.db')
src.backup(dst)"
docker compose cp dezhurka:/app/data/backup.db ./dezhurka-$(date +%F).db
```

Если используете Postgres (`DEZHURKA_DATABASE_URL`), делайте обычный `pg_dump`.
Положите в cron раз в сутки и храните копии вне сервера.

**Обновление.**

```bash
git pull --ff-only
docker compose up -d --build
```

Миграции базы применяются при старте. Перед обновлением сделайте бэкап. На демо-стенде
(`deploy/setup.sh`) то же делает `sudo bash deploy/update.sh`: он не трогает `config.yaml`
и `.env`, а если в отслеживаемых файлах есть локальные правки, останавливается.

**Прочее.** Запускайте один процесс, а пароль панели и секрет из `.env` не коммитьте.
Проверить, что сервис жив: `curl -s https://dezhurka.example.ru/healthz`.

## Подключение источников

URL вебхука: `https://<адрес>/api/v1/ingest/<integration_key>/<тип>`,
где тип — `alertmanager`, `grafana`, `zabbix` или `generic`. `integration_key` — секрет сервиса из конфига.

### Prometheus Alertmanager

```yaml
receivers:
  - name: dezhurka
    webhook_configs:
      - url: https://dezhurka.example.ru/api/v1/ingest/<integration_key>/alertmanager
        send_resolved: true
route:
  receiver: dezhurka
```

### Grafana Alerting

Contact point → тип **Webhook**, URL `.../ingest/<integration_key>/grafana`.

### Zabbix

Одной командой через Zabbix API (Python 3.8+, без зависимостей; проверено на Zabbix 7.0):

```bash
python3 contrib/zabbix/setup_zabbix.py --zabbix-url https://zabbix.example.ru \
  --webhook-url https://dezhurka.example.ru/api/v1/ingest/<integration_key>/zabbix
```

Скрипт создаёт медиа-тип «Дежурка», канал у вашего пользователя и действие с Operations и
Recovery operations (инцидент закроется сам). Повторный запуск обновляет, а не дублирует.

Вручную: Administration → Media types → Create, тип **Webhook**, скрипт
[`contrib/zabbix/dezhurka_webhook.js`](contrib/zabbix/dezhurka_webhook.js) с параметрами из его шапки;
медиа-тип пользователю-получателю; в Trigger action — Operations **и** Recovery operations.

### Свои скрипты и cron

```bash
curl -X POST https://dezhurka.example.ru/api/v1/ingest/<integration_key>/generic \
  -H 'Content-Type: application/json' \
  -d '{"title": "Ночной бэкап не прошёл", "dedup_key": "backup-db1", "severity": "critical", "details": "exit code 2"}'

# закрыть:
curl ... -d '{"title": "Ночной бэкап", "dedup_key": "backup-db1", "action": "resolve"}'
```

## Как устроены эскалации

```yaml
escalation_policies:
  - id: sre
    repeat: 2                       # пройти шаги ещё 2 раза, если никто не взял
    steps:
      - targets: [{schedule: sre-primary}]
        escalate_after_min: 5       # ждём «Беру» 5 минут
      - targets: [{user: lead}, {schedule: sre-secondary}]
        escalate_after_min: 10
```

«Беру» останавливает эскалацию. Если круги закончились, а никто не взял — это видно в хронологии.

## Разработка

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

Хранилище по умолчанию — SQLite в `./data`. Для Postgres укажите в `config.yaml`
`settings.database_url: postgresql+asyncpg://user:pass@host/dezhurka`.

Схема БД обновляется миграциями Alembic автоматически при старте. Ядро ставится как пакет
(`pip install .`) и расширяется модулями через entry points — подробнее в
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#расширения).

## Лицензия

Это не open-source. Код открыт для ознакомления, тестовый запуск на непродуктивном стенде
разрешён на 30 дней. Использование в продакшне и любое распространение — по договору
с правообладателем. Подробности в [LICENSE](LICENSE).

## Дорожная карта

- Звонки и SMS через российских провайдеров.
- Мессенджер MAX.
- Статус-страница для клиентов.
- Отчёты по нагрузке дежурных и постмортемы.
- SSO (LDAP / Keycloak), роли и аудит — для корпоративной версии.
