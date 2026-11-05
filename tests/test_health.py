"""Предупреждение, если Telegram недоступен."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from dezhurka.config import Config
from dezhurka.health import ChannelWatchdog
from tests.conftest import CONFIG

NOW = datetime(2026, 11, 5, 3, 0)


class FakeBot:
    failing_since = None
    last_error = ""


class FakeEmail:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, to, subject, body):
        self.sent.append((to, subject, body))


def _cfg(**settings) -> Config:
    return Config.model_validate({**CONFIG, "settings": {**CONFIG["settings"], **settings}})


def test_alert_once_after_threshold_and_on_recovery():
    bot, email = FakeBot(), FakeEmail()
    wd = ChannelWatchdog(_cfg(admin_email="ops@example.com", channel_alert_after_min=10), bot, email)

    bot.failing_since, bot.last_error = NOW, "ConnectTimeout"
    asyncio.run(wd.check(NOW + timedelta(minutes=9)))
    assert email.sent == [] and wd.warnings(NOW + timedelta(minutes=9)) == []  # кратковременный сбой — молчим

    for minutes in (10, 11, 30):  # письмо одно, а не каждую минуту
        asyncio.run(wd.check(NOW + timedelta(minutes=minutes)))
    assert len(email.sent) == 1
    to, subject, body = email.sent[0]
    assert to == "ops@example.com" and "недоступен" in subject and "ConnectTimeout" in body
    warn = wd.warnings(NOW + timedelta(minutes=30))
    assert len(warn) == 1 and "по почте" in warn[0]

    bot.failing_since = None
    asyncio.run(wd.check(NOW + timedelta(minutes=31)))
    asyncio.run(wd.check(NOW + timedelta(minutes=32)))
    assert len(email.sent) == 2 and "снова доступен" in email.sent[1][1]
    assert wd.warnings(NOW + timedelta(minutes=32)) == []


def test_without_email_only_panel_warning():
    bot = FakeBot()
    bot.failing_since = NOW
    wd = ChannelWatchdog(_cfg(), bot, None)
    asyncio.run(wd.check(NOW + timedelta(hours=1)))
    assert "не настроена" in wd.warnings(NOW + timedelta(hours=1))[0]


def test_no_bot_no_warnings():
    wd = ChannelWatchdog(_cfg(), None, FakeEmail())
    asyncio.run(wd.check(NOW))
    assert wd.warnings(NOW) == []


def test_panel_shows_warning(tmp_path):
    import base64

    from fastapi.testclient import TestClient

    from dezhurka.main import create_app
    from dezhurka.models import utcnow

    cfg = _cfg(database_url=f"sqlite+aiosqlite:///{tmp_path}/h.db")
    app = create_app(cfg, start_background=False)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:admin123").decode()}
    with TestClient(app) as c:
        assert "Telegram недоступен" not in c.get("/", headers=auth).text
        bot = FakeBot()
        bot.failing_since = utcnow() - timedelta(hours=1)
        app.state.watchdog.bot = bot
        assert "Telegram недоступен" in c.get("/", headers=auth).text
