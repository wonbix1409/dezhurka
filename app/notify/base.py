"""Общий интерфейс каналов уведомлений."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import Incident


@dataclass(frozen=True)
class Contact:
    user_id: str
    name: str
    email: str | None = None
    telegram_chat_id: int | None = None


@dataclass(frozen=True)
class Delivery:
    """Что реально ушло: канал и (для Telegram) куда, чтобы потом обновить сообщение."""

    channel: str
    ok: bool
    chat_id: int | None = None
    message_id: int | None = None
    error: str = ""


class Dispatcher(Protocol):
    async def page(self, contact: Contact, incident: Incident, service_name: str, reason: str) -> list[Delivery]:
        """Позвать человека на инцидент."""

    async def team_post(self, chat_id: int, incident: Incident, service_name: str) -> list[Delivery]:
        """Продублировать инцидент в общий чат команды."""

    async def status_changed(
        self, incident: Incident, service_name: str, messages: list[tuple[int, int]], contacts: list[Contact]
    ) -> None:
        """Инцидент взят или решён: обновить сообщения и сообщить тем, кого звали."""
