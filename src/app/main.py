from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from sqlalchemy import text

from app.database import get_engine, get_session_factory
from app.routers.gtfs_rt import router as gtfs_rt_router
from app.routers.mqtt_auth import router as mqtt_auth_router
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    # Verify Redis connectivity on startup
    redis = aioredis.from_url(str(settings.redis_url))
    await redis.ping()
    app.state.redis = redis

    yield

    await redis.aclose()
    await get_engine().dispose()


def create_public_app() -> FastAPI:
    app = FastAPI(
        title="redis-gtfs-rt-api",
        lifespan=lifespan,
    )

    app.include_router(gtfs_rt_router)
    app.include_router(mqtt_auth_router)

    @app.get("/health")
    async def health(request: Request):
        redis: aioredis.Redis = request.app.state.redis
        await redis.ping()
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_public_app()
