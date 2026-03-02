from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from app.database import get_engine
from app.routers.gtfs_rt import router as gtfs_rt_router
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

    return app


app = create_public_app()
