"""Расширения: как дополнительные модули (например, «Дежурка Pro») встраиваются в ядро.

Расширение — объект с атрибутом ``name`` и необязательными хуками:

* ``migrations`` — :class:`dezhurka.db.MigrationSet` со своими таблицами и своей таблицей версий;
* ``setup(app, config)`` — при создании приложения: свои маршруты, middleware, замена авторизации
  (``app.state.auth``);
* ``async startup(app)`` — после подготовки БД и движка (``app.state.engine``, ``app.state.db``);
* ``async shutdown(app)`` — при остановке, в обратном порядке.

Расширения находятся через entry points группы ``dezhurka.extensions``: пакет расширения
объявляет в своём pyproject.toml::

    [project.entry-points."dezhurka.extensions"]
    pro = "dezhurka_pro:extension"

Значение — объект расширения или вызываемый объект без аргументов, который его возвращает.
Ошибка загрузки расширения останавливает запуск: сервис с отвалившимся модулем
авторизации не должен молча стартовать без него.
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .config import Config
    from .db import MigrationSet

log = logging.getLogger("dezhurka.extensions")
GROUP = "dezhurka.extensions"


@runtime_checkable
class Extension(Protocol):
    name: str


class ExtensionError(RuntimeError):
    pass


def _instantiate(obj: Any, source: str) -> Extension:
    if isinstance(obj, type):  # класс расширения
        ext = obj()
    elif hasattr(obj, "name"):  # готовый объект
        ext = obj
    elif callable(obj):  # фабрика
        ext = obj()
    else:
        ext = obj
    if not isinstance(getattr(ext, "name", None), str) or not ext.name:
        raise ExtensionError(f"{source}: у расширения нет строкового атрибута name")
    return ext


def load_extensions() -> list[Extension]:
    """Найти и загрузить все установленные расширения."""
    exts: list[Extension] = []
    seen: set[str] = set()
    for ep in sorted(entry_points(group=GROUP), key=lambda e: e.name):
        try:
            ext = _instantiate(ep.load(), f"entry point {ep.name}")
        except ExtensionError:
            raise
        except Exception as e:
            raise ExtensionError(f"Не удалось загрузить расширение {ep.name} ({ep.value}): {e}") from e
        if ext.name in seen:
            raise ExtensionError(f"Расширение {ext.name} установлено дважды")
        seen.add(ext.name)
        exts.append(ext)
    if exts:
        log.info("Расширения: %s", ", ".join(e.name for e in exts))
    return exts


def migration_sets(exts: list[Extension]) -> list[MigrationSet]:
    return [m for e in exts if (m := getattr(e, "migrations", None)) is not None]


def run_setup(exts: list[Extension], app: FastAPI, config: Config) -> None:
    for ext in exts:
        hook = getattr(ext, "setup", None)
        if hook:
            hook(app, config)


async def run_startup(exts: list[Extension], app: FastAPI) -> None:
    for ext in exts:
        hook = getattr(ext, "startup", None)
        if hook:
            await hook(app)


async def run_shutdown(exts: list[Extension], app: FastAPI) -> None:
    for ext in reversed(exts):
        hook = getattr(ext, "shutdown", None)
        if hook:
            try:
                await hook(app)
            except Exception:
                log.exception("ошибка при остановке расширения %s", ext.name)
