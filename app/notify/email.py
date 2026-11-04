from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..config import Smtp

log = logging.getLogger("dezhurka.email")


class EmailSender:
    def __init__(self, cfg: Smtp) -> None:
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.host and self.cfg.sender)

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.cfg.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if self.cfg.ssl:
            server = smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, timeout=15, context=ctx)
        else:
            server = smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=15)
        with server:
            if self.cfg.starttls and not self.cfg.ssl:
                server.starttls(context=ctx)
            if self.cfg.username:
                server.login(self.cfg.username, self.cfg.password)
            server.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> str | None:
        """Возвращает текст ошибки или None при успехе."""
        try:
            await asyncio.to_thread(self._send_sync, to, subject, body)
            return None
        except Exception as e:  # сеть, авторизация, отказ сервера
            log.warning("письмо на %s не ушло: %s", to, e)
            return str(e) or e.__class__.__name__
