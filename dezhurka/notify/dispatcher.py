"""Рассылка по всем включённым каналам: Telegram и почта."""
from __future__ import annotations

import logging

from ..action_links import make_token
from ..models import Incident, Status, utcnow
from .base import Contact, Delivery
from .email import EmailSender
from .telegram import TelegramAPI, TelegramError, incident_keyboard
from .text import email_body, email_html, email_subject, telegram_html

log = logging.getLogger("dezhurka.dispatch")


class MultiDispatcher:
    def __init__(
        self,
        public_url: str,
        telegram: TelegramAPI | None,
        email: EmailSender | None,
        link_secret: str | None = None,
    ) -> None:
        self.public_url = public_url
        self.telegram = telegram
        self.email = email if email and email.enabled else None
        # Секрет для ссылок «Беру»/«Решено» в письмах; без него письма только со ссылкой в панель
        self.link_secret = link_secret

    def email_actions(self, incident: Incident, user_id: str) -> list[tuple[str, str]]:
        if not self.link_secret:
            return []
        if incident.status == Status.TRIGGERED:
            wanted = [("Беру", "ack"), ("Решено", "resolve")]
        elif incident.status == Status.ACKNOWLEDGED:
            wanted = [("Решено", "resolve")]
        else:
            return []
        base = self.public_url.rstrip("/")
        now = utcnow()
        return [
            (label, f"{base}/a/{make_token(self.link_secret, incident.id, action, user_id, now)}")
            for label, action in wanted
        ]

    async def _tg_send(self, chat_id: int, incident: Incident, service_name: str, reason: str | None) -> Delivery:
        try:
            msg = await self.telegram.call(
                "sendMessage",
                chat_id=chat_id,
                text=telegram_html(incident, service_name, self.public_url, reason),
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                reply_markup=incident_keyboard(incident),
            )
            return Delivery("telegram", True, chat_id, msg["message_id"])
        except (TelegramError, Exception) as e:
            log.warning("telegram %s: %s", chat_id, e)
            return Delivery("telegram", False, chat_id, error=str(e))

    async def page(self, contact: Contact, incident: Incident, service_name: str, reason: str) -> list[Delivery]:
        out: list[Delivery] = []
        if self.telegram and contact.telegram_chat_id:
            out.append(await self._tg_send(contact.telegram_chat_id, incident, service_name, reason))
        if self.email and contact.email:
            actions = self.email_actions(incident, contact.user_id)
            err = await self.email.send(
                contact.email,
                email_subject(incident, service_name),
                email_body(incident, service_name, self.public_url, reason, actions=actions),
                html=email_html(incident, service_name, self.public_url, reason, actions=actions),
            )
            out.append(Delivery("email", err is None, error=err or ""))
        return out

    async def team_post(self, chat_id: int, incident: Incident, service_name: str) -> list[Delivery]:
        if not self.telegram:
            return []
        return [await self._tg_send(chat_id, incident, service_name, "Новый инцидент")]

    async def status_changed(
        self, incident: Incident, service_name: str, messages: list[tuple[int, int]], contacts: list[Contact]
    ) -> None:
        if self.telegram:
            text = telegram_html(incident, service_name, self.public_url)
            for chat_id, message_id in messages:
                try:
                    await self.telegram.call(
                        "editMessageText",
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode="HTML",
                        link_preview_options={"is_disabled": True},
                        reply_markup=incident_keyboard(incident),
                    )
                except Exception as e:  # сообщение могли удалить — не страшно
                    log.info("не удалось обновить сообщение %s/%s: %s", chat_id, message_id, e)
        # Тем, кого звали только по почте, сообщаем письмом
        if self.email:
            for c in contacts:
                if c.email and not (self.telegram and c.telegram_chat_id):
                    actions = self.email_actions(incident, c.user_id)
                    await self.email.send(
                        c.email,
                        email_subject(incident, service_name),
                        email_body(incident, service_name, self.public_url, actions=actions),
                        html=email_html(incident, service_name, self.public_url, actions=actions),
                    )
