"""Контроль каналов уведомлений.

Если Telegram недоступен дольше settings.channel_alert_after_min, это видно в панели и
администратору уходит письмо (settings.admin_email), а после восстановления — второе.
Иначе о молчащем боте узнают только после пропущенной ночной аварии.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from .config import Config
from .notify.email import EmailSender
from .notify.text import fmt_time

log = logging.getLogger("dezhurka.health")


class ChannelWatchdog:
    def __init__(self, config: Config, bot, email: EmailSender | None) -> None:
        self.config = config
        self.bot = bot
        self.email = email if email and email.enabled else None
        self.alerted = False

    @property
    def _threshold(self) -> timedelta:
        return timedelta(minutes=self.config.settings.channel_alert_after_min)

    def _down_since(self, now: datetime) -> datetime | None:
        since = getattr(self.bot, "failing_since", None) if self.bot else None
        if since is not None and now - since >= self._threshold:
            return since
        return None

    def warnings(self, now: datetime) -> list[str]:
        since = self._down_since(now)
        if since is None:
            return []
        text = (
            f"Telegram недоступен с {fmt_time(since, self.config.settings.timezone)}: "
            "уведомления в Telegram сейчас не доходят."
        )
        text += " Дежурных зовём по почте." if self.email else " Почта не настроена — дежурных сейчас не зовут никак."
        return [text]

    async def _notify(self, subject: str, body: str) -> None:
        to = self.config.settings.admin_email
        if self.email and to:
            err = await self.email.send(to, subject, body)
            if err:
                log.error("не удалось предупредить администратора (%s): %s", to, err)

    async def check(self, now: datetime) -> None:
        since = self._down_since(now)
        if since is not None and not self.alerted:
            self.alerted = True
            err = getattr(self.bot, "last_error", "") or "нет ответа"
            log.error("Telegram недоступен больше %s мин: %s", self.config.settings.channel_alert_after_min, err)
            await self._notify(
                "Дежурка: Telegram недоступен",
                "\n".join(self.warnings(now))
                + f"\n\nПоследняя ошибка: {err}\n"
                "Проверьте доступ сервера к api.telegram.org или настройку telegram.proxy.",
            )
        elif since is None and self.alerted and getattr(self.bot, "failing_since", None) is None:
            self.alerted = False
            log.info("Telegram снова доступен")
            await self._notify("Дежурка: Telegram снова доступен", "Связь с Telegram восстановлена.")


async def watchdog_loop(watchdog: ChannelWatchdog, now, every: float = 60) -> None:
    while True:
        try:
            await watchdog.check(now())
        except Exception:
            log.exception("ошибка проверки каналов")
        await asyncio.sleep(every)
