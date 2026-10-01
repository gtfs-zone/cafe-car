from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from gtfs_zone_rt_api.settings import get_settings

_engine = None


def _asyncpg_url(url: str) -> str:
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            _asyncpg_url(str(settings.database_url)),
            echo=False,
        )
    return _engine


def get_session_factory() -> sessionmaker[AsyncSession]:
    return sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
