"""Миграции схемы: свежая база, база до миграций, повторный запуск, расширения."""
from __future__ import annotations

import asyncio
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from dezhurka.db import CORE_MIGRATIONS, MigrationSet, current_revision, head_revision, init_db, make_engine
from dezhurka.models import Base


def _run(coro):
    return asyncio.run(coro)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


async def _diff(engine) -> list:
    def check(conn):
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        return await conn.run_sync(check)


async def _revision(engine, mset=CORE_MIGRATIONS):
    async with engine.connect() as conn:
        return await conn.run_sync(current_revision, mset)


def test_fresh_db_matches_models(tmp_path):
    async def main():
        engine = make_engine(_url(tmp_path))
        await init_db(engine)
        # Миграции дают ровно ту схему, что описана в моделях: если модель поменяли,
        # а миграцию не написали — тест упадёт
        assert await _diff(engine) == []
        assert await _revision(engine) == head_revision()
        await engine.dispose()

    _run(main())


def test_legacy_db_is_stamped_and_keeps_data(tmp_path):
    async def main():
        engine = make_engine(_url(tmp_path))
        # Так создавались базы до появления миграций
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("INSERT INTO kv (key, value) VALUES ('tg_offset', '42')"))
        await init_db(engine)
        assert await _revision(engine) == head_revision()
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT value FROM kv WHERE key='tg_offset'"))).scalar() == "42"
        assert await _diff(engine) == []
        await engine.dispose()

    _run(main())


def test_init_db_is_idempotent(tmp_path):
    async def main():
        engine = make_engine(_url(tmp_path))
        await init_db(engine)
        await init_db(engine)
        assert await _revision(engine) == head_revision()
        await engine.dispose()

    _run(main())


def test_extension_migrations_use_own_version_table(tmp_path):
    ext_dir = tmp_path / "ext_migrations"
    (ext_dir / "versions").mkdir(parents=True)
    (ext_dir / "env.py").write_text(
        "from alembic import context\n"
        "conn = context.config.attributes['connection']\n"
        "context.configure(connection=conn, target_metadata=None,\n"
        "                  version_table=context.config.attributes['version_table'])\n"
        "with context.begin_transaction():\n"
        "    context.run_migrations()\n",
        encoding="utf-8",
    )
    (ext_dir / "versions" / "e001_users.py").write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision = 'e001'\ndown_revision = None\nbranch_labels = None\ndepends_on = None\n"
        "def upgrade():\n"
        "    op.create_table('ext_users', sa.Column('id', sa.Integer, primary_key=True))\n"
        "def downgrade():\n"
        "    op.drop_table('ext_users')\n",
        encoding="utf-8",
    )
    ext = MigrationSet(script_location=str(ext_dir), version_table="alembic_version_ext")

    async def main():
        engine = make_engine(_url(tmp_path))
        await init_db(engine, [ext])
        await init_db(engine, [ext])  # повторно — без ошибок
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert {"ext_users", "alembic_version", "alembic_version_ext", "incidents"} <= tables
        assert await _revision(engine) == head_revision()
        assert await _revision(engine, ext) == "e001"
        await engine.dispose()

    _run(main())
