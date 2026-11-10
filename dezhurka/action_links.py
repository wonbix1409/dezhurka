"""Подписанные ссылки «Беру» / «Решено» для писем.

Ссылка работает без входа в панель: в ней зашиты инцидент, действие, кто действует и срок
жизни, всё подписано HMAC от settings.secret_key. Подделать или продлить её нельзя.

Открытие ссылки (GET) ничего не меняет — показывает страницу с кнопкой. Действие выполняется
только по нажатию (POST): почтовые сервисы и антивирусы сами открывают ссылки из писем,
и робот не должен «взять» инцидент за дежурного.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ACTIONS = ("ack", "resolve")
DEFAULT_TTL = timedelta(hours=24)


class LinkError(ValueError):
    pass


@dataclass(frozen=True)
class ActionClaim:
    incident_id: int
    action: str
    user_id: str
    expires: datetime


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _ts(dt: datetime) -> int:
    """naive UTC (как везде в проекте) → unix-время."""
    return int(dt.replace(tzinfo=UTC).timestamp())


def _sign(secret: str, payload: str) -> str:
    # префикс отделяет эти подписи от кодов привязки Telegram на том же секрете
    mac = hmac.new(secret.encode(), f"action-link:{payload}".encode(), hashlib.sha256).digest()
    return _b64(mac[:18])


def make_token(
    secret: str, incident_id: int, action: str, user_id: str, now: datetime, ttl: timedelta = DEFAULT_TTL
) -> str:
    """now и срок — naive UTC."""
    if action not in ACTIONS:
        raise LinkError(f"неизвестное действие {action}")
    exp = _ts(now + ttl)
    payload = _b64(json.dumps([incident_id, action, user_id, exp], separators=(",", ":")).encode())
    return f"{payload}.{_sign(secret, payload)}"


def read_token(secret: str, token: str, now: datetime) -> ActionClaim:
    try:
        payload, sig = token.split(".")
    except ValueError as e:
        raise LinkError("ссылка повреждена") from e
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        raise LinkError("ссылка повреждена или подделана")
    try:
        incident_id, action, user_id, exp = json.loads(_unb64(payload))
        expires = datetime.fromtimestamp(int(exp), UTC).replace(tzinfo=None)
    except (ValueError, TypeError) as e:
        raise LinkError("ссылка повреждена") from e
    if action not in ACTIONS or not isinstance(incident_id, int) or not isinstance(user_id, str):
        raise LinkError("ссылка повреждена")
    if now >= expires:
        raise LinkError("срок действия ссылки истёк — откройте инцидент в панели")
    return ActionClaim(incident_id, action, user_id, expires)
