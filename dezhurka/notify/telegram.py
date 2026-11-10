"""Минимальный клиент Telegram Bot API на httpx — без тяжёлых зависимостей."""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..models import Incident, Status

log = logging.getLogger("dezhurka.telegram")


class TelegramError(Exception):
    pass


def proxy_for_log(url: str) -> str:
    """Адрес прокси без логина и пароля."""
    return re.sub(r"//[^@/]*@", "//***@", url)


class TelegramAPI:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.telegram.org",
        client: httpx.AsyncClient | None = None,
        proxy: str | None = None,
    ):
        self.token = token
        self.base = f"{base_url.rstrip('/')}/bot{token}"
        self.proxy = proxy or None
        # Таймаут больше, чем long polling getUpdates
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0), proxy=self.proxy)
        if self.proxy:
            log.info("Telegram Bot API через прокси %s", proxy_for_log(self.proxy))

    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        resp = await self.client.post(f"{self.base}/{method}", json=payload)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', resp.status_code)}")
        return data["result"]

    async def close(self) -> None:
        await self.client.aclose()


def incident_keyboard(incident: Incident) -> dict | None:
    if incident.status == Status.TRIGGERED:
        buttons = [
            {"text": "👀 Беру", "callback_data": f"ack:{incident.id}"},
            {"text": "✅ Решено", "callback_data": f"res:{incident.id}"},
        ]
    elif incident.status == Status.ACKNOWLEDGED:
        buttons = [{"text": "✅ Решено", "callback_data": f"res:{incident.id}"}]
    else:
        return {"inline_keyboard": []}
    return {"inline_keyboard": [buttons]}
