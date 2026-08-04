"""Test database.

SQLite in memory, not Postgres. The models are dialect-agnostic (no JSONB,
arrays or enums), so ``SQLModel.metadata.create_all`` reproduces the schema
faithfully enough for the logic under test, and the suite needs no running
service. What it does *not* cover is the Alembic migrations; those keep being
round-tripped against the dev database by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import railroad_club.models  # noqa: F401 - registers every table on the metadata
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.pool import ConnectionPoolEntry


@pytest.fixture(scope="session")
def engine() -> AsyncEngine:
    # StaticPool: every ``sqlite://`` connection otherwise opens its *own* empty
    # in-memory database, so tables created on one connection are invisible to
    # the next. One shared connection keeps the schema alive for the session.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # SQLite ignores foreign keys unless asked. Without this every
    # ondelete="CASCADE" / "SET NULL" assertion in the suite would pass
    # vacuously, which is worse than not asserting at all.
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_pragma(
        dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    """A clean database per test."""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)

    # expire_on_commit=False mirrors cafe_car.database.get_session_factory: the
    # code under test reads attributes off objects after committing them.
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
