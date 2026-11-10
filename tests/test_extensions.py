"""Расширения: хуки, замена авторизации, загрузка через entry points."""
from __future__ import annotations

import base64

import pytest
from fastapi import Depends, HTTPException, Request
from fastapi.testclient import TestClient

from dezhurka import extensions as ext_mod
from dezhurka.auth import Principal, require_principal
from dezhurka.config import Config
from dezhurka.extensions import ExtensionError, load_extensions
from dezhurka.main import create_app
from tests.conftest import CONFIG

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin123").decode()}


def _config(tmp_path) -> Config:
    cfg = dict(CONFIG)
    cfg["settings"] = {**CONFIG["settings"], "database_url": f"sqlite+aiosqlite:///{tmp_path}/ext.db"}
    return Config.model_validate(cfg)


class TokenAuth:
    """Пример бэкенда расширения: вход по заголовку X-Token вместо общего пароля."""

    async def authenticate(self, request: Request) -> Principal:
        if request.headers.get("x-token") != "secret":
            raise HTTPException(401, "нужен токен")
        return Principal(id="u1", name="Мария", roles=frozenset({"responder"}), user_id="ivan")


class DemoExtension:
    name = "demo"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def setup(self, app, config):
        self.calls.append("setup")
        app.state.auth = TokenAuth()

        @app.get("/demo/whoami")
        async def whoami(principal: Principal = Depends(require_principal)):
            return {"name": principal.name, "roles": sorted(principal.roles), "user_id": principal.user_id}

    async def startup(self, app):
        # к этому моменту движок и БД уже готовы
        assert app.state.engine is not None and app.state.db is not None
        self.calls.append("startup")

    async def shutdown(self, app):
        self.calls.append("shutdown")


def test_extension_hooks_routes_and_auth(tmp_path):
    ext = DemoExtension()
    app = create_app(_config(tmp_path), start_background=False, extensions=[ext])
    assert ext.calls == ["setup"]
    with TestClient(app) as c:
        assert ext.calls == ["setup", "startup"]
        # маршрут расширения под авторизацией расширения
        assert c.get("/demo/whoami").status_code == 401
        r = c.get("/demo/whoami", headers={"X-Token": "secret"})
        assert r.json() == {"name": "Мария", "roles": ["responder"], "user_id": "ivan"}
        # маршруты ядра тоже перешли на новую авторизацию: старый пароль больше не работает
        assert c.get("/", headers=AUTH).status_code == 401
        assert c.get("/", headers={"X-Token": "secret"}).status_code == 200
        # вебхуки мониторинга авторизуются ключом интеграции, а не входом в панель
        r = c.post("/api/v1/ingest/api-key-0123456789/generic", json={"title": "x"})
        assert r.status_code == 200
    assert ext.calls == ["setup", "startup", "shutdown"]


def test_csrf_check_applies_to_extension_auth(tmp_path):
    app = create_app(_config(tmp_path), start_background=False, extensions=[DemoExtension()])
    with TestClient(app) as c:
        r = c.post(
            "/schedules/primary/overrides/1/delete",
            headers={"X-Token": "secret", "Origin": "https://evil.example"},
            follow_redirects=False,
        )
        assert r.status_code == 403


class _EP:
    def __init__(self, name, obj, value="pkg:obj"):
        self.name, self._obj, self.value = name, obj, value

    def load(self):
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def test_load_extensions_from_entry_points(monkeypatch):
    class ByClass:
        name = "by-class"

    by_object = DemoExtension()

    def factory():
        e = DemoExtension()
        e.name = "by-factory"
        return e

    eps = [_EP("b", ByClass), _EP("a", by_object), _EP("c", factory)]
    monkeypatch.setattr(ext_mod, "entry_points", lambda group: eps if group == ext_mod.GROUP else [])
    loaded = load_extensions()
    assert [e.name for e in loaded] == ["demo", "by-class", "by-factory"]  # по имени entry point
    assert isinstance(loaded[1], ByClass)  # класс превращён в экземпляр


@pytest.mark.parametrize(
    "eps, message",
    [
        ([_EP("x", ImportError("no module"))], "Не удалось загрузить расширение x"),
        ([_EP("x", object())], "нет строкового атрибута name"),
        ([_EP("x", DemoExtension()), _EP("y", DemoExtension())], "установлено дважды"),
    ],
)
def test_broken_extension_stops_startup(monkeypatch, eps, message):
    monkeypatch.setattr(ext_mod, "entry_points", lambda group: eps)
    with pytest.raises(ExtensionError, match=message):
        load_extensions()


def test_no_extensions_by_default(monkeypatch):
    monkeypatch.setattr(ext_mod, "entry_points", lambda group: [])
    assert load_extensions() == []
