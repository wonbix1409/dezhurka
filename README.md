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
DEZHURKA_CONFIG=config.yaml uvicorn app.main:app_factory --factory --port 8000
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

1. Administration → Media types → Create, тип **Webhook**, скрипт из
   [`contrib/zabbix/dezhurka_webhook.js`](contrib/zabbix/dezhurka_webhook.js), параметры перечислены в его шапке.
2. Создайте пользователя-«получателя» с этим медиа-типом.
3. В Trigger action включите Operations **и** Recovery operations — тогда инцидент закроется сам.

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

Хранилище по умолчанию — SQLite в `./data`. Для Postgres задайте
`DEZHURKA_DATABASE_URL=postgresql+asyncpg://user:pass@host/dezhurka`.

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
