"""Загрузка и проверка конфигурации из YAML.

Вся «статичная» настройка (люди, графики, эскалации, сервисы) живёт в одном
файле config.yaml — его удобно хранить в git рядом с остальной инфраструктурой.
Динамика (инциденты, подмены дежурств, привязки Telegram) хранится в БД.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    """Подставляет ${VAR} и ${VAR:-default} из окружения, чтобы секреты не лежали в git."""

    def repl(m: re.Match) -> str:
        value = os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        # Пустая подстановка в YAML превратилась бы в null — оставляем пустую строку
        return value if value else '""'

    return _ENV_RE.sub(repl, text)


class User(BaseModel):
    id: str
    name: str
    email: str | None = None
    telegram_chat_id: int | None = None  # можно задать сразу, иначе привязка через /start


class Override(BaseModel):
    user: str
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _order(self) -> "Override":
        if self.end <= self.start:
            raise ValueError("override: end должен быть позже start")
        return self


class Rotation(BaseModel):
    start: datetime  # момент начала первой смены, локальное время графика
    shift_hours: float = 168  # длина смены; 168 = неделя, 24 = сутки
    users: list[str] = Field(min_length=1)


class Schedule(BaseModel):
    id: str
    name: str | None = None
    timezone: str = "Europe/Moscow"
    rotation: Rotation
    overrides: list[Override] = []

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"неизвестный часовой пояс: {v}") from e
        return v


class Target(BaseModel):
    """Кого звать на шаге эскалации: конкретного человека или текущего дежурного по графику."""

    user: str | None = None
    schedule: str | None = None

    @model_validator(mode="after")
    def _one(self) -> "Target":
        if bool(self.user) == bool(self.schedule):
            raise ValueError("target: укажите ровно одно из user или schedule")
        return self


class EscalationStep(BaseModel):
    targets: list[Target] = Field(min_length=1)
    escalate_after_min: float = 10  # сколько ждать подтверждения перед следующим шагом


class EscalationPolicy(BaseModel):
    id: str
    repeat: int = 0  # сколько раз прогнать политику по кругу, если никто не взял
    steps: list[EscalationStep] = Field(min_length=1)


class Service(BaseModel):
    id: str
    name: str
    escalation_policy: str
    integration_key: str = Field(min_length=12)  # секрет в URL вебхука
    team_chat_id: int | None = None  # общий чат команды для дублирования инцидентов


class Telegram(BaseModel):
    bot_token: str = ""
    enabled: bool = True
    # Прокси только для Bot API, если api.telegram.org недоступен напрямую:
    # socks5://host:1080, socks5h://… (DNS через прокси), http://user:pass@host:3128
    proxy: str = ""

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^(https?|socks5h?)://[^\s/]+(:\d+)?/?$", v):
            raise ValueError("telegram.proxy: нужен адрес вида socks5://host:port или http://host:port")
        return v


class Smtp(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    starttls: bool = True
    ssl: bool = False


class Settings(BaseModel):
    public_url: str = "http://localhost:8000"
    secret_key: str = Field(min_length=16)
    admin_password: str = Field(min_length=6)
    database_url: str = "sqlite+aiosqlite:///./data/dezhurka.db"
    tick_seconds: float = 10
    timezone: str = "Europe/Moscow"  # в каком поясе показывать время в панели
    # Куда писать, если канал уведомлений (Telegram) недоступен дольше channel_alert_after_min
    admin_email: str = ""
    channel_alert_after_min: int = Field(10, ge=1)
    # Лимит запросов в минуту на один ключ интеграции; 0 — без ограничения
    ingest_rate_per_min: int = Field(600, ge=0)


class Config(BaseModel):
    settings: Settings
    telegram: Telegram = Telegram()
    smtp: Smtp = Smtp()
    users: list[User]
    schedules: list[Schedule] = []
    escalation_policies: list[EscalationPolicy]
    services: list[Service]

    @model_validator(mode="after")
    def _references(self) -> "Config":
        users = {u.id for u in self.users}
        schedules = {s.id for s in self.schedules}
        policies = {p.id for p in self.escalation_policies}
        errors: list[str] = []
        for kind, items in (
            ("users", self.users),
            ("schedules", self.schedules),
            ("escalation_policies", self.escalation_policies),
            ("services", self.services),
        ):
            ids = [i.id for i in items]
            dup = {i for i in ids if ids.count(i) > 1}
            if dup:
                errors.append(f"{kind}: повторяются id {sorted(dup)}")
        for s in self.schedules:
            for u in s.rotation.users:
                if u not in users:
                    errors.append(f"график {s.id}: неизвестный пользователь {u}")
            for o in s.overrides:
                if o.user not in users:
                    errors.append(f"график {s.id}: подмена на неизвестного {o.user}")
        for p in self.escalation_policies:
            for i, step in enumerate(p.steps):
                for t in step.targets:
                    if t.user and t.user not in users:
                        errors.append(f"политика {p.id}, шаг {i + 1}: неизвестный пользователь {t.user}")
                    if t.schedule and t.schedule not in schedules:
                        errors.append(f"политика {p.id}, шаг {i + 1}: неизвестный график {t.schedule}")
        keys = [s.integration_key for s in self.services]
        if len(set(keys)) != len(keys):
            errors.append("services: integration_key должны быть уникальны")
        for s in self.services:
            if s.escalation_policy not in policies:
                errors.append(f"сервис {s.id}: неизвестная политика {s.escalation_policy}")
        if errors:
            raise ValueError("ошибки в конфиге:\n  - " + "\n  - ".join(errors))
        return self

    # Быстрый доступ по id
    def user(self, uid: str) -> User | None:
        return next((u for u in self.users if u.id == uid), None)

    def schedule(self, sid: str) -> Schedule | None:
        return next((s for s in self.schedules if s.id == sid), None)

    def policy(self, pid: str) -> EscalationPolicy | None:
        return next((p for p in self.escalation_policies if p.id == pid), None)

    def service(self, sid: str) -> Service | None:
        return next((s for s in self.services if s.id == sid), None)

    def service_by_key(self, key: str) -> Service | None:
        return next((s for s in self.services if s.integration_key == key), None)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("DEZHURKA_CONFIG", "config.yaml"))
    raw = yaml.safe_load(_expand_env(path.read_text(encoding="utf-8")))
    return Config.model_validate(raw)
