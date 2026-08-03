from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from cafe_car.database import get_engine, get_session_factory
from cafe_car.routers.catalog import router as catalog_router
from cafe_car.routers.gtfs_rt import router as gtfs_rt_router
from cafe_car.routers.ingest import router as ingest_router
from cafe_car.routers.internal import router as internal_router
from cafe_car.settings import get_settings


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
    settings = get_settings()
    include_in_schema = settings.debug

    app = FastAPI(
        title="cafe-car",
        lifespan=lifespan,
    )

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    app.include_router(gtfs_rt_router)
    # In the schema unconditionally, unlike the two below: this one is public
    # API that another site consumes, so its shape is worth publishing.
    app.include_router(catalog_router)
    app.include_router(ingest_router, include_in_schema=include_in_schema)
    app.include_router(internal_router, include_in_schema=include_in_schema)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        redis: aioredis.Redis = request.app.state.redis
        await redis.ping()
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_public_app()
