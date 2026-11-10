"""Авторизация панели и REST API.

Ядро проверяет один общий пароль (Basic). Расширение может заменить бэкенд —
положить в ``app.state.auth`` объект с методом ``async authenticate(request) -> Principal``
(например, вход через OIDC/Keycloak с пользователями и ролями).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.security import HTTPBasic

from .config import Config


@dataclass(frozen=True)
class Principal:
    """Кто выполняет запрос."""

    id: str
    name: str
    roles: frozenset[str] = field(default_factory=frozenset)
    # id пользователя из конфига (users[].id), если вход привязан к конкретному человеку
    user_id: str | None = None
    # общий пароль: кто именно действует, из учётки не видно (в панели имя выбирают вручную)
    shared: bool = False


class AuthBackend(Protocol):
    async def authenticate(self, request: Request) -> Principal: ...


class BasicPasswordAuth:
    """Один общий пароль из settings.admin_password; логин любой."""

    def __init__(self, config: Config) -> None:
        self._password = config.settings.admin_password.encode()
        self._basic = HTTPBasic(realm="Dezhurka")

    async def authenticate(self, request: Request) -> Principal:
        creds = await self._basic(request)  # без заголовка — 401 с WWW-Authenticate
        if not secrets.compare_digest(creds.password.encode(), self._password):
            raise HTTPException(401, "Неверный пароль", headers={"WWW-Authenticate": 'Basic realm="Dezhurka"'})
        login = creds.username or "admin"
        return Principal(id=login, name=login, roles=frozenset({"admin"}), shared=True)


async def require_principal(request: Request) -> Principal:
    """FastAPI-зависимость для всех закрытых маршрутов — и ядра, и расширений."""
    principal = await request.app.state.auth.authenticate(request)
    # Защита форм от CSRF: браузер сам подставляет авторизацию (Basic, cookie) в любые запросы
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Запрос с чужого сайта")
    request.state.principal = principal
    return principal
