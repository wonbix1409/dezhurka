"""Окружение Alembic для схемы ядра «Дежурки».

Два режима:
* из приложения (dezhurka.db.init_db): соединение передаётся через config.attributes["connection"];
* вручную для разработки (`alembic revision --autogenerate`): URL берётся из DEZHURKA_DB_URL.
"""
from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from dezhurka.models import Base

config = context.config
target_metadata = Base.metadata
VERSION_TABLE = config.attributes.get("version_table", "alembic_version")


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        # SQLite не умеет ALTER большинства вещей — Alembic пересоздаёт таблицу
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def _run(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = os.environ.get("DEZHURKA_DB_URL", "sqlite+aiosqlite:///./data/dezhurka.db")

    async def main() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(_run)
        await engine.dispose()

    asyncio.run(main())


if context.is_offline_mode():
    raise SystemExit("Офлайн-режим (генерация SQL) не поддерживается — подключитесь к БД")
run_migrations_online()
