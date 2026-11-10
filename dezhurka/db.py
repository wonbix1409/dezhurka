from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

log = logging.getLogger("dezhurka.db")


@dataclass(frozen=True)
class MigrationSet:
    """Набор миграций Alembic со своей таблицей версий.

    У ядра и у каждого расширения (например, модулей Pro) — свой набор: они обновляются
    независимо и не мешают друг другу.
    """

    script_location: str
    version_table: str = "alembic_version"
    # Базы, созданные до появления миграций: если есть эта таблица, а таблицы версий нет —
    # схема уже соответствует ревизии baseline, её достаточно отметить (stamp).
    legacy_table: str | None = None
    baseline: str | None = None


CORE_MIGRATIONS = MigrationSet(
    script_location=str(Path(__file__).parent / "migrations"),
    legacy_table="incidents",
    baseline="0001",
)


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        # Создаём каталог под файл БД, если его ещё нет
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(url, future=True)


def _alembic_config(mset: MigrationSet, connection: Connection) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", mset.script_location)
    cfg.attributes["connection"] = connection
    cfg.attributes["version_table"] = mset.version_table
    return cfg


def _upgrade(connection: Connection, mset: MigrationSet) -> None:
    cfg = _alembic_config(mset, connection)
    tables = set(inspect(connection).get_table_names())
    if mset.legacy_table and mset.legacy_table in tables and mset.version_table not in tables:
        log.info("База создана до миграций — отмечаю ревизию %s (%s)", mset.baseline, mset.version_table)
        command.stamp(cfg, mset.baseline)
    command.upgrade(cfg, "head")


def current_revision(connection: Connection, mset: MigrationSet = CORE_MIGRATIONS) -> str | None:
    ctx = MigrationContext.configure(connection, opts={"version_table": mset.version_table})
    return ctx.get_current_revision()


def head_revision(mset: MigrationSet = CORE_MIGRATIONS) -> str | None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", mset.script_location)
    return ScriptDirectory.from_config(cfg).get_current_head()


async def init_db(engine: AsyncEngine, extra: Sequence[MigrationSet] = ()) -> None:
    """Довести схему до актуальной: сначала ядро, затем расширения."""
    for mset in (CORE_MIGRATIONS, *extra):
        async with engine.begin() as conn:
            await conn.run_sync(_upgrade, mset)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
