from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.config import Config
from app.db import init_db, make_engine, make_sessionmaker
from app.engine import Engine
from app.notify.base import Delivery

CONFIG = {
    "settings": {"secret_key": "test-secret-key-123456", "admin_password": "admin123"},
    "users": [
        {"id": "ivan", "name": "Иван", "email": "ivan@example.com", "telegram_chat_id": 101},
        {"id": "petr", "name": "Пётр", "email": "petr@example.com", "telegram_chat_id": 102},
        {"id": "anna", "name": "Анна", "email": "anna@example.com"},
        {"id": "lead", "name": "Тимлид", "telegram_chat_id": 199},
    ],
    "schedules": [
        {
            "id": "primary",
            "timezone": "Europe/Moscow",
            "rotation": {"start": "2026-11-02T10:00:00", "shift_hours": 24, "users": ["ivan", "petr", "anna"]},
        }
    ],
    "escalation_policies": [
        {
            "id": "default",
            "repeat": 1,
            "steps": [
                {"targets": [{"schedule": "primary"}], "escalate_after_min": 5},
                {"targets": [{"user": "lead"}], "escalate_after_min": 10},
            ],
        }
    ],
    "services": [
        {
            "id": "api",
            "name": "Публичный API",
            "escalation_policy": "default",
            "integration_key": "api-key-0123456789",
            "team_chat_id": -500,
        }
    ],
}


class FakeDispatcher:
    def __init__(self) -> None:
        self.pages: list[tuple[str, int, str]] = []
        self.team_posts: list[tuple[int, int]] = []
        self.status_changes: list[tuple[int, str, list, list[str]]] = []
        self._msg = 1000

    async def page(self, contact, incident, service_name, reason):
        self.pages.append((contact.user_id, incident.id, reason))
        if contact.telegram_chat_id:
            self._msg += 1
            return [Delivery("telegram", True, contact.telegram_chat_id, self._msg)]
        if contact.email:
            return [Delivery("email", True)]
        return []

    async def team_post(self, chat_id, incident, service_name):
        self.team_posts.append((chat_id, incident.id))
        self._msg += 1
        return [Delivery("telegram", True, chat_id, self._msg)]

    async def status_changed(self, incident, service_name, messages, contacts):
        self.status_changes.append((incident.id, incident.status, messages, [c.user_id for c in contacts]))


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        from datetime import timedelta

        self.at += timedelta(**kw)


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG)


@pytest.fixture
def env(config, tmp_path):
    """Движок на временной SQLite, фейковые уведомления и управляемые часы."""
    engine_db = make_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    loop = asyncio.new_event_loop()
    loop.run_until_complete(init_db(engine_db))
    clock = Clock(datetime(2026, 11, 4, 12, 0))  # 15:00 МСК, среда
    disp = FakeDispatcher()
    eng = Engine(config, make_sessionmaker(engine_db), disp, clock=clock)
    yield eng, disp, clock, loop.run_until_complete
    loop.run_until_complete(engine_db.dispose())
    loop.close()
